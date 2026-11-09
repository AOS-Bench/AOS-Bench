from __future__ import annotations

from aos_bench.monitor.tail.claude_parser import (
    IncrementalClaudeParser,
    _coerce_ts,
    parse_claude_line,
)


def _assistant(
    ts: str,
    *,
    msg_id: str,
    content: list[dict] | str,
    usage: dict | None = None,
    model: str = "claude-opus-4-8",
    agent_id: str | None = None,
    is_sidechain: bool = False,
) -> dict:
    message: dict = {"id": msg_id, "role": "assistant", "content": content, "model": model}
    if usage is not None:
        message["usage"] = usage
    rec: dict = {"type": "assistant", "timestamp": ts, "message": message}
    if agent_id is not None:
        rec["agentId"] = agent_id
    if is_sidechain:
        rec["isSidechain"] = True
    return rec


def _user(ts: str, *, content: list[dict] | str, agent_id: str | None = None, is_sidechain: bool = False) -> dict:
    rec: dict = {"type": "user", "timestamp": ts, "message": {"role": "user", "content": content}}
    if agent_id is not None:
        rec["agentId"] = agent_id
    if is_sidechain:
        rec["isSidechain"] = True
    return rec


def test_assistant_turn_emits_step_text_and_tokens() -> None:
    p = IncrementalClaudeParser(run_id="run_root_001")
    events = p.feed(
        [
            _user("2026-05-23T10:00:00.000Z", content="solve it"),
            _assistant(
                "2026-05-23T10:00:01.000Z",
                msg_id="m1",
                content=[
                    {"type": "thinking", "thinking": "let me think"},
                    {"type": "text", "text": "here is the plan"},
                ],
                usage={"input_tokens": 80, "cache_read_input_tokens": 20, "output_tokens": 30},
            ),
        ]
    )
    kinds = [e["kind"] for e in events]
    # user message, then step_begin, then think + text for the assistant turn.
    assert kinds == ["message", "step_begin", "message", "message"]
    step = events[1]
    assert step["payload"]["n"] == 1
    assert step["step_id"] == "run_root_001_step_1"
    # think + text payloads match kimi's shape the frontend reads.
    think = events[2]
    text = events[3]
    assert think["payload"] == {"type": "think", "think": "let me think", "role": "assistant"}
    assert text["payload"]["type"] == "text"
    assert text["payload"]["text"] == "here is the plan"
    # Tokens: 80 + 20 cache_read = 100 prompt, 30 completion.
    assert p.prompt_tokens == 100
    assert p.completion_tokens == 30


def test_tool_use_and_result_pairing() -> None:
    p = IncrementalClaudeParser(run_id="run_root_001")
    events = p.feed(
        [
            _assistant(
                "2026-05-23T10:00:01.000Z",
                msg_id="m1",
                content=[
                    {"type": "text", "text": "running a command"},
                    {"type": "tool_use", "id": "tc_1", "name": "Bash", "input": {"command": "ls"}},
                ],
                usage={"input_tokens": 10, "output_tokens": 5},
            ),
            _user(
                "2026-05-23T10:00:02.000Z",
                content=[
                    {"type": "tool_result", "tool_use_id": "tc_1", "content": "file.txt\n"},
                ],
            ),
        ]
    )
    call = next(e for e in events if e["kind"] == "tool_call")
    result = next(e for e in events if e["kind"] == "tool_result")
    # tool_call payload carries id + function.name (frontend matches on payload.id).
    assert call["payload"]["id"] == "tc_1"
    assert call["payload"]["function"]["name"] == "Bash"
    assert call["payload"]["function"]["arguments"] == {"command": "ls"}
    # tool_result links back by id and carries the body.
    assert result["payload"]["id"] == "tc_1"
    assert "file.txt" in result["payload"]["return_value"]
    assert result["payload"]["error"] is False
    # Pairing removed it from pending.
    assert "tc_1" not in p.pending_calls


def test_task_tool_synthesizes_one_spawn() -> None:
    p = IncrementalClaudeParser(run_id="run_root_001", synthesize_subagent_spawns=True)
    events = p.feed(
        [
            _assistant(
                "2026-05-23T10:00:01.000Z",
                msg_id="m1",
                content=[
                    {
                        "type": "tool_use",
                        "id": "spawn_1",
                        "name": "Task",
                        "input": {"subagent_type": "reviewer", "description": "review the diff"},
                    }
                ],
            ),
        ]
    )
    spawns = [e for e in events if e["kind"] == "subagent_spawn"]
    assert len(spawns) == 1
    assert spawns[0]["payload"]["subagent_type"] == "reviewer"
    assert spawns[0]["payload"]["description"] == "review the diff"
    # The Task call still emits a normal tool_call too.
    assert any(e["kind"] == "tool_call" and e["payload"]["id"] == "spawn_1" for e in events)
    # Feeding the same call id again does not double-spawn.
    again = p.feed(
        [
            _assistant(
                "2026-05-23T10:00:02.000Z",
                msg_id="m1",  # same streamed message id → no new step/usage
                content=[{"type": "tool_use", "id": "spawn_1", "name": "Task", "input": {}}],
            )
        ]
    )
    assert not any(e["kind"] == "subagent_spawn" for e in again)


def test_repeated_message_id_charges_usage_once() -> None:
    p = IncrementalClaudeParser(run_id="run_root_001")
    usage = {"input_tokens": 100, "output_tokens": 40}
    p.feed([_assistant("2026-05-23T10:00:01Z", msg_id="m1", content="hi", usage=usage)])
    p.feed([_assistant("2026-05-23T10:00:01.500Z", msg_id="m1", content="hi", usage=usage)])
    # Same message id across streamed chunks → counted once.
    assert p.prompt_tokens == 100
    assert p.completion_tokens == 40
    assert p.step_n == 1


def test_seq_monotonic_across_feeds() -> None:
    p = IncrementalClaudeParser(run_id="run_root_001")
    first = p.feed([_assistant("2026-05-23T10:00:01Z", msg_id="m1", content="a")])
    second = p.feed([_assistant("2026-05-23T10:00:02Z", msg_id="m2", content="b")])
    assert first[0]["seq"] == 1
    assert second[-1]["seq"] > first[-1]["seq"]


def test_coerce_ts_and_parse_line() -> None:
    assert _coerce_ts("2026-05-23T10:00:00Z") is not None
    assert _coerce_ts(1234.5) == 1234.5
    assert _coerce_ts("not-a-date") is None
    assert parse_claude_line("") is None
    assert parse_claude_line('{"no_type": 1}') is None
    rec = parse_claude_line('{"type": "assistant", "message": {"content": "x"}}')
    assert rec is not None and rec["type"] == "assistant"
