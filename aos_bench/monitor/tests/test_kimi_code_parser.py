"""Unit tests for the incremental kimi-code wire parser.

Driven by the landed ground-truth fixtures under
``aos_bench/tests/fixtures/kimi_code/`` (real ``kimi -p`` runs of the published
0.36.1 CLI against a local stub model, plus the byte-real upstream ``apps/vis``
fixtures as the older-protocol read-compat case). Only the record shapes no
fixture exercises — a ``think`` content part, an erroring tool result, a
three-item ``AgentSwarm``, a record with no ``time`` — are hand-written, and each
is labelled as source-derived.

The load-bearing test is :func:`test_events_match_offline_converter_exactly`:
this parser and ``aos_bench/trace/kimi_code.py`` must describe the same journal
in the same words, differing only by the two documented monitor kind overrides.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aos_bench.monitor.errors import DEFAULT_RULES
from aos_bench.monitor.tail.kimi_code_parser import (
    IncrementalKimiCodeParser,
    parse_kimi_code_line,
)
from aos_bench.monitor.tasks import extract_task_lifecycle_from_events
from aos_bench.trace.kimi_code import (
    MONITOR_KIND_OVERRIDES,
    find_kimi_code_latest_session_dir,
    normalize_kimi_code_records,
)

FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures" / "kimi_code"
LIVE = FIXTURES / "live_probe"
VIS_SESSIONS = FIXTURES / "vis_sample" / "sessions"

LIVE_CASES = (
    "solo",
    "subagent",
    "swarm",
    "background",
    "mcp_v2_no_tools",
    "mcp_legacy",
)

# The closed set of kinds the dashboard can render: the 15 values
# ``aos_bench/monitor/tail/parser.py:20-38`` maps to, plus the synthesized
# ``subagent_spawn`` (``parser.py:143-165``). Anything else falls into the
# frontend's ``default`` arm (``app.js:1577``).
MONITOR_KINDS = frozenset(
    {
        "turn_begin",
        "turn_end",
        "step_begin",
        "step_end",
        "compaction_begin",
        "compaction_end",
        "status",
        "tool_call",
        "tool_result",
        "subagent_event",
        "subagent_spawn",
        "approval_request",
        "approval_response",
        "hook",
        "notification",
        "message",
    }
)

# ``aos_bench/monitor/tail/parser.py:41-48`` — the only three legal phases.
PHASES = frozenset({"begin", "update", "completed"})

# The canonical event dict (``parser.py:220-237``).
EVENT_KEYS = frozenset(
    {
        "event_id",
        "seq",
        "timestamp",
        "kind",
        "phase",
        "step_id",
        "parent_event_id",
        "payload",
        "metrics",
        "raw_ref",
        "extra",
    }
)


# ── fixture helpers ────────────────────────────────────────────────────


def _agent_records(session_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    """Every parsed record of one agent journal, in write order."""
    path = session_dir / "agents" / agent_id / "wire.jsonl"
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        record = parse_kimi_code_line(raw)
        if record is not None:
            records.append(record)
    return records


def _live_session(case: str) -> Path:
    session_dir = find_kimi_code_latest_session_dir(LIVE / case)
    assert session_dir is not None, f"fixture {case} has no session dir"
    return session_dir


def _main_records(case: str) -> list[dict[str, Any]]:
    return _agent_records(_live_session(case), "main")


def _loop(event: dict[str, Any], time_ms: int) -> dict[str, Any]:
    return {"type": "context.append_loop_event", "event": event, "time": time_ms}


# ── parse_kimi_code_line ───────────────────────────────────────────────


def test_parse_kimi_code_line_drops_blank_and_garbage() -> None:
    assert parse_kimi_code_line("") is None
    assert parse_kimi_code_line("   ") is None
    assert parse_kimi_code_line("not json") is None
    assert parse_kimi_code_line("[1, 2]") is None  # not a dict
    assert parse_kimi_code_line(json.dumps({"time": 1})) is None  # no ``type``
    # A torn final line (the only line that can ever be torn) is dropped.
    assert parse_kimi_code_line('{"type": "turn.prom') is None


def test_parse_kimi_code_line_keeps_the_metadata_header() -> None:
    """Unlike ``parse_wire_line`` (parser.py:254-255) the header survives the
    line parse — the parser consumes it for ``protocol_version`` / ``created_at``
    and drops it from the event stream instead."""
    record = parse_kimi_code_line(
        json.dumps(
            {"type": "metadata", "protocol_version": "1.5", "created_at": 1786981621294}
        )
    )
    assert record is not None
    assert record["protocol_version"] == "1.5"

    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    assert parser.feed([record]) == []
    assert parser.protocol_version == "1.5"
    assert parser.created_at == 1786981621.294


# ── the lockstep guarantee with the offline converter ──────────────────


@pytest.mark.parametrize("case", LIVE_CASES)
def test_events_match_offline_converter_exactly(case: str) -> None:
    """Live events must be identical to the post-run AgentTrace's events for the
    same records, modulo ``MONITOR_KIND_OVERRIDES``. This is the property the
    shared kind map / payload builders exist to guarantee; if it ever breaks, the
    dashboard and the trace are describing the same run differently."""
    records = _main_records(case)
    offline, _info = normalize_kimi_code_records(
        records, run_id="run_root_001", synthesize_subagent_spawns=True
    )
    live = IncrementalKimiCodeParser(
        run_id="run_root_001", synthesize_subagent_spawns=True
    ).feed(records)

    assert len(live) == len(offline)
    for live_ev, offline_ev in zip(live, offline):
        expected_kind = MONITOR_KIND_OVERRIDES.get(
            offline_ev["kind"], offline_ev["kind"]
        )
        assert live_ev["kind"] == expected_kind
        assert {**live_ev, "kind": offline_ev["kind"]} == offline_ev


@pytest.mark.parametrize("case", LIVE_CASES)
def test_event_shape_is_the_canonical_monitor_contract(case: str) -> None:
    parser = IncrementalKimiCodeParser(
        run_id="run_root_001", synthesize_subagent_spawns=True
    )
    events = parser.feed(_main_records(case))
    assert events, f"fixture {case} produced no events"
    for index, event in enumerate(events, start=1):
        assert set(event) == EVENT_KEYS
        assert event["kind"] in MONITOR_KINDS
        assert event["phase"] in PHASES
        assert event["parent_event_id"] is None
        assert event["seq"] == index
        assert event["event_id"] == f"run_root_001_evt_{index:04d}"
        # ms → s: the SVG renderer silently drops non-float timestamps
        # (app.js:545), so a lane would look empty rather than wrong.
        assert isinstance(event["timestamp"], float)
        assert 1e9 < event["timestamp"] < 1e10
        assert isinstance(event["payload"], dict)
        assert isinstance(event["metrics"], dict)
        assert event["raw_ref"]["source"] == "kimi-code-wire"
        assert isinstance(event["raw_ref"]["message_type"], str)


# ── step_begin / step_id / seq bookkeeping ─────────────────────────────


def test_step_begin_is_emitted_and_step_ids_progress() -> None:
    """``n_llm_calls`` derives from ``step_begin`` (state.py:327-328); without it
    every lane header shows llm=0 (app.js:1698). The solo fixture has two
    ``step.begin`` loop events."""
    records = _main_records("solo")
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    events = parser.feed(records)

    steps = [event for event in events if event["kind"] == "step_begin"]
    assert len(steps) == 2
    assert [event["step_id"] for event in steps] == [
        "run_root_001_step_1",
        "run_root_001_step_2",
    ]
    # ``payload.n`` is OUR counter (app.js:1716-1721 renders it); the native
    # ``event.step`` restarts at 1 per turn and is preserved alongside it.
    assert [event["payload"]["n"] for event in steps] == [1, 2]
    assert [event["payload"]["native_step"] for event in steps] == [1, 2]
    assert parser.step_n == 2

    # Every event after the first step_begin carries a step_id; the ones before
    # it (profile.bind, turn.prompt, …) carry None.
    first_step_index = events.index(steps[0])
    assert all(event["step_id"] is None for event in events[:first_step_index])
    assert all(event["step_id"] is not None for event in events[first_step_index:])


def test_native_step_restarts_per_turn_but_step_id_does_not() -> None:
    """Ground truth: ``event.step`` restarts at 1 on every turn (the subagent
    fixture's agent-0 has turnId "0" step 1 then turnId "1" step 1), which is why
    it cannot back the gap-free step counter."""
    records = _agent_records(_live_session("subagent"), "agent-0")
    events = IncrementalKimiCodeParser(run_id="run_sub_agent-0").feed(records)
    steps = [event for event in events if event["kind"] == "step_begin"]
    assert [event["payload"]["native_step"] for event in steps] == [1, 1]
    assert [event["payload"]["turn_id"] for event in steps] == ["0", "1"]
    assert [event["step_id"] for event in steps] == [
        "run_sub_agent-0_step_1",
        "run_sub_agent-0_step_2",
    ]


def test_seq_and_step_state_continue_across_feed_calls() -> None:
    """The tail feeds whatever bytes landed since the last tick, so a step can be
    opened in one batch and used by the next."""
    records = _main_records("solo")
    split = 12  # mid-run: after the first step.begin, before the second
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    first = parser.feed(records[:split])
    second = parser.feed(records[split:])

    whole = IncrementalKimiCodeParser(run_id="run_root_001").feed(records)
    assert first + second == whole

    assert [event["seq"] for event in first + second] == list(
        range(1, len(first) + len(second) + 1)
    )
    # The step opened in the first batch still labels the second batch's events.
    assert second[0]["step_id"] == "run_root_001_step_1"


# ── tokens: step.end only, never usage.record ──────────────────────────


def test_tokens_come_from_step_end_only() -> None:
    """``usage.record`` repeats the identical TokenUsage object at turn scope
    (byte-for-byte in the solo fixture), so charging both would double every
    token in ``state.push_events`` (state.py:331-337)."""
    records = _main_records("solo")
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    events = parser.feed(records)

    charged = [event for event in events if event["metrics"].get("prompt_tokens")]
    assert [event["kind"] for event in charged] == ["step_end", "step_end"]
    # The two usage.record statuses carry the same numbers but no metrics.
    usage_statuses = [
        event
        for event in events
        if event["kind"] == "status" and "token_usage" in event["payload"]
    ]
    assert len(usage_statuses) == 2
    assert all(event["metrics"] == {} for event in usage_statuses)

    assert parser.prompt_tokens == sum(e["metrics"]["prompt_tokens"] for e in charged)
    assert parser.completion_tokens == sum(
        e["metrics"]["completion_tokens"] for e in charged
    )
    # inputOther + inputCacheRead + inputCacheCreation, output — twice.
    assert parser.prompt_tokens == 2 * (23 + 100 + 0)
    assert parser.completion_tokens == 2 * 45


def test_turn_and_status_bookkeeping_from_the_solo_run() -> None:
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    parser.feed(_main_records("solo"))
    assert parser.n_turns == 1
    assert parser.last_turn_reason == "completed"
    assert parser.last_finish_reason == "end_turn"
    assert parser.protocol_version == "1.5"
    assert parser.profile_name == "agent"
    assert parser.model_alias == "__kimi_env_model__"
    assert parser.first_ts is not None and parser.last_ts is not None
    assert parser.last_ts >= parser.first_ts


def test_lane_header_stats_all_populate_from_the_events() -> None:
    """P10: the lane header prints events / llm / tools / ctx_gen / in / out off
    ``RunSummary`` (app.js:1698; state.py:117-133). Push the solo run through the
    real state object so a lane that would render visibly degraded fails here."""
    from aos_bench.monitor.state import MonitorState

    state = MonitorState(jobs_dir="/tmp")
    state.ensure_trial("t1", "/tmp/t1", agent="kimi-code")
    state.ensure_run("t1", "run_root_001", role="root", agent_name="orchestrator")
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    state.push_events("t1", "run_root_001", parser.feed(_main_records("solo")))

    run = state.trials["t1"].runs["run_root_001"]
    assert run.n_events > 0
    assert run.n_llm_calls == 2  # two step.begin
    assert run.n_tool_calls == 1  # the Bash call
    assert run.prompt_tokens == parser.prompt_tokens > 0
    assert run.completion_tokens == parser.completion_tokens > 0
    assert run.started_at is not None and run.last_event_at is not None
    # Structural markers survive the rolling tail cap (state.py:346-347).
    assert [event["kind"] for event in run.structural_events] == [
        "turn_begin",
        "turn_end",
    ]


# ── tool_call / tool_result payload contracts (P9, P12) ────────────────


def test_tool_call_and_result_carry_both_id_keys() -> None:
    """``app.js:1512-1531`` accepts either id key, but ``tasks.py`` reads ``id``
    on tool_call (:149) and ``tool_call_id`` on tool_result (:178) — so both are
    set on both."""
    events = IncrementalKimiCodeParser(run_id="run_root_001").feed(
        _main_records("solo")
    )
    call = next(event for event in events if event["kind"] == "tool_call")
    result = next(event for event in events if event["kind"] == "tool_result")

    assert call["payload"]["id"] == call["payload"]["tool_call_id"] == "call_probe_1"
    assert call["payload"]["function"]["name"] == "Bash"
    assert json.loads(call["payload"]["function"]["arguments"]) == {"command": "ls -1"}
    assert call["phase"] == "begin"

    assert (
        result["payload"]["id"] == result["payload"]["tool_call_id"] == "call_probe_1"
    )
    # The kimi dict shape ``{"output": …}`` is what tasks.py:115-124 handles.
    assert result["payload"]["return_value"] == {"output": "README.txt\n"}
    assert "error" not in result["payload"]
    assert result["phase"] == "completed"


def test_failed_tool_result_trips_the_shared_error_rule() -> None:
    """Source-derived shape (no fixture has an erroring tool result): the
    ``isError`` flag must surface as ``payload["error"] = True`` or
    ``errors.py:51-57`` never fires (blueprint §4 P8)."""
    events = IncrementalKimiCodeParser(run_id="run_root_001").feed(
        [
            _loop(
                {
                    "type": "tool.result",
                    "toolCallId": "call_1",
                    "result": {"output": "boom: no such file", "isError": True},
                },
                1786981621486,
            )
        ]
    )
    assert len(events) == 1
    assert events[0]["payload"]["error"] is True
    marked = next(
        rule for rule in DEFAULT_RULES if rule.rule_id == "event_marked_error"
    )
    assert marked.check(events[0]) is not None


def test_dispatcher_task_snapshot_reads_the_normalized_events() -> None:
    """P12: with the kimi dict ``return_value`` shape the dispatcher panel works
    with zero changes, including the ``mcp__aos_pool__`` name prefix."""
    snapshot = [
        {"task_id": "t1", "at_sec": 0, "status": "completed", "note": "ok"},
        {"task_id": "t2", "at_sec": 5, "status": "ready", "note": ""},
    ]
    events = IncrementalKimiCodeParser(run_id="run_root_001").feed(
        [
            _loop(
                {
                    "type": "tool.call",
                    "toolCallId": "call_list",
                    "name": "mcp__aos_pool__todo_list",
                    "args": {},
                },
                1786981621000,
            ),
            _loop(
                {
                    "type": "tool.result",
                    "toolCallId": "call_list",
                    "result": {"output": json.dumps(snapshot)},
                },
                1786981621100,
            ),
        ]
    )
    rows = extract_task_lifecycle_from_events(events)
    assert [row["task_id"] for row in rows] == ["t1", "t2"]
    assert rows[0]["status"] == "completed"


# ── message payloads + the two monitor kind overrides ──────────────────


def test_text_part_becomes_a_message_the_frontend_can_render() -> None:
    events = IncrementalKimiCodeParser(run_id="run_root_001").feed(
        _main_records("solo")
    )
    texts = [
        event
        for event in events
        if event["kind"] == "message" and event["payload"].get("type") == "text"
    ]
    assert any(event["payload"]["text"] == "Files listed. done." for event in texts)


def test_think_part_downgrades_reasoning_to_message() -> None:
    """Source-derived (no fixture has a ``think`` part). AgentTrace calls this
    ``reasoning``; the monitor has no such kind, so it renders as
    ``message`` + ``payload.type == "think"`` (app.js:517-520)."""
    events = IncrementalKimiCodeParser(run_id="run_root_001").feed(
        [
            _loop(
                {
                    "type": "content.part",
                    "turnId": "0",
                    "step": 1,
                    "part": {"type": "think", "think": "weighing options"},
                },
                1786981621490,
            )
        ]
    )
    assert events[0]["kind"] == "message"
    assert events[0]["payload"] == {"type": "think", "think": "weighing options"}


def test_background_task_records_downgrade_to_status_keeping_phase() -> None:
    """AgentTrace has a ``background_task`` kind; the monitor does not, so both
    records render as ``status`` — but the begin/completed phase pair survives so
    the timeline still shows the task opening and closing."""
    events = IncrementalKimiCodeParser(run_id="run_root_001").feed(
        _main_records("background")
    )
    tasks = [
        event
        for event in events
        if event["raw_ref"]["message_type"] in {"task.started", "task.terminated"}
    ]
    assert [event["kind"] for event in tasks] == ["status", "status"]
    assert [event["phase"] for event in tasks] == ["begin", "completed"]
    assert tasks[0]["payload"]["task_id"] == "agent-g7xgrry2"
    assert tasks[1]["payload"]["status"] == "completed"


def test_turn_begin_synthesizes_user_input_from_input_parts() -> None:
    """There is no ``user_input`` field on disk — the prompt is
    ``input: [{type:'text',text:…}]`` plus an ``origin`` block."""
    events = IncrementalKimiCodeParser(run_id="run_root_001").feed(
        _main_records("solo")
    )
    turn_begin = next(event for event in events if event["kind"] == "turn_begin")
    assert turn_begin["phase"] == "begin"
    assert (
        turn_begin["payload"]["user_input"]
        == "List the files in this directory with Bash, then say done."
    )
    assert turn_begin["extra"]["origin_kind"] == "user"


def test_subagent_driven_turns_are_discriminated_by_origin() -> None:
    """``origin.kind == "system_trigger"`` with ``name == "subagent"`` is the
    clean tell for a turn the parent drove, and it reaches the dashboard in
    ``extra``."""
    records = _agent_records(_live_session("subagent"), "agent-0")
    events = IncrementalKimiCodeParser(run_id="run_sub_agent-0").feed(records)
    turn_begins = [event for event in events if event["kind"] == "turn_begin"]
    assert len(turn_begins) == 2
    assert all(
        event["extra"]["origin_kind"] == "system_trigger" for event in turn_begins
    )
    assert all(event["extra"]["origin_name"] == "subagent" for event in turn_begins)


def test_failed_turn_flags_error_but_a_cancelled_one_does_not() -> None:
    """Source-derived: no fixture has a failed or cancelled turn. Only
    ``reason == "failed"`` may set the errors.py marker, or every user
    cancellation would be reported as an error."""
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    failed, cancelled = parser.feed(
        [
            {
                "type": "turn.ended",
                "turnId": 0,
                "reason": "failed",
                "error": {"message": "provider 500"},
                "time": 1786981621500,
            },
            {
                "type": "turn.ended",
                "turnId": 1,
                "reason": "cancelled",
                "time": 1786981621600,
            },
        ]
    )
    assert failed["kind"] == cancelled["kind"] == "turn_end"
    assert failed["payload"]["error"] is True
    assert "error" not in cancelled["payload"]
    assert parser.last_turn_reason == "cancelled"


# ── spawn synthesis (Agent / AgentSwarm) ───────────────────────────────


def test_spawn_synthesis_is_off_by_default() -> None:
    """Only the root parser gets the flag — a subagent lane must not re-draw its
    parent's arrows."""
    events = IncrementalKimiCodeParser(run_id="run_sub_agent-0").feed(
        _main_records("subagent")
    )
    assert not [event for event in events if event["kind"] == "subagent_spawn"]


def test_foreground_agent_spawn_is_backfilled_from_the_tool_result() -> None:
    """The ``Agent`` result opens with ``agent_id: <id>``, so attribution is
    exact. The spawn event is emitted *before* its ``tool_call`` (so the arrow
    sits at the call site) with ``child_run_id: None``, then back-filled **in
    place** — the dict MonitorState already holds is the one that gains the
    link."""
    records = _main_records("subagent")
    parser = IncrementalKimiCodeParser(
        run_id="run_root_001", synthesize_subagent_spawns=True
    )
    events = parser.feed(records)

    spawns = [event for event in events if event["kind"] == "subagent_spawn"]
    assert len(spawns) == 1
    spawn = spawns[0]
    call_index = next(
        index for index, event in enumerate(events) if event["kind"] == "tool_call"
    )
    assert events.index(spawn) == call_index - 1
    assert spawn["phase"] == "begin"
    assert spawn["payload"]["child_run_id"] == "run_sub_agent-0"
    assert spawn["payload"]["task_id"] == "agent-0"  # app.js:1528-1540 fallback key
    assert spawn["payload"]["subagent_type"] == "coder"
    assert spawn["payload"]["launch_mode"] == "foreground"
    assert spawn["payload"]["parent_tool_call_id"] == "call_agent_1"
    # The join results are what the tail reads to wire the child lane.
    assert parser.spawn_events["agent-0"] is spawn
    assert parser.child_launch_modes == {"agent-0": "foreground"}
    assert parser.child_subagent_types == {"agent-0": "coder"}


def test_spawn_child_run_id_is_unknown_until_the_result_lands() -> None:
    """Split the feed at the tool call: the arrow exists immediately (so a lane
    is never missing a spawn marker) and the link appears on the next tick."""
    records = _main_records("subagent")
    result_index = next(
        index
        for index, record in enumerate(records)
        if (record.get("event") or {}).get("type") == "tool.result"
    )
    parser = IncrementalKimiCodeParser(
        run_id="run_root_001", synthesize_subagent_spawns=True
    )
    first = parser.feed(records[:result_index])
    spawn = next(event for event in first if event["kind"] == "subagent_spawn")
    assert spawn["payload"]["child_run_id"] is None
    assert spawn["payload"]["task_id"] is None

    parser.feed(records[result_index:])
    # Same object, mutated in place — no second spawn event was emitted.
    assert spawn["payload"]["child_run_id"] == "run_sub_agent-0"


def test_agent_swarm_emits_one_spawn_per_item_bound_by_item() -> None:
    """The swarm fixture fans out to ``alpha`` / ``beta`` and the result names
    both children with their items, so each lane binds to its own child."""
    parser = IncrementalKimiCodeParser(
        run_id="run_root_001", synthesize_subagent_spawns=True
    )
    events = parser.feed(_main_records("swarm"))
    spawns = [event for event in events if event["kind"] == "subagent_spawn"]
    assert len(spawns) == 2
    assert [event["payload"]["swarm_item"] for event in spawns] == ["alpha", "beta"]
    assert [event["payload"]["child_run_id"] for event in spawns] == [
        "run_sub_agent-0",
        "run_sub_agent-1",
    ]
    assert all(event["payload"]["spawn_tool"] == "AgentSwarm" for event in spawns)
    assert parser.child_swarm_items == {"agent-0": "alpha", "agent-1": "beta"}


def test_agent_swarm_with_three_items_emits_three_spawns() -> None:
    """Source-derived item count (the fixture has two): the lane count must match
    ``items[]``, so a three-item swarm is three spawn events sharing one
    tool-call id."""
    events = IncrementalKimiCodeParser(
        run_id="run_root_001", synthesize_subagent_spawns=True
    ).feed(
        [
            _loop(
                {
                    "type": "tool.call",
                    "toolCallId": "call_swarm_1",
                    "name": "AgentSwarm",
                    "args": {
                        "description": "fan out",
                        "subagent_type": "coder",
                        "prompt_template": "do {{item}}",
                        "items": ["a", "b", "c"],
                    },
                },
                1786981851770,
            )
        ]
    )
    spawns = [event for event in events if event["kind"] == "subagent_spawn"]
    assert len(spawns) == 3
    assert [event["payload"]["swarm_item"] for event in spawns] == ["a", "b", "c"]
    assert [event["seq"] for event in spawns] == [1, 2, 3]
    # The tool_call event follows the three spawns, numbering unbroken.
    assert events[-1]["kind"] == "tool_call"
    assert events[-1]["seq"] == 4


def test_background_agent_spawn_flips_launch_mode_and_carries_the_task_id() -> None:
    """``task.started.info`` names both ``parentToolCallId`` and ``agentId`` — the
    exact join a ``run_in_background`` delegation needs."""
    parser = IncrementalKimiCodeParser(
        run_id="run_root_001", synthesize_subagent_spawns=True
    )
    events = parser.feed(_main_records("background"))
    spawns = [event for event in events if event["kind"] == "subagent_spawn"]
    assert len(spawns) == 1
    assert spawns[0]["payload"]["launch_mode"] == "background"
    assert spawns[0]["payload"]["child_run_id"] == "run_sub_agent-0"
    assert spawns[0]["payload"]["native_task_id"] == "agent-g7xgrry2"
    assert parser.child_launch_modes == {"agent-0": "background"}
    assert parser.background_tasks["agent-g7xgrry2"]["status"] == "completed"


def test_spawn_is_deduped_per_loop_event_uuid_across_a_replay() -> None:
    """``_FileTail`` re-reads from byte 0 after a truncation or rotation, so the
    live parser can see the same ``Agent`` call twice; the offline reader never
    does. One arrow per call, always — keyed on the loop event's ``uuid``, which
    is stable across a replay of the same bytes."""
    records = _main_records("subagent")
    parser = IncrementalKimiCodeParser(
        run_id="run_root_001", synthesize_subagent_spawns=True
    )
    parser.feed(records)
    replay = parser.feed(records)
    assert not [event for event in replay if event["kind"] == "subagent_spawn"]
    assert parser.synthesized_spawns == {"95a6413a-efa9-4a15-9195-2a123d542314"}


def test_two_agent_calls_sharing_a_tool_call_id_stay_distinct() -> None:
    """``toolCallId`` is provider-generated and only unique *within* a step —
    upstream tolerates per-response counter ids (``call_0`` again next step). Two
    genuinely different ``Agent`` calls must draw two arrows and bind their own
    children; deduping on the id collapsed them into one."""
    parser = IncrementalKimiCodeParser(
        run_id="run_root_001", synthesize_subagent_spawns=True
    )
    events = parser.feed(
        [
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "tool.call",
                    "uuid": "u1",
                    "toolCallId": "call_0",
                    "name": "Agent",
                    "args": {"prompt": "a"},
                },
                "time": 1_000_100,
            },
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "tool.result",
                    "parentUuid": "u1",
                    "toolCallId": "call_0",
                    "result": {"output": "agent_id: agent-0\nstatus: completed"},
                },
                "time": 1_000_200,
            },
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "tool.call",
                    "uuid": "u2",
                    "toolCallId": "call_0",
                    "name": "Agent",
                    "args": {"prompt": "b"},
                },
                "time": 1_000_300,
            },
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "tool.result",
                    "parentUuid": "u2",
                    "toolCallId": "call_0",
                    "result": {"output": "agent_id: agent-1\nstatus: completed"},
                },
                "time": 1_000_400,
            },
        ]
    )
    spawns = [event for event in events if event["kind"] == "subagent_spawn"]
    assert len(spawns) == 2
    # Each result binds the spawn it actually answers, in call order.
    assert [spawn["payload"]["child_run_id"] for spawn in spawns] == [
        "run_sub_agent-0",
        "run_sub_agent-1",
    ]
    assert [spawn["payload"]["prompt"] for spawn in spawns] == ["a", "b"]
    assert set(parser.spawn_events) == {"agent-0", "agent-1"}


