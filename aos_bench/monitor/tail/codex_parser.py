"""Incremental parser for codex CLI ``rollout-*.jsonl`` session logs.

Where kimi-cli emits a flat ``wire.jsonl`` of typed protocol messages and Claude
Code persists an Anthropic-style transcript, codex writes a stream of
``{type, payload, timestamp}`` events under
``$CODEX_HOME/sessions/<yyyy>/<mm>/<dd>/rollout-<ts>-<sid>.jsonl``.

The offline collector for this format lives in harbor at
``harbor/src/harbor/agents/installed/codex.py:_convert_events_to_trajectory``.
We deliberately mirror that normalization so the live monitor and the
post-mortem ATIF trace agree on what happened. The key difference: we emit the
**monitor's** canonical event shape (same ``kind`` / ``payload`` / ``step_id``
/ ``metrics`` produced by ``aos_bench.monitor.tail.parser`` for kimi) so the
existing dashboard renders a codex trial with zero frontend changes:

    response_item / message       → kind="message"     payload={role, text}
    response_item / reasoning     → kind="message" payload={type:"think",
                                          text, encrypted_bytes}
    response_item / function_call → kind="tool_call"   payload={id,
                                          function:{name, arguments}}
    response_item / *_output      → kind="tool_result" payload={tool_call_id,
                                          return_value:{output}}
    event_msg / token_count       → kind="status"      payload={token_usage}
    event_msg / task_started      → kind="turn_begin"
    event_msg / task_complete     → kind="turn_end" + duration_ms metric
    turn_context                  → synthesize kind="step_begin" (per-turn
                                          LLM-call boundary; codex emits one
                                          per turn just before its first model
                                          call)

Codex CLI does **not** spawn sidechain agents in-process — multi-agent
orchestration with codex relies on an outer driver writing its own
``events.jsonl`` (caught by the AgentEventsTail). So this parser is a pure
single-run normalizer; no subagent spawn synthesis is needed.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

# Tool-call payload type names. ``function_call`` is the OpenAI Responses-API
# default; ``custom_tool_call`` is codex's wrapper for tools the CLI defines
# locally (e.g. ``apply_patch``). Both pair with their ``*_output`` counterpart
# by ``call_id``.
_TOOL_CALL_TYPES = frozenset({"function_call", "custom_tool_call"})
_TOOL_OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})


def _coerce_ts(value: Any) -> float | None:
    """codex timestamps are ISO-8601 strings (``2026-06-23T07:34:00.530Z``).

    Mirrors ``claude_parser._coerce_ts``. Returns float epoch seconds, or
    ``None`` for unparseable input — downstream tolerates a missing timestamp.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _extract_message_text(content: Any) -> tuple[str, str]:
    """Concatenate text from a ``message.content`` block array.

    Returns ``(text, role_hint)`` — role_hint is empty unless the parent
    payload didn't carry one. codex's content blocks are
    ``{"type": "input_text" | "output_text", "text": "..."}``.
    """
    if isinstance(content, str):
        return content, ""
    if not isinstance(content, list):
        return "", ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts), ""


def _extract_reasoning_summary(payload: dict[str, Any]) -> str | None:
    """codex 0.122+ wraps reasoning summary items as
    ``{"type": "summary_text", "text": "..."}``; older builds emitted bare
    strings. Accept both shapes (mirrors the harbor adapter fix).

    Returns the joined summary text, or ``None`` when ``summary[]`` is empty
    (which is the default ``summary=auto`` outcome for many models — the
    monitor still emits the reasoning event with an ``encrypted_bytes``
    metric so the dashboard can render "thinking happened" without text).
    """
    summary = payload.get("summary")
    if not isinstance(summary, list) or not summary:
        return None
    parts: list[str] = []
    for item in summary:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts) if parts else None


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Best-effort decode of a tool_call's arguments field.

    ``function_call`` payloads carry ``arguments`` as a JSON string;
    ``custom_tool_call`` carries ``input`` (also typically a JSON string but
    may be a raw blob like an apply_patch envelope). Always return a dict so
    downstream UI rendering doesn't have to type-check.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"input": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    if raw is None:
        return {}
    return {"value": raw}


