"""Unit tests for the codex CLI rollout-jsonl parser."""

from __future__ import annotations

import json
from pathlib import Path

from aos_bench.monitor.tail.codex_parser import (
    IncrementalCodexParser,
    parse_codex_line,
)


# ── parse_codex_line ───────────────────────────────────────────────────


def test_parse_codex_line_drops_blank_and_garbage() -> None:
    assert parse_codex_line("") is None
    assert parse_codex_line("   ") is None
    assert parse_codex_line("not json") is None
    # missing ``type`` envelope → drop (defensive against header lines)
    assert parse_codex_line(json.dumps({"foo": 1})) is None


def test_parse_codex_line_accepts_minimal() -> None:
    rec = parse_codex_line(json.dumps({"type": "session_meta", "payload": {}}))
    assert rec is not None
    assert rec["type"] == "session_meta"


# ── shape: synthesized step_begin per turn_context ─────────────────────


def test_turn_context_synthesizes_step_begin() -> None:
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "turn_context",
                "timestamp": "2026-06-23T07:34:00.911Z",
                "payload": {"model": "gpt-5.4-mini", "effort": "high", "summary": "detailed"},
            }
        ]
    )
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "step_begin"
    assert e["step_id"] == "run_root_001_step_1"
    assert e["payload"]["model"] == "gpt-5.4-mini"
    assert e["payload"]["reasoning_effort"] == "high"
    assert e["payload"]["reasoning_summary"] == "detailed"
    assert parser.default_model == "gpt-5.4-mini"


def test_session_meta_caches_metadata_without_emitting() -> None:
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "session_meta",
                "timestamp": "2026-06-23T07:34:00.530Z",
                "payload": {
                    "session_id": "abc-123",
                    "cli_version": "0.142.0",
                    "originator": "codex_exec",
                },
            }
        ]
    )
    assert events == []  # session_meta is consumed silently
    assert parser.session_id == "abc-123"
    assert parser.cli_version == "0.142.0"
    assert parser.originator == "codex_exec"


# ── token_count delta semantics ────────────────────────────────────────


def test_token_count_emits_deltas_not_cumulative() -> None:
    """codex's token_count.info.total_token_usage is CUMULATIVE on every event;
    the dashboard sums per-event metrics, so the parser must emit deltas to
    avoid double-counting on multi-turn runs."""
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "event_msg",
                "timestamp": "2026-06-23T07:34:01.0Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 0,
                            "output_tokens": 10,
                            "reasoning_output_tokens": 5,
                        },
                    },
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-06-23T07:34:05.0Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 250,
                            "cached_input_tokens": 100,
                            "output_tokens": 30,
                            "reasoning_output_tokens": 15,
                        },
                    },
                },
            },
        ]
    )
    assert len(events) == 2
    first, second = events
    # First event = baseline (delta == cumulative since prior was 0).
    assert first["metrics"]["prompt_tokens"] == 100
    assert first["metrics"]["completion_tokens"] == 10
    assert first["metrics"]["reasoning_tokens"] == 5
    # Second event = delta only: 250-100 prompt, 30-10 completion.
    assert second["metrics"]["prompt_tokens"] == 150
    assert second["metrics"]["completion_tokens"] == 20
    assert second["metrics"]["cached_tokens"] == 100  # 100 - 0
    assert second["metrics"]["reasoning_tokens"] == 10  # 15 - 5
    # Cumulative totals also surface on the payload for clients that need them.
    assert second["payload"]["token_usage_total"]["input_tokens"] == 250
    # Parser running totals carry forward.
    assert parser.prompt_tokens == 250
    assert parser.completion_tokens == 30
    assert parser.cached_tokens == 100
    assert parser.reasoning_tokens == 15


# ── tool_call / tool_result pairing ────────────────────────────────────


def test_function_call_args_decoded_from_json_string() -> None:
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "response_item",
                "timestamp": "2026-06-23T07:34:00.0Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "call_abc",
                    "name": "shell",
                    "arguments": json.dumps({"command": ["bash", "-c", "echo hi"]}),
                    "status": "completed",
                },
            }
        ]
    )
    assert len(events) == 1
    p = events[0]["payload"]
    assert events[0]["kind"] == "tool_call"
    assert p["function"]["name"] == "shell"
    # arguments string decoded into a dict
    assert p["function"]["arguments"] == {"command": ["bash", "-c", "echo hi"]}
    assert p["id"] == "call_abc"