def test_non_spawning_tools_never_synthesize() -> None:
    events = IncrementalKimiCodeParser(
        run_id="run_root_001", synthesize_subagent_spawns=True
    ).feed(_main_records("solo"))
    assert not [event for event in events if event["kind"] == "subagent_spawn"]


# ── MCP: a journal with no mcp record, and the positive case ───────────


def test_no_mcp_record_yields_no_mcp_servers() -> None:
    """``mcp_servers == []`` means only "this journal carried no ``mcp.*``
    record" — never "``aos_pool`` failed to wire up". The fixture behind this
    case came from an offline HTTP stub the v2 engine declined to register;
    against the real FastMCP ``aos_pool`` the same engine *does* register the
    tools and write ``mcp.tools_discovered`` (verified live 2026-08-18), so
    treat an empty list as absence of evidence, not evidence of absence."""
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    parser.feed(_main_records("mcp_v2_no_tools"))
    assert parser.mcp_servers == []


def test_legacy_engine_surfaces_mcp_tools_discovered_as_a_status() -> None:
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    events = parser.feed(_main_records("mcp_legacy"))
    assert parser.protocol_version == "1.4"
    assert parser.mcp_servers == ["aos_pool"]
    discovered = next(
        event
        for event in events
        if event["raw_ref"]["message_type"] == "mcp.tools_discovered"
    )
    assert discovered["kind"] == "status"
    assert discovered["payload"]["server_name"] == "aos_pool"
    assert discovered["payload"]["tool_names"] == ["todo_next", "todo_status"]
    # The legacy engine spells the lane label ``config.update``, not
    # ``profile.bind`` — both feed ``profile_name``.
    assert parser.profile_name == "agent"
    assert parser.last_finish_reason == "end_turn"
    assert parser.last_turn_reason is None  # legacy never writes turn.ended


