from __future__ import annotations

import json
import os
from pathlib import Path

from aos_bench.trace.batch_convert import (
    convert_trial,
    detect_agent_kind,
    find_trial_dirs,
)
from aos_bench.trace.claude_code import collect_claude_code_agent_trace
from aos_bench.trace.codex import collect_codex_agent_trace


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def test_claude_code_native_trace_preserves_sidechain(tmp_path: Path) -> None:
    trial = tmp_path / "trial"
    transcript = trial / "agent" / "sessions" / "projects" / "-workspace" / "root.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "uuid": "root-1",
                "type": "assistant",
                "timestamp": "2026-08-10T00:00:00Z",
                "isSidechain": False,
                "message": {
                    "id": "msg-root",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-task",
                            "name": "Task",
                            "input": {"description": "inspect files"},
                        }
                    ],
                },
            },
            {
                "uuid": "sub-1",
                "type": "assistant",
                "timestamp": "2026-08-10T00:00:01Z",
                "isSidechain": True,
                "agentId": "agent-abc",
                "message": {
                    "id": "msg-sub",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 4, "output_tokens": 3},
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ],
    )

    assert detect_agent_kind(trial / "agent") == "claude-code"
    trace = collect_claude_code_agent_trace(trial, trace_id="trace-test")
    assert trace["schema_version"] == "AgentTrace-v0.1"
    assert trace["summary"]["subagent_count_total"] == 1
    runs = {run["run_id"]: run for run in trace["runs"]}
    child = runs["run_sub_agent-abc"]
    assert child["parent_run_id"] == "run_root_001"
    assert child["source_event_id"] is not None
    spawn = next(
        event
        for event in runs["run_root_001"]["events"]
        if event["kind"] == "subagent_spawn"
    )
    assert spawn["payload"]["child_run_id"] == child["run_id"]
    ok, message = convert_trial(trial, render_svg=True)
    assert ok, message
    assert (trial / "agent" / "agent_trace.json").is_file()
    assert (trial / "agent" / "agent_trace.svg").is_file()


def test_codex_native_trace_preserves_full_thread_ids_and_nested_parent(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    day = trial / "agent" / "sessions" / "2026" / "08" / "10"
    root_id = "019fd5a5-7b36-71f0-9ffd-7868db88eee2"
    child_id = "019fd5a7-92f8-74a3-8228-387d6714264b"
    root = day / f"rollout-root-{root_id}.jsonl"
    child = day / f"rollout-child-{child_id}.jsonl"
    _write_jsonl(
        root,
        [
            {
                "timestamp": "2026-08-10T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": root_id, "cli_version": "0.145.0"},
            },
            {
                "timestamp": "2026-08-10T00:00:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.5"},
            },
            {
                "timestamp": "2026-08-10T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "spawn-1",
                    "name": "spawn_agent",
                    "arguments": json.dumps({"message": "work"}),
                },
            },
            {
                "timestamp": "2026-08-10T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "spawn-1",
                    "output": json.dumps({"agent_id": child_id, "nickname": "Curie"}),
                },
            },
        ],
    )
    _write_jsonl(
        child,
        [
            {
                "timestamp": "2026-08-10T00:00:03Z",
                "type": "session_meta",
                "payload": {
                    "id": child_id,
                    "parent_thread_id": root_id,
                    "agent_nickname": "Curie",
                    "cli_version": "0.145.0",
                },
            },
            {
                "timestamp": "2026-08-10T00:00:03Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.5"},
            },
        ],
    )
    os.utime(root, (1, 1))
    os.utime(child, (2, 2))

    assert detect_agent_kind(trial / "agent") == "codex"
    trace = collect_codex_agent_trace(trial, trace_id="trace-test")
    runs = {run["run_id"]: run for run in trace["runs"]}
    child_run_id = f"run_sub_{child_id}"
    assert child_run_id in runs
    assert runs[child_run_id]["parent_run_id"] == "run_root_001"
    assert runs[child_run_id]["source_event_id"] is not None
    assert trace["agent_system"]["version"] == "0.145.0"


def test_batch_discovery_handles_v1_deep_job_nesting(tmp_path: Path) -> None:
    trial = tmp_path / "jobs" / "claude-opus48" / "stream" / "job" / "trial"
    (trial / "agent").mkdir(parents=True)
    assert find_trial_dirs(tmp_path) == [trial]
