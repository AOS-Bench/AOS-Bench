"""Incremental parser for Claude Code session ``*.jsonl`` transcripts.

Where kimi-cli emits a flat ``wire.jsonl`` of typed protocol messages, Claude
Code persists its transcript as a JSON-lines log under
``$CLAUDE_CONFIG_DIR/projects/<project>/<session>/*.jsonl``. Each line is one
event with a ``type`` (``assistant`` / ``user`` / ``system``), a ``message``
object (Anthropic message shape: ``content`` blocks, ``usage``, ``model``,
``id``), plus envelope fields ``timestamp`` / ``sessionId`` / ``isSidechain`` /
``agentId`` / ``version`` / ``cwd`` / ``gitBranch``.

The offline collector for this format lives in harbor at
``harbor/src/harbor/agents/installed/claude_code.py:_convert_events_to_trajectory``.
We deliberately mirror that normalization (text/reasoning/tool-use extraction,
``last usage per message id`` accounting, ``tool_use`` → ``tool_result``
pairing) so that the live monitor and the post-mortem ATIF trace agree on what
happened. The key difference: we emit the **monitor's** canonical event shape
(same ``kind`` / ``payload`` / ``step_id`` / ``metrics`` produced by
``aos_bench.monitor.tail.parser`` for kimi) so the existing dashboard renders a
Claude Code trial with zero frontend changes:

    assistant text/think block   → kind="message"        payload={type, text|think}
    assistant tool_use block      → kind="tool_call"       payload={id, function:{name, arguments}}
    user tool_result block        → kind="tool_result"     payload={id, return_value}
    assistant turn (first of id)  → kind="step_begin"      payload={n}        (LLM-call proxy)
    Task/Agent tool_use           → kind="subagent_spawn"  payload={child_run_id, ...}
    token usage on assistant turn → kind="status"          payload={token_usage}

Claude Code interleaves *subagent* activity into the same transcript with
``isSidechain: true`` and a distinct ``agentId``. The parser does not itself
split runs — it tags every produced event with ``extra.agent_id`` /
``extra.is_sidechain`` and the caller (:class:`ClaudeSessionTail`) routes events
to the right run, synthesizing a child run + spawn marker the first time it sees
a new ``agentId``.
"""

from __future__ import annotations

import json
from typing import Any

# Claude Code tool names that spawn a child agent. ``Task`` is the public tool
# name; ``Agent`` is the internal/newer alias used by some builds (mirrors the
# kimi parser, which keys off ``Agent``).
_SPAWN_TOOL_NAMES = frozenset({"Task", "Agent"})


def _coerce_ts(value: Any) -> float | None:
    """Claude Code timestamps are ISO-8601 strings (``2026-05-23T10:11:12.345Z``).

    The monitor's event/timeline code keys off float epoch seconds (kimi-cli
    wire timestamps are already floats), so normalize here. Returns ``None`` for
    anything unparseable — downstream tolerates a missing timestamp.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    # ``datetime.fromisoformat`` handles offsets but historically choked on a
    # trailing ``Z`` (pre-3.11) — normalize to ``+00:00`` defensively.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _extract_text_reasoning_tools(
    content: Any,
) -> tuple[str, str | None, list[dict[str, Any]]]:
    """Split an assistant ``message.content`` into (text, reasoning, tool_uses).

    ``content`` is either a plain string or a list of typed blocks
    (``text`` / ``thinking`` / ``redacted_thinking`` / ``tool_use``). Mirrors
    harbor's ``_extract_text_reasoning_tool_uses``.
    """
    if isinstance(content, str):
        return content, None, []
    if not isinstance(content, list):
        return "", None, []
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if isinstance(t, str) and t:
                text_parts.append(t)
        elif btype in ("thinking", "redacted_thinking"):
            t = block.get("thinking") or block.get("text")
            if isinstance(t, str) and t:
                reasoning_parts.append(t)
        elif btype == "tool_use":
            tool_uses.append(block)
    text = "\n".join(text_parts)
    reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
    return text, reasoning, tool_uses


def _stringify(value: Any, cap: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)[:cap]
    except (TypeError, ValueError):
        return str(value)[:cap]


def _tool_result_text(block: dict[str, Any]) -> str:
    """Flatten a ``tool_result`` content block to a string body."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for sub in content:
            if isinstance(sub, dict) and sub.get("type") == "text":
                t = sub.get("text")
                if isinstance(t, str):
                    parts.append(t)
            else:
                parts.append(_stringify(sub))
        return "\n".join(p for p in parts if p)
    return _stringify(content)