def test_custom_tool_call_uses_input_field() -> None:
    """``apply_patch`` ships as a custom_tool_call whose body is in
    ``input`` (a raw patch envelope, not necessarily JSON)."""
    parser = IncrementalCodexParser(run_id="run_root_001")
    raw_patch = "*** Begin Patch\n*** Add File: /app/x\n+hi\n*** End Patch\n"
    events = parser.feed(
        [
            {
                "type": "response_item",
                "timestamp": "2026-06-23T07:34:00.0Z",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call_xyz",
                    "name": "apply_patch",
                    "input": raw_patch,
                },
            }
        ]
    )
    p = events[0]["payload"]
    assert events[0]["kind"] == "tool_call"
    assert p["function"]["name"] == "apply_patch"
    # Non-JSON input wraps as {"input": <raw>} for downstream consumers.
    assert p["function"]["arguments"] == {"input": raw_patch}


def test_tool_call_output_pairs_to_call_id() -> None:
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "response_item",
                "timestamp": "2026-06-23T07:34:00.0Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "call_42",
                    "name": "shell",
                    "arguments": "{}",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-06-23T07:34:00.5Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_42",
                    "output": "exit 0",
                },
            },
        ]
    )
    assert events[0]["kind"] == "tool_call"
    assert events[1]["kind"] == "tool_result"
    # Tool name was carried over from the matching call.
    assert events[1]["payload"]["tool_name"] == "shell"
    assert events[1]["payload"]["return_value"]["output"] == "exit 0"


# ── reasoning ──────────────────────────────────────────────────────────


def test_reasoning_summary_text_extracted_from_dict_blocks() -> None:
    """codex 0.122+ wraps each summary item as
    ``{"type": "summary_text", "text": "..."}``. Parser must extract the
    ``text`` field (older builds emitted bare strings — both shapes work)."""
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "response_item",
                "timestamp": "2026-06-23T07:34:00.0Z",
                "payload": {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "Step 1"},
                        {"type": "summary_text", "text": "Step 2"},
                    ],
                    "encrypted_content": "gAAAAA" + "x" * 100,
                },
            }
        ]
    )
    assert len(events) == 1
    p = events[0]["payload"]
    assert events[0]["kind"] == "message"
    assert p["type"] == "think"
    assert p["text"] == "Step 1\nStep 2"
    # encrypted_bytes surfaces both on payload and metrics so dashboards
    # can render "thinking happened" even when text is empty (auto mode).
    assert p["encrypted_bytes"] == 106
    assert events[0]["metrics"]["encrypted_bytes"] == 106


def test_reasoning_empty_summary_still_emits_with_encrypted_bytes() -> None:
    """summary=auto + many models → ``summary[]`` is empty but
    encrypted_content is present. We still emit a think event so the
    dashboard can show "agent thought for N tokens / M bytes"."""
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "response_item",
                "timestamp": "2026-06-23T07:34:00.0Z",
                "payload": {
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "gAAAAA" + "y" * 50,
                },
            }
        ]
    )
    assert len(events) == 1
    assert events[0]["payload"]["text"] == ""
    assert events[0]["payload"]["encrypted_bytes"] == 56


# ── duplicate-suppression for event_msg/agent|user|reasoning_message ──


def test_event_msg_agent_message_dropped() -> None:
    """``event_msg/agent_message`` and ``event_msg/user_message`` carry the
    same content as a co-emitted ``response_item/message`` and would
    double-count if both reached the dashboard."""
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "response_item",
                "timestamp": "2026-06-23T07:34:00.0Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello"}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-06-23T07:34:00.1Z",
                "payload": {"type": "agent_message", "message": "Hello"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-06-23T07:34:00.2Z",
                "payload": {"type": "agent_reasoning", "text": "..."},
            },
        ]
    )
    # Only the response_item/message survives.
    assert len(events) == 1
    assert events[0]["kind"] == "message"
    assert events[0]["payload"]["text"] == "Hello"


# ── task lifecycle (turn_begin / turn_end) ─────────────────────────────


