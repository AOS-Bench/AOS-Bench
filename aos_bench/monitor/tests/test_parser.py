from __future__ import annotations

from aos_bench.monitor.tail.parser import IncrementalWireParser, parse_wire_line


def _rec(ts: float, msg_type: str, payload: dict | None = None) -> dict:
    return {"timestamp": ts, "message": {"type": msg_type, "payload": payload or {}}}


def test_incremental_parser_basic() -> None:
    p = IncrementalWireParser(run_id="run_root_001")
    events = p.feed(
        [
            _rec(1.0, "TurnBegin", {"user_input": "go"}),
            _rec(2.0, "StepBegin", {"n": 1}),
            _rec(3.0, "ToolCall", {"id": "c1", "function": {"name": "Shell"}}),
            _rec(4.0, "ToolResult", {"id": "c1", "return_value": "done"}),
            _rec(5.0, "StatusUpdate", {"token_usage": {"input_other": 100, "output": 25}}),
        ]
    )
    kinds = [e["kind"] for e in events]
    assert kinds == ["turn_begin", "step_begin", "tool_call", "tool_result", "status"]
    # step_id is assigned to step_begin and following events
    assert events[1]["step_id"] == "run_root_001_step_1"
    assert events[3]["step_id"] == "run_root_001_step_1"
    # seq is monotonic
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    # Token totals accumulated
    assert p.prompt_tokens == 100
    assert p.completion_tokens == 25
    # Tool call ownership tracked
    assert p.tool_call_owners["c1"] == "run_root_001"


def test_subagent_spawn_synthesis_once_per_agent() -> None:
    p = IncrementalWireParser(run_id="run_root_001", synthesize_subagent_spawns=True)
    events = p.feed(
        [
            _rec(1.0, "SubagentEvent", {"agent_id": "a1", "subagent_type": "coder"}),
            _rec(2.0, "SubagentEvent", {"agent_id": "a1", "subagent_type": "coder"}),
            _rec(3.0, "SubagentEvent", {"agent_id": "a2", "subagent_type": "coder"}),
        ]
    )
    spawns = [e for e in events if e["kind"] == "subagent_spawn"]
    # Two spawns total (one for a1, one for a2), but three subagent_event passthroughs.
    assert len(spawns) == 2
    spawned_ids = {e["payload"]["task_id"] for e in spawns}
    assert spawned_ids == {"a1", "a2"}
    passthroughs = [e for e in events if e["kind"] == "subagent_event"]
    assert len(passthroughs) == 3


def test_incremental_seq_continuity_across_feed_calls() -> None:
    p = IncrementalWireParser(run_id="run_root_001")
    first = p.feed([_rec(1.0, "TurnBegin", {})])
    second = p.feed([_rec(2.0, "StepBegin", {"n": 1})])
    assert first[0]["seq"] == 1
    assert second[0]["seq"] == 2


def test_parse_wire_line_skips_blank_and_metadata() -> None:
    assert parse_wire_line("") is None
    assert parse_wire_line('{"type": "metadata", "protocol_version": "0.5"}') is None
    assert parse_wire_line('{"foo": 1}') is None  # missing timestamp+message
    rec = parse_wire_line('{"timestamp": 1.0, "message": {"type": "TurnBegin", "payload": {}}}')
    assert rec is not None
    assert rec["timestamp"] == 1.0
