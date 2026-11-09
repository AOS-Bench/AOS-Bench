"""Incremental version of ``_normalize_wire_records``.

The offline collector at ``aos_bench/trace/kimi_cli.py:243`` builds the full
event list in one pass. For the live monitor we need to feed wire records as
they arrive and produce the same normalized events shape, preserving the
``seq`` / ``step_id`` / token totals across feed calls.

We deliberately mirror the offline collector's behavior so that downstream
consumers (state, errors, frontend) only see one canonical event shape — and
so that the offline AgentTrace produced after the run is byte-identical to
what the live monitor saw during the run.
"""

from __future__ import annotations

import json
from typing import Any


def map_wire_kind(msg_type: str) -> str:
    """Mirror of ``aos_bench.trace.kimi_cli._map_wire_kind``."""
    return {
        "TurnBegin": "turn_begin",
        "TurnEnd": "turn_end",
        "StepBegin": "step_begin",
        "StepInterrupted": "step_end",
        "CompactionBegin": "compaction_begin",
        "CompactionEnd": "compaction_end",
        "StatusUpdate": "status",
        "ToolCall": "tool_call",
        "ToolResult": "tool_result",
        "SubagentEvent": "subagent_event",
        "ApprovalRequest": "approval_request",
        "ApprovalResponse": "approval_response",
        "HookTriggered": "hook",
        "HookResolved": "hook",
        "Notification": "notification",
    }.get(msg_type, "message")


def default_phase(msg_type: str) -> str:
    if msg_type.endswith("Begin"):
        return "begin"
    if msg_type.endswith("End") or msg_type in {"ToolResult", "ApprovalResponse", "HookResolved"}:
        return "completed"
    if msg_type in {"StatusUpdate", "Notification"}:
        return "update"
    return "completed"