def test_task_complete_carries_duration_and_ttft() -> None:
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "event_msg",
                "timestamp": "2026-06-23T07:34:00.0Z",
                "payload": {"type": "task_started", "turn_id": "t1"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-06-23T07:34:11.0Z",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "t1",
                    "duration_ms": 11000,
                    "time_to_first_token_ms": 2100,
                    "last_agent_message": "done",
                },
            },
        ]
    )
    assert events[0]["kind"] == "turn_begin"
    assert events[1]["kind"] == "turn_end"
    # Latency telemetry surfaces on metrics so the dashboard can show TTFT.
    assert events[1]["metrics"]["duration_ms"] == 11000
    assert events[1]["metrics"]["time_to_first_token_ms"] == 2100


# ── full smoke fixture ────────────────────────────────────────────────


_SMOKE_FIXTURE = (
    str(Path.home() / "aos-bench-results/smoke/codex0142-detailed-052501/hello-world__kCHACkn/agent/sessions")
)


def test_full_smoke_rollout_normalizes_cleanly() -> None:
    """Replay an actual codex 0.142 + gpt-5.4-mini run end-to-end through the
    parser. Skip if the cloud-mounted fixture isn't reachable (e.g. on CI)."""
    fixture_root = Path(_SMOKE_FIXTURE)
    if not fixture_root.exists():
        import pytest

        pytest.skip(f"smoke fixture not present at {_SMOKE_FIXTURE}")
    rollouts = list(fixture_root.glob("*/*/*/rollout-*.jsonl"))
    assert rollouts, f"no rollout-*.jsonl under {fixture_root}"
    parser = IncrementalCodexParser(run_id="run_root_001")
    records = []
    for line in rollouts[0].read_text().splitlines():
        rec = parse_codex_line(line)
        if rec is not None:
            records.append(rec)
    events = parser.feed(records)
    # Sanity checks against the known smoke result (reward 1.0, 1 tool_call,
    # 1 turn, gpt-5.4-mini).
    assert parser.default_model == "gpt-5.4-mini"
    assert parser.cli_version == "0.142.0"
    assert any(e["kind"] == "step_begin" for e in events)
    assert any(e["kind"] == "tool_call" for e in events)
    assert any(e["kind"] == "tool_result" for e in events)
    assert any(e["kind"] == "turn_end" for e in events)
    # Token totals are the cumulative final values.
    assert parser.prompt_tokens > 0
    assert parser.completion_tokens > 0
    # Reasoning summary made it through (gpt-5.4-mini + summary=detailed).
    think_events = [e for e in events if e["payload"].get("type") == "think"]
    assert think_events
    assert any(e["payload"]["text"] for e in think_events)


# ── spawn_agent → subagent_spawn synthesis ────────────────────────────


def test_spawn_agent_call_caches_args_but_does_not_emit_yet() -> None:
    """spawn_agent's tool_call arrives BEFORE the output that supplies
    the child agent_id. Parser should cache the args (so spawn_map
    fills in once the *_output arrives) and emit only the normal
    tool_call event at this point, NOT a subagent_spawn (we don't know
    the child id yet)."""
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "response_item",
                "timestamp": "2026-06-24T07:34:00.0Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "call_spawn1",
                    "name": "spawn_agent",
                    "arguments": json.dumps(
                        {
                            "agent_type": "worker",
                            "fork_context": False,
                            "reasoning_effort": "medium",
                            "message": "do task X",
                        }
                    ),
                    "status": "completed",
                },
            }
        ]
    )
    # tool_call emitted normally; spawn synthesis deferred until output
    assert [e["kind"] for e in events] == ["tool_call"]
    assert "call_spawn1" in parser.spawn_calls
    assert parser.spawn_map == {}  # no child id yet


