from __future__ import annotations

import json
from pathlib import Path

from aos_bench.trace import collect_kimi_cli_agent_trace


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_collect_kimi_cli_agent_trace(tmp_path: Path) -> None:
    session_dir = tmp_path / "session-1"

    _write_jsonl(
        session_dir / "wire.jsonl",
        [
            {"type": "metadata", "protocol_version": "0.5"},
            {
                "timestamp": 1.0,
                "message": {
                    "type": "TurnBegin",
                    "payload": {"user_input": "Fix the bug and run tests."},
                },
            },
            {
                "timestamp": 2.0,
                "message": {
                    "type": "StepBegin",
                    "payload": {"n": 1},
                },
            },
            {
                "timestamp": 3.0,
                "message": {
                    "type": "ToolCall",
                    "payload": {
                        "id": "call_agent_1",
                        "function": {"name": "Agent"},
                    },
                },
            },
            {
                "timestamp": 4.0,
                "message": {
                    "type": "SubagentEvent",
                    "payload": {
                        "parent_tool_call_id": "call_agent_1",
                        "agent_id": "a123",
                        "subagent_type": "explore",
                        "event": {
                            "type": "StepBegin",
                            "payload": {"n": 1},
                        },
                    },
                },
            },
            {
                "timestamp": 5.0,
                "message": {
                    "type": "StatusUpdate",
                    "payload": {
                        "token_usage": {
                            "input_other": 10,
                            "input_cache_read": 2,
                            "input_cache_creation": 3,
                            "output": 5,
                        }
                    },
                },
            },
            {
                "timestamp": 5.4,
                "message": {
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "call_shell_1",
                        "return_value": {
                            "is_error": False,
                            "output": "ok",
                            "message": "Command executed successfully.",
                            "display": [],
                            "extras": {
                                "shell_resource_usage": {
                                    "resource_scope": "cgroup",
                                    "cgroup_path": "/sys/fs/cgroup/demo/task-fg-shell-call_shell_1",
                                    "cpu_time_ms": 120,
                                    "memory_current_kb": 1024,
                                    "memory_peak_kb": 2048,
                                    "read_bytes": 512,
                                    "write_bytes": 256,
                                }
                            },
                        },
                    },
                },
            },
            {
                "timestamp": 6.0,
                "message": {
                    "type": "TurnEnd",
                    "payload": {},
                },
            },
        ],
    )

    subagent_dir = session_dir / "subagents" / "a123"
    subagent_dir.mkdir(parents=True, exist_ok=True)
    (subagent_dir / "prompt.txt").write_text("Search auth middleware", encoding="utf-8")
    (subagent_dir / "meta.json").write_text(
        json.dumps(
            {
                "agent_id": "a123",
                "subagent_type": "explore",
                "status": "completed",
                "description": "search auth code",
                "created_at": 1.5,
                "updated_at": 8.0,
                "last_task_id": None,
                "launch_spec": {
                    "agent_id": "a123",
                    "subagent_type": "explore",
                    "model_override": None,
                    "effective_model": "kimi-k2.5",
                    "created_at": 1.5,
                },
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        subagent_dir / "wire.jsonl",
        [
            {"type": "metadata", "protocol_version": "0.5"},
            {
                "timestamp": 4.0,
                "message": {
                    "type": "StepBegin",
                    "payload": {"n": 1},
                },
            },
            {
                "timestamp": 4.5,
                "message": {
                    "type": "StatusUpdate",
                    "payload": {
                        "token_usage": {
                            "input_other": 7,
                            "input_cache_read": 0,
                            "input_cache_creation": 0,
                            "output": 4,
                        }
                    },
                },
            },
            {
                "timestamp": 5.5,
                "message": {
                    "type": "TurnEnd",
                    "payload": {},
                },
            },
        ],
    )

    task_dir = session_dir / "tasks" / "agent-001"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "spec.json").write_text(
        json.dumps(
            {
                "id": "agent-001",
                "kind": "agent",
                "session_id": "session-1",
                "description": "search auth code",
                "tool_call_id": "call_agent_1",
                "owner_role": "root",
                "kind_payload": {
                    "agent_id": "a123",
                    "subagent_type": "explore",
                },
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "runtime.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "worker_pid": 1001,
                "child_pid": 1002,
                "started_at": 4.0,
                "updated_at": 6.0,
                "finished_at": 6.0,
                "exit_code": 0,
                "timed_out": False,
                "cpu_time_ms": 250,
                "memory_peak_kb": 4096,
                "read_bytes": 1024,
                "write_bytes": 512,
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "output.log").write_text("done\n", encoding="utf-8")

    trace = collect_kimi_cli_agent_trace(session_dir, trace_id="trace_test", model_name="kimi-k2.5")

    assert trace["trace_id"] == "trace_test"
    assert trace["agent_system"]["name"] == "kimi-cli"
    assert trace["root_run_id"] == "run_root_001"
    assert len(trace["runs"]) == 2

    root_run = trace["runs"][0]
    sub_run = trace["runs"][1]

    assert root_run["session_mode"] == "same_session"
    assert root_run["status"] == "completed"
    assert root_run["resource_usage"]["prompt_tokens"] == 15
    assert root_run["resource_usage"]["completion_tokens"] == 5
    assert root_run["resource_usage"]["cpu_time_ms"] == 120
    assert root_run["resource_usage"]["memory_current_kb"] == 1024
    assert root_run["resource_usage"]["memory_peak_kb"] == 2048
    assert root_run["resource_usage"]["read_bytes"] == 512
    assert root_run["resource_usage"]["write_bytes"] == 256
    assert any(event["kind"] == "subagent_spawn" for event in root_run["events"])
    tool_results = [event for event in root_run["events"] if event["kind"] == "tool_result"]
    assert any((event.get("metrics") or {}).get("cpu_time_ms") == 120 for event in tool_results)

    assert sub_run["run_id"] == "run_sub_a123"
    assert sub_run["parent_run_id"] == "run_root_001"
    assert sub_run["session_mode"] == "same_session"
    assert sub_run["agent_name"] == "explore"
    assert sub_run["task"]["prompt"] == "Search auth middleware"
    assert sub_run["resource_usage"]["prompt_tokens"] == 7
    assert sub_run["resource_usage"]["completion_tokens"] == 4
    bg_events = [event for event in sub_run["events"] if event["kind"] == "background_task"]
    assert bg_events
    assert bg_events[0]["metrics"]["cpu_time_ms"] == 250
    assert bg_events[0]["metrics"]["memory_peak_kb"] == 4096


def test_root_background_bash_task_does_not_get_misattributed_on_tool_id_collision(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session-collision"

    _write_jsonl(
        session_dir / "wire.jsonl",
        [
            {"type": "metadata", "protocol_version": "0.5"},
            {
                "timestamp": 1.0,
                "message": {
                    "type": "ToolCall",
                    "payload": {
                        "id": "functions.Shell:0",
                        "function": {"name": "Shell"},
                    },
                },
            },
            {
                "timestamp": 2.0,
                "message": {
                    "type": "ToolCall",
                    "payload": {
                        "id": "functions.Agent:1",
                        "function": {"name": "Agent"},
                    },
                },
            },
            {
                "timestamp": 2.1,
                "message": {
                    "type": "SubagentEvent",
                    "payload": {
                        "parent_tool_call_id": "functions.Agent:1",
                        "agent_id": "a-sub",
                        "subagent_type": "explore",
                        "event": {"type": "TurnBegin", "payload": {"user_input": "inspect"}},
                    },
                },
            },
        ],
    )

    subagent_dir = session_dir / "subagents" / "a-sub"
    subagent_dir.mkdir(parents=True, exist_ok=True)
    (subagent_dir / "prompt.txt").write_text("inspect", encoding="utf-8")
    (subagent_dir / "meta.json").write_text(
        json.dumps(
            {
                "agent_id": "a-sub",
                "subagent_type": "explore",
                "status": "completed",
                "description": "inspect",
                "created_at": 2.1,
                "updated_at": 3.0,
                "last_task_id": None,
                "launch_spec": {
                    "agent_id": "a-sub",
                    "subagent_type": "explore",
                    "model_override": None,
                    "effective_model": "kimi-k2.5",
                    "created_at": 2.1,
                },
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        subagent_dir / "wire.jsonl",
        [
            {"type": "metadata", "protocol_version": "0.5"},
            {
                "timestamp": 2.2,
                "message": {
                    "type": "ToolCall",
                    "payload": {
                        "id": "functions.Shell:0",
                        "function": {"name": "Shell"},
                    },
                },
            },
        ],
    )

    task_dir = session_dir / "tasks" / "bash-001"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "spec.json").write_text(
        json.dumps(
            {
                "id": "bash-001",
                "kind": "bash",
                "session_id": "session-collision",
                "description": "root background shell",
                "tool_call_id": "functions.Shell:0",
                "owner_role": "root",
                "owner_agent_id": None,
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "runtime.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "worker_pid": 2001,
                "child_pid": 2002,
                "child_pgid": 2002,
                "started_at": 1.0,
                "updated_at": 2.0,
                "finished_at": 2.0,
                "exit_code": 0,
                "timed_out": False,
                "resource_scope": "process_group",
                "cpu_time_ms": 900,
                "memory_peak_kb": 2048,
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "output.log").write_text("done\n", encoding="utf-8")

    trace = collect_kimi_cli_agent_trace(
        session_dir,
        trace_id="trace_collision",
        model_name="kimi-k2.5",
    )
    root_run = trace["runs"][0]
    sub_run = trace["runs"][1]

    root_bg_events = [event for event in root_run["events"] if event["kind"] == "background_task"]
    sub_bg_events = [event for event in sub_run["events"] if event["kind"] == "background_task"]

    assert len(root_bg_events) == 1
    assert not sub_bg_events
    assert root_run["resource_usage"]["cpu_time_ms"] == 900