def event_metrics(msg_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if msg_type == "StatusUpdate":
        token_usage = payload.get("token_usage") or {}
        return {
            "prompt_tokens": (
                int(token_usage.get("input_other", 0))
                + int(token_usage.get("input_cache_read", 0))
                + int(token_usage.get("input_cache_creation", 0))
            ),
            "completion_tokens": int(token_usage.get("output", 0)),
        }
    if msg_type == "ToolResult":
        return_value = payload.get("return_value")
        if not isinstance(return_value, dict):
            return {}
        extras = return_value.get("extras")
        if not isinstance(extras, dict):
            return {}
        shell_usage = extras.get("shell_resource_usage") or {}
        if isinstance(shell_usage, dict) and shell_usage:
            return {
                "cpu_time_ms": shell_usage.get("cpu_time_ms"),
                "memory_peak_kb": shell_usage.get("memory_peak_kb"),
                "read_bytes": shell_usage.get("read_bytes"),
                "write_bytes": shell_usage.get("write_bytes"),
            }
    return {}


class IncrementalWireParser:
    """Feed wire records one by one (or in batches) and emit normalized events.

    State carried across calls:
    - ``seq``: monotonic event sequence within this run
    - ``step_id``: most recent ``StepBegin`` produced step id
    - token totals: accumulated from ``StatusUpdate`` events
    - ``synthesized_spawns``: ``SubagentEvent`` agent_ids we already synthesized a
      ``subagent_spawn`` event for (so we only emit one per child).
    """

    def __init__(self, *, run_id: str, synthesize_subagent_spawns: bool = False) -> None:
        self.run_id = run_id
        self.synthesize = synthesize_subagent_spawns
        self.seq = 0
        self.step_id: str | None = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tool_call_owners: dict[str, str] = {}
        self.synthesized_spawns: set[str] = set()
        self.first_ts: float | None = None
        self.last_ts: float | None = None

    def feed(self, raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert raw wire records (``{"timestamp": .., "message": ..}``) to
        normalized event dicts. Returns the list of newly produced events; the
        caller is responsible for pushing them into MonitorState.
        """
        out: list[dict[str, Any]] = []
        for record in raw_records:
            ts = record.get("timestamp")
            message = record.get("message") or {}
            msg_type = message.get("type", "unknown")
            payload = message.get("payload") or {}

            if isinstance(ts, (int, float)):
                if self.first_ts is None:
                    self.first_ts = float(ts)
                self.last_ts = float(ts)

            if msg_type == "StepBegin":
                n = payload.get("n")
                self.step_id = f"{self.run_id}_step_{n}"
            elif msg_type == "StatusUpdate":
                tu = payload.get("token_usage") or {}
                self.prompt_tokens += (
                    int(tu.get("input_other", 0))
                    + int(tu.get("input_cache_read", 0))
                    + int(tu.get("input_cache_creation", 0))
                )
                self.completion_tokens += int(tu.get("output", 0))
            elif msg_type == "ToolCall":
                tc_id = payload.get("id")
                if isinstance(tc_id, str) and tc_id:
                    self.tool_call_owners[tc_id] = self.run_id

            if self.synthesize and msg_type == "SubagentEvent":
                agent_id = payload.get("agent_id")
                if isinstance(agent_id, str) and agent_id and agent_id not in self.synthesized_spawns:
                    self.seq += 1
                    out.append(
                        {
                            "event_id": f"{self.run_id}_evt_{self.seq:04d}",
                            "seq": self.seq,
                            "timestamp": ts,
                            "kind": "subagent_spawn",
                            "phase": "begin",
                            "step_id": self.step_id,
                            "parent_event_id": None,
                            "payload": {
                                "child_run_id": f"run_sub_{agent_id}",
                                "subagent_type": payload.get("subagent_type"),
                                "task_id": agent_id,
                                "launch_mode": "foreground",
                            },
                            "metrics": {},
                            "raw_ref": {
                                "source": "kimi-cli-wire",
                                "message_type": msg_type,
                            },
                            "extra": {
                                "parent_tool_call_id": payload.get("parent_tool_call_id"),
                            },
                        }
                    )
                    self.synthesized_spawns.add(agent_id)

            # kimi-cli's orchestrator spawns subagents by calling the
            # ``Agent`` tool — there is no separate SubagentEvent emitted
            # into the parent's wire. Synthesize a ``subagent_spawn`` event
            # off the Agent ToolCall so the dashboard can render the spawn
            # marker on the root lane. Each ToolCall id only spawns once.
            if self.synthesize and msg_type == "ToolCall":
                fn_name = (payload.get("function") or {}).get("name")
                tc_id = payload.get("id")
                if (
                    fn_name == "Agent"
                    and isinstance(tc_id, str)
                    and tc_id
                    and tc_id not in self.synthesized_spawns
                ):
                    self.seq += 1
                    raw_args = (payload.get("function") or {}).get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                    subagent_type = None
                    description = None
                    if isinstance(args, dict):
                        subagent_type = args.get("subagent_type") or args.get("type")
                        description = args.get("description") or args.get("prompt")
                    out.append(
                        {
                            "event_id": f"{self.run_id}_evt_{self.seq:04d}",
                            "seq": self.seq,
                            "timestamp": ts,
                            "kind": "subagent_spawn",
                            "phase": "begin",
                            "step_id": self.step_id,
                            "parent_event_id": None,
                            "payload": {
                                "child_run_id": None,  # unknown until subagent dir appears
                                "subagent_type": subagent_type,
                                "task_id": None,
                                "description": (description or "")[:200] if isinstance(description, str) else None,
                                "launch_mode": "foreground",
                                "parent_tool_call_id": tc_id,
                            },
                            "metrics": {},
                            "raw_ref": {
                                "source": "kimi-cli-wire",
                                "message_type": "ToolCall:Agent",
                            },
                            "extra": {"parent_tool_call_id": tc_id},
                        }
                    )
                    self.synthesized_spawns.add(tc_id)

            self.seq += 1
            out.append(
                {
                    "event_id": f"{self.run_id}_evt_{self.seq:04d}",
                    "seq": self.seq,
                    "timestamp": ts,
                    "kind": map_wire_kind(msg_type),
                    "phase": default_phase(msg_type),
                    "step_id": self.step_id,
                    "parent_event_id": None,
                    "payload": payload,
                    "metrics": event_metrics(msg_type, payload),
                    "raw_ref": {
                        "source": "kimi-cli-wire",
                        "message_type": msg_type,
                    },
                    "extra": {},
                }
            )
        return out


def parse_wire_line(line: str) -> dict[str, Any] | None:
    """Parse one line of wire.jsonl. Return record dict, or None to skip.

    Returns None for blank lines, metadata header lines, and parse errors. The
    caller decides what to do — for live tail we just skip and continue.
    """
    line = line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    if parsed.get("type") == "metadata":
        return None
    if "timestamp" in parsed and "message" in parsed:
        return parsed
    return None