class IncrementalCodexParser:
    """Feed codex rollout records one by one (or in batches) and emit
    normalized events. State carried across calls:

    - ``seq``: monotonic event sequence within this run
    - ``step_id``: most recent ``turn_context``-synthesized step id
    - ``default_model``: model name from the latest ``turn_context``
    - ``prompt_tokens`` / ``completion_tokens`` / ``cached_tokens`` /
      ``reasoning_tokens``: cumulative totals (codex token_count.info already
      reports cumulative; we keep the running last-known values for symmetry
      with the kimi parser)
    - ``cli_version`` / ``originator`` / ``session_id``: pulled from the
      ``session_meta`` header (one per session); surfaced via the first
      ``status`` event so downstream can label the run
    - ``pending_calls``: ``call_id → tool_name`` so a stray ``*_output``
      record (rare, but seen when codex writes them out of order) still
      pairs to a tool name
    - ``spawn_map``: ``call_id → child_agent_id`` discovered from each
      ``spawn_agent`` tool_call_output. The CodexSessionTail reads this
      after each feed() to map a freshly-discovered sub-rollout file
      (whose ``session_meta.id`` is the child agent_id) back to the
      parent ``spawn_agent`` call_id, so the dashboard can render the
      spawn arrow on the correct root step.
    - ``agent_role`` / ``agent_nickname`` / ``parent_thread_id`` /
      ``thread_id``: pulled from a ``session_meta`` payload when present
      (subagent rollouts carry these; root rollouts don't have
      ``agent_role``). Tail uses these to set the run's role / parent.
    """

    # Codex 0.122+ tool name that spawns a sub-agent (sibling concept to
    # claude-code's ``Task`` and kimi-cli's ``Agent`` tool).
    _SPAWN_TOOL_NAMES = frozenset({"spawn_agent"})

    def __init__(self, *, run_id: str) -> None:
        self.run_id = run_id
        self.seq = 0
        self.step_id: str | None = None
        self.turn_n: int = 0
        self.default_model: str | None = None
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.cached_tokens: int = 0
        self.reasoning_tokens: int = 0
        self.cli_version: str | None = None
        self.originator: str | None = None
        self.session_id: str | None = None
        # Subagent linkage. ``thread_id`` is this rollout's own thread id
        # (== child agent_id when this parser instance is a sub-rollout
        # parser); ``parent_thread_id`` points at the root's session_id;
        # ``agent_role`` is "worker" for sub, absent for root.
        self.thread_id: str | None = None
        self.parent_thread_id: str | None = None
        self.agent_role: str | None = None
        self.agent_nickname: str | None = None
        # call_id → {message, agent_type, fork_context, reasoning_effort}
        # (the args the root sent to spawn_agent).
        self.spawn_calls: dict[str, dict[str, Any]] = {}
        # call_id → child agent_id (parsed from spawn_agent's
        # function_call_output). Caller drains this to map newly-created
        # sub rollouts (whose session_meta.id == agent_id) to the parent
        # spawn call.
        self.spawn_map: dict[str, str] = {}
        # Child agent_ids already announced via subagent_spawn event so
        # we don't fire a second one if the same call_id reappears.
        self._synthesized_spawns: set[str] = set()
        self.pending_calls: dict[str, str] = {}
        self.first_ts: float | None = None
        self.last_ts: float | None = None

    def feed(self, raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert raw codex records to normalized event dicts. Returns
        newly produced events; the caller pushes them into MonitorState.

        Records of types ``event_msg/agent_message`` and
        ``event_msg/user_message`` are intentionally dropped: each pairs
        with a same-timestamp ``response_item/message`` that already
        carries the canonical text. Surfacing both would double-count
        every assistant turn in the dashboard.
        """
        out: list[dict[str, Any]] = []
        for record in raw_records:
            ts = _coerce_ts(record.get("timestamp"))
            if ts is not None:
                if self.first_ts is None:
                    self.first_ts = ts
                self.last_ts = ts

            etype = record.get("type")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                payload = {}

            if etype == "session_meta":
                # Cache the metadata; don't emit an event (dashboard reads it
                # off MonitorState directly via the trial summary).
                # ``session_id`` is the SHARED root session_id across the
                # whole trial; ``id`` is this rollout's own thread id.
                # Subagent rollouts have ``agent_role="worker"`` +
                # ``parent_thread_id`` pointing at the root's session_id.
                self.session_id = payload.get("session_id") or payload.get("id")
                self.thread_id = payload.get("id")
                self.parent_thread_id = payload.get("parent_thread_id")
                self.agent_role = payload.get("agent_role")
                self.agent_nickname = payload.get("agent_nickname")
                self.cli_version = payload.get("cli_version")
                self.originator = payload.get("originator")
                model_provider = payload.get("model_provider")
                if isinstance(model_provider, str):
                    pass  # surfaced indirectly via status events; not used here
                continue

            if etype == "turn_context":
                # Each turn_context marks the start of a new model call cycle.
                # Treat it as a step_begin so the dashboard's LLM-call counter
                # advances per turn (mirrors kimi's ``StepBegin``).
                model = payload.get("model")
                if isinstance(model, str) and model:
                    self.default_model = model
                self.turn_n += 1
                self.step_id = f"{self.run_id}_step_{self.turn_n}"
                effort = payload.get("effort") or payload.get(
                    "collaboration_mode", {}
                ).get("settings", {}).get("reasoning_effort")
                summary_mode = payload.get("summary")
                self.seq += 1
                out.append(
                    {
                        "event_id": f"{self.run_id}_evt_{self.seq:04d}",
                        "seq": self.seq,
                        "timestamp": ts,
                        "kind": "step_begin",
                        "phase": "begin",
                        "step_id": self.step_id,
                        "parent_event_id": None,
                        "payload": {
                            "n": self.turn_n,
                            "model": self.default_model,
                            "reasoning_effort": effort,
                            "reasoning_summary": summary_mode,
                            "turn_id": payload.get("turn_id"),
                        },
                        "metrics": {},
                        "raw_ref": {
                            "source": "codex-rollout",
                            "message_type": "turn_context",
                        },
                        "extra": {},
                    }
                )
                continue

            if etype == "event_msg":
                ptype = payload.get("type")
                if ptype == "task_started":
                    self.seq += 1
                    out.append(self._wrap(ts, "turn_begin", "begin", payload, {}, "event_msg/task_started"))
                    continue
                if ptype == "task_complete":
                    duration_ms = payload.get("duration_ms")
                    ttft = payload.get("time_to_first_token_ms")
                    metrics: dict[str, Any] = {}
                    if isinstance(duration_ms, (int, float)):
                        metrics["duration_ms"] = duration_ms
                    if isinstance(ttft, (int, float)):
                        metrics["time_to_first_token_ms"] = ttft
                    self.seq += 1
                    out.append(
                        self._wrap(
                            ts,
                            "turn_end",
                            "completed",
                            payload,
                            metrics,
                            "event_msg/task_complete",
                        )
                    )
                    continue
                if ptype == "token_count":
                    info = payload.get("info") or {}
                    if not isinstance(info, dict):
                        info = {}
                    total = info.get("total_token_usage") or {}
                    if not isinstance(total, dict):
                        total = {}
                    in_tok = int(total.get("input_tokens") or 0)
                    out_tok = int(total.get("output_tokens") or 0)
                    cache_tok = int(total.get("cached_input_tokens") or 0)
                    reasoning_tok = int(total.get("reasoning_output_tokens") or 0)
                    # codex reports CUMULATIVE totals on every token_count
                    # event; the dashboard's per-run rollup sums event metrics,
                    # so emit DELTAS to avoid double-counting. State carries
                    # the running totals.
                    prompt_delta = max(0, in_tok - self.prompt_tokens)
                    completion_delta = max(0, out_tok - self.completion_tokens)
                    cached_delta = max(0, cache_tok - self.cached_tokens)
                    reasoning_delta = max(0, reasoning_tok - self.reasoning_tokens)
                    self.prompt_tokens = in_tok
                    self.completion_tokens = out_tok
                    self.cached_tokens = cache_tok
                    self.reasoning_tokens = reasoning_tok
                    self.seq += 1
                    # Map onto the same token_usage shape kimi uses so the
                    # dashboard's existing counters work:
                    #   input_other        = input_delta - cached_delta
                    #   input_cache_read   = cached_delta
                    #   output             = output_delta
                    # Plus a codex-specific reasoning_output_tokens passthrough
                    # (delta). The payload also carries the cumulative totals
                    # for clients that want them directly.
                    input_other_delta = max(0, prompt_delta - cached_delta)
                    out.append(
                        self._wrap(
                            ts,
                            "status",
                            "update",
                            {
                                "token_usage": {
                                    "input_other": input_other_delta,
                                    "input_cache_read": cached_delta,
                                    "input_cache_creation": 0,
                                    "output": completion_delta,
                                    "reasoning_output_tokens": reasoning_delta,
                                },
                                "token_usage_total": {
                                    "input_tokens": in_tok,
                                    "cached_input_tokens": cache_tok,
                                    "output_tokens": out_tok,
                                    "reasoning_output_tokens": reasoning_tok,
                                    "total_tokens": total.get("total_tokens"),
                                },
                                "model_context_window": info.get("model_context_window"),
                                "rate_limits": payload.get("rate_limits"),
                            },
                            {
                                "prompt_tokens": prompt_delta,
                                "completion_tokens": completion_delta,
                                "cached_tokens": cached_delta,
                                "reasoning_tokens": reasoning_delta,
                            },
                            "event_msg/token_count",
                        )
                    )
                    continue
                if ptype in {"agent_message", "user_message", "agent_reasoning"}:
                    # Drop — same content arrives via the corresponding
                    # ``response_item`` (message / reasoning).
                    continue
                if ptype == "patch_apply_end":
                    # Structured patch result that pairs with an earlier
                    # custom_tool_call (apply_patch). Surface it as a hook so
                    # the dashboard can show file_change deltas without
                    # confusing it with the tool_result emitted off
                    # custom_tool_call_output.
                    self.seq += 1
                    out.append(
                        self._wrap(
                            ts,
                            "hook",
                            "completed",
                            {
                                "type": "patch_apply_end",
                                "call_id": payload.get("call_id"),
                                "success": payload.get("success"),
                                "changes": payload.get("changes"),
                                "stdout_excerpt": (payload.get("stdout") or "")[:400],
                                "stderr_excerpt": (payload.get("stderr") or "")[:400],
                            },
                            {},
                            "event_msg/patch_apply_end",
                        )
                    )
                    continue
                # Unknown event_msg subtype: pass through generically as a
                # notification so it shows up in the dashboard's notification
                # lane rather than being silently dropped.
                self.seq += 1
                out.append(
                    self._wrap(
                        ts,
                        "notification",
                        "update",
                        payload,
                        {},
                        f"event_msg/{ptype or 'unknown'}",
                    )
                )
                continue

            if etype != "response_item":
                # Unknown top-level type: skip rather than guess.
                continue

            ptype = payload.get("type")
            if ptype == "message":
                role = payload.get("role") or "assistant"
                text, _ = _extract_message_text(payload.get("content"))
                if not text:
                    continue
                self.seq += 1
                out.append(
                    self._wrap(
                        ts,
                        "message",
                        "completed",
                        {
                            "role": role,
                            "text": text,
                            "id": payload.get("id"),
                        },
                        {},
                        "response_item/message",
                    )
                )
                continue

            if ptype == "reasoning":
                # gpt-5.x with summary=detailed emits natural-language
                # thinking; with summary=auto the array is typically empty
                # and only encrypted_content (Fernet) is present. Surface
                # both signals so the dashboard can show "agent thought for
                # N tokens / M bytes" even without plaintext.
                summary_text = _extract_reasoning_summary(payload)
                encrypted = payload.get("encrypted_content")
                encrypted_bytes = (
                    len(encrypted) if isinstance(encrypted, str) else 0
                )
                self.seq += 1
                out.append(
                    self._wrap(
                        ts,
                        "message",
                        "completed",
                        {
                            "type": "think",
                            "text": summary_text or "",
                            "id": payload.get("id"),
                            "encrypted_bytes": encrypted_bytes,
                        },
                        {"encrypted_bytes": encrypted_bytes},
                        "response_item/reasoning",
                    )
                )
                continue

            if ptype in _TOOL_CALL_TYPES:
                call_id = payload.get("call_id") or payload.get("id") or ""
                # function_call uses ``arguments`` (JSON string); custom_tool_call
                # uses ``input``.
                raw_args = (
                    payload.get("arguments")
                    if ptype == "function_call"
                    else payload.get("input")
                )
                args_dict = _parse_tool_arguments(raw_args)
                tool_name = payload.get("name") or ""
                if isinstance(call_id, str) and call_id and tool_name:
                    self.pending_calls[call_id] = tool_name
                # ``spawn_agent`` is codex 0.122+'s subagent-spawn tool
                # (analogue to claude-code's Task and kimi-cli's Agent).
                # Cache the args so when CodexSessionTail discovers a
                # new sub-rollout file we can render the spawn arrow on
                # the right root step. The actual subagent_spawn event
                # is emitted later (when the *_output supplies the
                # child agent_id), because the child rollout file is
                # what carries the run id we want to link to.
                if (
                    isinstance(call_id, str)
                    and call_id
                    and tool_name in self._SPAWN_TOOL_NAMES
                ):
                    self.spawn_calls[call_id] = {
                        "agent_type": (args_dict.get("agent_type") if isinstance(args_dict, dict) else None),
                        "fork_context": (args_dict.get("fork_context") if isinstance(args_dict, dict) else None),
                        "reasoning_effort": (args_dict.get("reasoning_effort") if isinstance(args_dict, dict) else None),
                        "message": (args_dict.get("message") if isinstance(args_dict, dict) else None),
                        "ts": ts,
                        "step_id": self.step_id,
                    }
                self.seq += 1
                out.append(
                    self._wrap(
                        ts,
                        "tool_call",
                        "completed",
                        {
                            "id": call_id,
                            "function": {
                                "name": tool_name,
                                "arguments": args_dict,
                            },
                            "kind": ptype,
                            "status": payload.get("status"),
                        },
                        {},
                        f"response_item/{ptype}",
                    )
                )
                continue

            if ptype in _TOOL_OUTPUT_TYPES:
                call_id = payload.get("call_id") or ""
                tool_name = self.pending_calls.pop(call_id, "") if isinstance(call_id, str) else ""
                output = payload.get("output")
                # When this output is the reply to a ``spawn_agent`` call,
                # the child agent_id appears inside ``output`` as a JSON
                # blob like ``{"agent_id": "...", "nickname": "..."}``.
                # Decode + cache so CodexSessionTail can match the
                # child rollout file (whose ``session_meta.id`` is the
                # agent_id) back to this spawn call, and synthesize a
                # ``subagent_spawn`` event on the root lane.
                spawn_meta = self.spawn_calls.get(call_id) if isinstance(call_id, str) else None
                if spawn_meta is not None and isinstance(output, str) and output:
                    try:
                        parsed = json.loads(output)
                    except (json.JSONDecodeError, ValueError):
                        parsed = None
                    child_id = (
                        parsed.get("agent_id")
                        if isinstance(parsed, dict)
                        else None
                    )
                    nickname = (
                        parsed.get("nickname")
                        if isinstance(parsed, dict)
                        else None
                    )
                    if isinstance(child_id, str) and child_id and child_id not in self._synthesized_spawns:
                        self.spawn_map[call_id] = child_id
                        self._synthesized_spawns.add(child_id)
                        self.seq += 1
                        out.append(
                            self._wrap(
                                ts,
                                "subagent_spawn",
                                "begin",
                                {
                                    "child_run_id": f"run_sub_{child_id}",
                                    "task_id": child_id,
                                    "subagent_type": spawn_meta.get("agent_type"),
                                    "nickname": nickname,
                                    "description": (
                                        (spawn_meta.get("message") or "")[:200]
                                        if isinstance(spawn_meta.get("message"), str)
                                        else None
                                    ),
                                    "reasoning_effort": spawn_meta.get("reasoning_effort"),
                                    "fork_context": spawn_meta.get("fork_context"),
                                    "launch_mode": "foreground",
                                    "parent_tool_call_id": call_id,
                                },
                                {},
                                "synth:spawn_agent_output",
                            )
                        )
                # codex emits ``output`` as an opaque string for shell tools
                # and as a JSON-encoded blob for some structured tools. Keep
                # it as-is so the dashboard can show the raw text; let
                # downstream consumers parse if they want structured form.
                self.seq += 1
                out.append(
                    self._wrap(
                        ts,
                        "tool_result",
                        "completed",
                        {
                            "tool_call_id": call_id,
                            "tool_name": tool_name,
                            "return_value": {"output": output},
                            "kind": ptype,
                        },
                        {},
                        f"response_item/{ptype}",
                    )
                )
                continue

            if ptype == "web_search_call":
                action = payload.get("action") or {}
                if not isinstance(action, dict):
                    action = {}
                self.seq += 1
                out.append(
                    self._wrap(
                        ts,
                        "tool_call",
                        "completed",
                        {
                            "id": payload.get("id") or "",
                            "function": {
                                "name": "web_search_call",
                                "arguments": {
                                    k: action[k]
                                    for k in ("type", "query", "queries", "url")
                                    if k in action
                                },
                            },
                            "kind": ptype,
                            "status": payload.get("status"),
                        },
                        {},
                        "response_item/web_search_call",
                    )
                )
                continue

            # Unknown response_item subtype: pass through as message so it
            # doesn't disappear from the timeline.
            self.seq += 1
            out.append(
                self._wrap(
                    ts,
                    "message",
                    "completed",
                    payload,
                    {},
                    f"response_item/{ptype or 'unknown'}",
                )
            )
        return out

    def _wrap(
        self,
        ts: float | None,
        kind: str,
        phase: str,
        payload: dict[str, Any],
        metrics: dict[str, Any],
        message_type: str,
    ) -> dict[str, Any]:
        return {
            "event_id": f"{self.run_id}_evt_{self.seq:04d}",
            "seq": self.seq,
            "timestamp": ts,
            "kind": kind,
            "phase": phase,
            "step_id": self.step_id,
            "parent_event_id": None,
            "payload": payload,
            "metrics": metrics,
            "raw_ref": {"source": "codex-rollout", "message_type": message_type},
            "extra": {},
        }


def parse_codex_line(line: str) -> dict[str, Any] | None:
    """Parse one line of a codex ``rollout-*.jsonl``. Return record dict, or
    ``None`` to skip (blank lines, parse errors, or records lacking the
    minimum ``type`` envelope).

    Mirrors ``parse_wire_line`` / ``parse_claude_line`` so the file tail can
    funnel records through a single helper.
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