def test_spawn_agent_output_synthesizes_subagent_spawn_event() -> None:
    """When the spawn_agent's function_call_output arrives, parser must:
    1. parse the JSON output for ``agent_id`` and ``nickname``
    2. emit a ``subagent_spawn`` event keyed to the child run id
    3. update ``spawn_map`` (consumed by the tail to wire the child run)
    4. STILL emit the normal tool_result event after the spawn event"""
    parser = IncrementalCodexParser(run_id="run_root_001")
    events = parser.feed(
        [
            {
                "type": "response_item",
                "timestamp": "2026-06-24T07:34:00.0Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "call_spawn1",
                    "name": "spawn_agent",
                    "arguments": json.dumps(
                        {
                            "agent_type": "worker",
                            "fork_context": False,
                            "reasoning_effort": "medium",
                            "message": "work on task tune-mjcf",
                        }
                    ),
                    "status": "completed",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-06-24T07:34:00.1Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_spawn1",
                    "output": json.dumps({"agent_id": "child-abc-123", "nickname": "Linnaeus"}),
                },
            },
        ]
    )
    kinds = [e["kind"] for e in events]
    # Order: tool_call (the spawn invocation), then subagent_spawn (synthesized
    # from the output), then tool_result (the raw output).
    assert kinds == ["tool_call", "subagent_spawn", "tool_result"]

    spawn = events[1]
    assert spawn["payload"]["child_run_id"] == "run_sub_child-abc-123"
    assert spawn["payload"]["task_id"] == "child-abc-123"
    assert spawn["payload"]["nickname"] == "Linnaeus"
    assert spawn["payload"]["subagent_type"] == "worker"
    assert "work on task tune-mjcf" in spawn["payload"]["description"]
    assert spawn["payload"]["parent_tool_call_id"] == "call_spawn1"

    # spawn_map is what CodexSessionTail uses to wire child rollouts back
    # to their parent run.
    assert parser.spawn_map == {"call_spawn1": "child-abc-123"}


def test_spawn_event_emitted_only_once_per_child() -> None:
    """Defensive: if the same spawn_agent output appears twice (e.g.
    parser fed twice with overlapping records during a poll), the
    subagent_spawn event must not duplicate."""
    parser = IncrementalCodexParser(run_id="run_root_001")
    seq = [
        {
            "type": "response_item",
            "timestamp": "2026-06-24T07:34:00.0Z",
            "payload": {
                "type": "function_call",
                "call_id": "call_spawn1",
                "name": "spawn_agent",
                "arguments": "{}",
                "status": "completed",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-06-24T07:34:00.1Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_spawn1",
                "output": json.dumps({"agent_id": "child-1"}),
            },
        },
    ]
    parser.feed(seq)
    # Same records replayed (simulating a tail glitch): no second spawn event.
    second = parser.feed(seq)
    assert not any(e["kind"] == "subagent_spawn" for e in second)


# ── session_meta captures parent/role linkage for subagent rollouts ──


def test_subagent_session_meta_captures_role_and_parent_thread_id() -> None:
    """A sub rollout's session_meta carries ``agent_role="worker"``,
    ``parent_thread_id`` pointing at the root, and its own ``id`` ==
    the agent_id the root saw on its spawn_agent output. The tail
    relies on these fields to wire the run."""
    parser = IncrementalCodexParser(run_id="run_sub_x")
    parser.feed(
        [
            {
                "type": "session_meta",
                "timestamp": "2026-06-24T07:34:00.0Z",
                "payload": {
                    "session_id": "root-sess-uuid",
                    "id": "child-sess-uuid",
                    "parent_thread_id": "root-sess-uuid",
                    "agent_role": "worker",
                    "agent_nickname": "Linnaeus",
                    "cli_version": "0.142.0",
                    "originator": "codex_exec",
                },
            }
        ]
    )
    assert parser.session_id == "root-sess-uuid"
    assert parser.thread_id == "child-sess-uuid"
    assert parser.parent_thread_id == "root-sess-uuid"
    assert parser.agent_role == "worker"
    assert parser.agent_nickname == "Linnaeus"


def test_root_session_meta_leaves_agent_role_none() -> None:
    """Root rollouts emit session_meta WITHOUT ``agent_role`` /
    ``parent_thread_id``. Parser should leave those fields as None — the
    tail uses ``agent_role is None`` to decide root-vs-sub."""
    parser = IncrementalCodexParser(run_id="run_root_001")
    parser.feed(
        [
            {
                "type": "session_meta",
                "timestamp": "2026-06-24T07:34:00.0Z",
                "payload": {
                    "session_id": "root-sess-uuid",
                    "id": "root-sess-uuid",
                    "cli_version": "0.142.0",
                    "originator": "codex_exec",
                    "thread_source": "user",
                },
            }
        ]
    )
    assert parser.agent_role is None
    assert parser.parent_thread_id is None
    assert parser.thread_id == "root-sess-uuid"