# ── older protocol (upstream apps/vis fixtures, pv 1.1) ────────────────


def test_vis_sample_reads_with_the_same_kind_map() -> None:
    session_dir = VIS_SESSIONS / "sample-main"
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    events = parser.feed(_agent_records(session_dir, "main"))
    assert parser.protocol_version == "1.1"
    kinds = [event["kind"] for event in events]
    assert kinds == [
        "status",  # config.update
        "status",  # tools.set_active_tools
        "status",  # permission.set_mode
        "turn_begin",
        "message",  # context.append_message role=user
        "step_begin",
        "message",  # content.part text
        "step_end",
        "status",  # usage.record
    ]
    assert all(kind in MONITOR_KINDS for kind in kinds)


def test_vis_sample_compaction_renders_a_compaction_end() -> None:
    """``ctx_gen`` in the lane header counts compactions (app.js:1698), and the
    timeline pairs begin/end spans — the older engine only writes the apply
    record, which maps to ``compaction_end``."""
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    events = parser.feed(_agent_records(VIS_SESSIONS / "sample-compaction", "main"))
    compactions = [event for event in events if event["kind"].startswith("compaction")]
    assert [event["kind"] for event in compactions] == ["compaction_end"]
    assert compactions[0]["payload"]["summary"] == "compacted summary"
    assert compactions[0]["payload"]["compactedCount"] == 2
    # The assistant message is dropped — the loop events already cover it, and
    # emitting both would double-count.
    assert not [
        event for event in events if event["payload"].get("role") == "assistant"
    ]