class IncrementalClaudeParser:
    """Feed Claude Code transcript records and emit normalized monitor events.

    State carried across :meth:`feed` calls (one parser per *run* — root or a
    single subagent):

    - ``seq``: monotonic event sequence within the run.
    - ``step_id``: id of the most recent synthesized ``step_begin``.
    - ``step_n``: assistant-turn counter (the LLM-call proxy).
    - token totals from ``message.usage`` (accumulated; one charge per message
      id, since Claude Code re-emits the running usage on each streamed chunk).
    - ``pending_calls``: ``tool_use`` ids awaiting their ``tool_result``.
    - ``seen_message_ids``: assistant message ids already charged for usage.
    - ``synthesized_spawns``: ``Task``/``Agent`` tool_use ids already turned
      into a ``subagent_spawn`` event (one spawn marker per call).
    """

    def __init__(self, *, run_id: str, synthesize_subagent_spawns: bool = False) -> None:
        self.run_id = run_id
        self.synthesize = synthesize_subagent_spawns
        self.seq = 0
        self.step_id: str | None = None
        self.step_n = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.seen_message_ids: set[str] = set()
        self.pending_calls: dict[str, bool] = {}
        self.synthesized_spawns: set[str] = set()
        self.first_ts: float | None = None
        self.last_ts: float | None = None

    # ── helpers ──

    def _next_event(
        self,
        *,
        kind: str,
        phase: str,
        ts: float | None,
        payload: dict[str, Any],
        metrics: dict[str, Any] | None,
        message_type: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.seq += 1
        return {
            "event_id": f"{self.run_id}_evt_{self.seq:04d}",
            "seq": self.seq,
            "timestamp": ts,
            "kind": kind,
            "phase": phase,
            "step_id": self.step_id,
            "parent_event_id": None,
            "payload": payload,
            "metrics": metrics or {},
            "raw_ref": {"source": "claude-code-jsonl", "message_type": message_type},
            "extra": extra or {},
        }

    @staticmethod
    def _usage_metrics(usage: Any) -> dict[str, Any]:
        if not isinstance(usage, dict):
            return {}
        prompt = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        )
        completion = int(usage.get("output_tokens", 0) or 0)
        return {"prompt_tokens": prompt, "completion_tokens": completion}

    # ── main entry ──

    def feed(self, raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert raw transcript records to normalized monitor events.

        Returns newly produced events; the caller pushes them into MonitorState.
        Records with no ``message`` dict (pure ``system`` envelopes, summaries)
        are skipped, matching harbor's behaviour.
        """
        out: list[dict[str, Any]] = []
        for record in raw_records:
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            event_type = record.get("type")
            ts = _coerce_ts(record.get("timestamp"))
            if ts is not None:
                if self.first_ts is None:
                    self.first_ts = ts
                self.last_ts = ts

            agent_id = record.get("agentId")
            is_sidechain = bool(record.get("isSidechain"))
            base_extra = {"agent_id": agent_id, "is_sidechain": is_sidechain}

            if event_type == "assistant":
                out.extend(self._feed_assistant(message, ts, base_extra))
            elif event_type == "user":
                out.extend(self._feed_user(message, ts, base_extra))
            # ``system`` / other types carry no agent-visible activity we render.
        return out

    def _feed_assistant(
        self, message: dict[str, Any], ts: float | None, base_extra: dict[str, Any]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        text, reasoning, tool_uses = _extract_text_reasoning_tools(message.get("content"))
        model_name = message.get("model")
        msg_id = message.get("id")

        # One ``step_begin`` per assistant turn (= one LLM call). Claude Code
        # re-emits the same message id across streamed chunks; only the first
        # sighting opens a new step + is charged for usage.
        new_turn = not (isinstance(msg_id, str) and msg_id in self.seen_message_ids)
        if new_turn:
            self.step_n += 1
            self.step_id = f"{self.run_id}_step_{self.step_n}"
            out.append(
                self._next_event(
                    kind="step_begin",
                    phase="begin",
                    ts=ts,
                    payload={"n": self.step_n, "model": model_name},
                    metrics=None,
                    message_type="assistant",
                    extra=base_extra,
                )
            )

        usage = message.get("usage")
        metrics = self._usage_metrics(usage) if new_turn else {}
        if new_turn and isinstance(msg_id, str):
            self.seen_message_ids.add(msg_id)
        if metrics:
            self.prompt_tokens += int(metrics.get("prompt_tokens", 0) or 0)
            self.completion_tokens += int(metrics.get("completion_tokens", 0) or 0)

        # Assistant reasoning → message(type=think); assistant text → message(type=text).
        # Match kimi's payload shape (frontend reads payload.type + payload.text/think).
        if reasoning:
            out.append(
                self._next_event(
                    kind="message",
                    phase="completed",
                    ts=ts,
                    payload={"type": "think", "think": reasoning, "role": "assistant"},
                    metrics=None,
                    message_type="assistant",
                    extra=base_extra,
                )
            )
        if text:
            out.append(
                self._next_event(
                    kind="message",
                    phase="completed",
                    ts=ts,
                    payload={"type": "text", "text": text, "role": "assistant"},
                    metrics=metrics if not reasoning else None,
                    message_type="assistant",
                    extra=base_extra,
                )
            )

        for block in tool_uses:
            call_id = block.get("id")
            name = block.get("name") or ""
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                arguments = {} if arguments is None else {"input": arguments}

            # Spawn marker for Task/Agent tool calls so the dashboard draws the
            # parent→child arrow on the root lane (kimi does the same).
            if (
                self.synthesize
                and name in _SPAWN_TOOL_NAMES
                and isinstance(call_id, str)
                and call_id
                and call_id not in self.synthesized_spawns
            ):
                subagent_type = arguments.get("subagent_type") or arguments.get("type")
                description = arguments.get("description") or arguments.get("prompt")
                out.append(
                    self._next_event(
                        kind="subagent_spawn",
                        phase="begin",
                        ts=ts,
                        payload={
                            "child_run_id": None,  # unknown until the agentId appears
                            "subagent_type": subagent_type,
                            "task_id": None,
                            "description": (description or "")[:200]
                            if isinstance(description, str)
                            else None,
                            "launch_mode": "foreground",
                            "parent_tool_call_id": call_id,
                        },
                        metrics=None,
                        message_type="tool_use:Task",
                        extra={**base_extra, "parent_tool_call_id": call_id},
                    )
                )
                self.synthesized_spawns.add(call_id)

            if isinstance(call_id, str) and call_id:
                self.pending_calls[call_id] = True
            out.append(
                self._next_event(
                    kind="tool_call",
                    phase="begin",
                    ts=ts,
                    payload={
                        "id": call_id,
                        "tool_call_id": call_id,
                        "function": {"name": name, "arguments": arguments},
                        "name": name,
                    },
                    metrics=None,
                    message_type="tool_use",
                    extra=base_extra,
                )
            )
        return out

    def _feed_user(
        self, message: dict[str, Any], ts: float | None, base_extra: dict[str, Any]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        content = message.get("content")

        if isinstance(content, str):
            if content.strip():
                out.append(
                    self._next_event(
                        kind="message",
                        phase="completed",
                        ts=ts,
                        payload={"type": "text", "text": content, "role": "user"},
                        metrics=None,
                        message_type="user",
                        extra=base_extra,
                    )
                )
            return out

        if not isinstance(content, list):
            return out

        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                text_parts.append(_stringify(block))
                continue
            if block.get("type") == "tool_result":
                call_id = block.get("tool_use_id")
                body = _tool_result_text(block)
                is_error = block.get("is_error") is True
                if isinstance(call_id, str):
                    self.pending_calls.pop(call_id, None)
                out.append(
                    self._next_event(
                        kind="tool_result",
                        phase="completed",
                        ts=ts,
                        payload={
                            "id": call_id,
                            "tool_call_id": call_id,
                            "return_value": body,
                            "error": is_error,
                        },
                        metrics=None,
                        message_type="user:tool_result",
                        extra={**base_extra, "is_error": is_error},
                    )
                )
            elif block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str) and t:
                    text_parts.append(t)
            else:
                text_parts.append(_stringify(block))

        joined = "\n\n".join(p.strip() for p in text_parts if p and p.strip())
        if joined:
            out.append(
                self._next_event(
                    kind="message",
                    phase="completed",
                    ts=ts,
                    payload={"type": "text", "text": joined, "role": "user"},
                    metrics=None,
                    message_type="user",
                    extra=base_extra,
                )
            )
        return out


def parse_claude_line(line: str) -> dict[str, Any] | None:
    """Parse one line of a Claude Code ``*.jsonl`` transcript.

    Returns the record dict, or ``None`` to skip (blank lines, parse errors,
    or records without a ``type`` field — e.g. file-summary header lines some
    Claude Code builds prepend).
    """
    line = line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if "type" not in parsed:
        return None
    return parsed
