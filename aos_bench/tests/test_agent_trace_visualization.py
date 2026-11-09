from __future__ import annotations

import json
from pathlib import Path

from aos_bench.trace import collect_kimi_cli_agent_trace, collect_opencode_agent_trace
from aos_bench.visualization import render_agent_trace_svg, write_agent_trace_svg

from test_kimi_cli_trace import _write_jsonl


def _build_kimi_trace(tmp_path: Path) -> dict:
    session_dir = tmp_path / "session-visual"
    _write_jsonl(
        session_dir / "wire.jsonl",
        [
            {"type": "metadata", "protocol_version": "0.5"},
            {"timestamp": 1.0, "message": {"type": "TurnBegin", "payload": {"user_input": "Analyze three repos"}}},
            {"timestamp": 1.2, "message": {"type": "StepBegin", "payload": {"n": 1}}},
            {
                "timestamp": 1.4,
                "message": {
                    "type": "ToolCall",
                    "payload": {"id": "call_agent_1", "function": {"name": "Agent"}},
                },
            },
            {
                "timestamp": 1.6,
                "message": {
                    "type": "SubagentEvent",
                    "payload": {
                        "parent_tool_call_id": "call_agent_1",
                        "agent_id": "a123",
                        "subagent_type": "explore",
                        "event": {"type": "StepBegin", "payload": {"n": 1}},
                    },
                },
            },
            {
                "timestamp": 1.8,
                "message": {
                    "type": "StatusUpdate",
                    "payload": {
                        "token_usage": {
                            "input_other": 42,
                            "input_cache_read": 0,
                            "input_cache_creation": 0,
                            "output": 13,
                        }
                    },
                },
            },
            {"timestamp": 2.0, "message": {"type": "TurnEnd", "payload": {}}},
        ],
    )

    subagent_dir = session_dir / "subagents" / "a123"
    subagent_dir.mkdir(parents=True, exist_ok=True)
    (subagent_dir / "prompt.txt").write_text("Inspect repo_alpha", encoding="utf-8")
    (subagent_dir / "meta.json").write_text(
        json.dumps(
            {
                "agent_id": "a123",
                "subagent_type": "explore",
                "status": "running_background",
                "description": "repo alpha security",
                "created_at": 1.5,
                "launch_spec": {
                    "agent_id": "a123",
                    "subagent_type": "explore",
                    "effective_model": "kimi-k2.5",
                },
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        subagent_dir / "wire.jsonl",
        [
            {"type": "metadata", "protocol_version": "0.5"},
            {"timestamp": 1.7, "message": {"type": "StepBegin", "payload": {"n": 1}}},
            {
                "timestamp": 1.9,
                "message": {
                    "type": "StatusUpdate",
                    "payload": {"token_usage": {"input_other": 10, "output": 4}},
                },
            },
            {"timestamp": 2.1, "message": {"type": "TurnEnd", "payload": {}}},
        ],
    )
    task_dir = session_dir / "tasks" / "agent-001"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "spec.json").write_text(
        json.dumps(
            {
                "id": "agent-001",
                "kind": "agent",
                "description": "repo alpha security",
                "tool_call_id": "call_agent_1",
                "owner_role": "root",
                "owner_agent_id": "a123",
                "owner_subagent_type": "explore",
                "kind_payload": {
                    "agent_id": "a123",
                    "subagent_type": "explore",
                    "launch_mode": "background",
                },
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "runtime.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "started_at": 1.7,
                "finished_at": 2.1,
                "cpu_time_ms": 55,
                "memory_peak_kb": 2048,
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "output.log").write_text("ok\n", encoding="utf-8")
    return collect_kimi_cli_agent_trace(session_dir, trace_id="trace_visual", model_name="kimi-k2.5")


def test_render_agent_trace_svg_from_kimi_trace(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    trace = _build_kimi_trace(tmp_path)

    svg = render_agent_trace_svg(trace, title="Kimi Agent Trace")

    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert "Kimi Agent Trace" in svg
    assert "Run Graph" in svg
    assert "Timeline" in svg
    assert "run_root_001" in svg
    assert "run_sub_a123" in svg
    assert "spawn explore" in svg
    assert "task agent-001" in svg


def test_write_agent_trace_svg_from_json_path(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    jsonl_path = tmp_path / "opencode.jsonl"
    jsonl_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_created",
                        "sessionID": "sess_root",
                        "parentID": None,
                        "timestamp": 1000,
                        "info": {"title": "root", "agent": "orchestrator"},
                    }
                ),
                json.dumps(
                    {
                        "type": "subagent_spawn",
                        "sessionID": "sess_root",
                        "timestamp": 1100,
                        "tool_call_id": "tool_1",
                        "child_session_id": "sess_child",
                        "input": {"subagent_type": "explore"},
                    }
                ),
                json.dumps(
                    {
                        "type": "session_created",
                        "sessionID": "sess_child",
                        "parentID": "sess_root",
                        "timestamp": 1200,
                        "info": {"title": "child", "agent": "explore"},
                    }
                ),
                json.dumps(
                    {
                        "type": "step_start",
                        "sessionID": "sess_root",
                        "timestamp": 1300,
                        "part": {"id": "part_1"},
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "sessionID": "sess_root",
                        "timestamp": 1800,
                        "part": {"tokens": {"input": 10, "output": 5}, "cost": 0.1},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    trace = collect_opencode_agent_trace(jsonl_path, trace_id="trace_opencode_visual")
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps(trace), encoding="utf-8")

    svg_path = write_agent_trace_svg(trace_path, tmp_path / "trace.svg", title="Opencode Trace")

    svg = svg_path.read_text(encoding="utf-8")
    assert svg_path.exists()
    assert "Opencode Trace" in svg
    assert "run_root_sess_root" in svg
    assert "run_sub_sess_child" in svg