# ── defensive shapes with no fixture ──────────────────────────────────


def test_missing_time_carries_the_last_known_timestamp_forward() -> None:
    """``time`` is optional on a WireRecord; a ``None`` timestamp would make the
    SVG renderer drop the event (app.js:545)."""
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    events = parser.feed(
        [
            {"type": "permission.set_mode", "mode": "auto", "time": 1786981621370},
            {"type": "permission.set_mode", "mode": "auto"},  # no ``time``
        ]
    )
    assert events[1]["timestamp"] == events[0]["timestamp"] == 1786981621.37


def test_unknown_control_family_record_is_a_status_not_a_message() -> None:
    """A record type this build does not emit today (``tools.foo``) must not leak
    into the message stream; unknown *non*-control types fall back to
    ``message`` (matching parser.py:37)."""
    events = IncrementalKimiCodeParser(run_id="run_root_001").feed(
        [
            {"type": "tools.something_new", "names": [], "time": 1786981621370},
            {"type": "brand_new_thing", "time": 1786981621371},
        ]
    )
    assert [event["kind"] for event in events] == ["status", "message"]


def test_llm_records_are_dropped_but_still_name_the_model() -> None:
    """``llm.request`` + ``llm.tools_snapshot`` are ~95 KB of a 101 KB journal and
    never become events, yet they are the only place the real model name
    appears."""
    parser = IncrementalKimiCodeParser(run_id="run_root_001")
    events = parser.feed(_main_records("solo"))
    assert not [
        event for event in events if event["raw_ref"]["message_type"].startswith("llm.")
    ]
    assert parser.model_name
    assert parser.model_provider
