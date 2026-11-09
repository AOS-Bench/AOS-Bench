"""kimi-code wire reader / AgentTrace converter tests.

These are driven by the **landed ground-truth fixtures** under
``aos_bench/tests/fixtures/kimi_code/`` rather than by hand-written records:

* ``live_probe/{solo,subagent,swarm,background,mcp_v2_no_tools,mcp_legacy}`` —
  real ``kimi -p`` runs of the published 0.36.1 CLI against a local stub model
  (default v2 engine at ``protocol_version 1.5``, plus one legacy-engine run at
  ``1.4``). This is what an AOS-Bench trial actually produces.
* ``vis_sample/`` — byte-real upstream ``apps/vis`` fixtures at ``1.1``, shaped
  like the legacy engine. Read-compat case only; never the source of truth for
  the v1 kind map (see the fixture README).

Only the handful of record shapes that no fixture exercises (a ``think`` part,
an erroring tool result, a failed turn, a missing ``time``, a ``blobref:`` part,
an index tombstone) are written by hand, and each is marked as source-derived.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from aos_bench.trace.kimi_code import (
    MONITOR_KIND_OVERRIDES,
    KimiCodeSession,
    collect_kimi_code_agent_trace,
    find_kimi_code_latest_session_dir,
    find_kimi_code_session_dir,
    map_wire_kind,
    normalize_kimi_code_records,
    parse_kimi_code_wire_line,
    read_kimi_code_session,
)
from aos_bench.visualization import render_agent_trace_svg

FIXTURES = Path(__file__).parent / "fixtures" / "kimi_code"
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

# The AgentTrace event kinds this converter is allowed to emit
# (docs/framework/agent_trace.md §8.3).
ALLOWED_KINDS = {
    "turn_begin",
    "turn_end",
    "step_begin",
    "step_end",
    "message",
    "reasoning",
    "tool_call",
    "tool_result",
    "subagent_spawn",
    "subagent_event",
    "status",
    "approval_request",
    "approval_response",
    "background_task",
    "hook",
    "compaction_begin",
    "compaction_end",
    "error",
}


def _session_dir(case: str) -> Path:
    session_dir = find_kimi_code_latest_session_dir(LIVE / case)
    assert session_dir is not None, f"fixture {case} has no session dir"
    return session_dir


def _write_wire(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _kinds(events: list[dict[str, Any]]) -> list[str]:
    return [event["kind"] for event in events]


def _by_kind(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event["kind"] == kind]


# ── discovery ────────────────────────────────────────────────────────────


def test_find_session_dir_from_work_dir_key() -> None:
    """The ``wd_<slug>_<sha256[:12]>`` key is deterministic, so no index needed."""

    home = LIVE / "solo"
    work_dir = json.loads((home / "session_index.jsonl").read_text(encoding="utf-8"))[
        "workDir"
    ]

    session_dir = find_kimi_code_session_dir(home, work_dir)

    assert session_dir.parent.name == "wd_work_dc413a888a39"
    assert session_dir.name.startswith("session_")
    assert (session_dir / "agents" / "main" / "wire.jsonl").is_file()


def test_find_session_dir_raises_for_unknown_work_dir() -> None:
    with pytest.raises(FileNotFoundError):
        find_kimi_code_session_dir(LIVE / "solo", "/nowhere/at/all")


def test_find_latest_session_dir_reroots_recorded_session_dir(tmp_path: Path) -> None:
    """``session_index.jsonl`` records the path the *agent* saw.

    In a trial the agent writes under ``/logs/agent/kimi-code-home`` (the
    bind-mount target) while we read ``<trial>/agent/kimi-code-home``, so the
    recorded absolute path never resolves host-side and must be re-rooted onto
    the home we were handed.
    """

    home = tmp_path / "kimi-code-home"
    shutil.copytree(LIVE / "subagent", home)
    index_path = home / "session_index.jsonl"
    entry = json.loads(index_path.read_text(encoding="utf-8"))
    container_path = (
        "/logs/agent/kimi-code-home/sessions/"
        f"wd_work2_c564c142c5ec/{entry['sessionId']}"
    )
    index_path.write_text(
        json.dumps({**entry, "sessionDir": container_path, "workDir": "/app"}) + "\n",
        encoding="utf-8",
    )
    assert not Path(container_path).exists()

    session_dir = find_kimi_code_latest_session_dir(home)

    assert session_dir is not None
    assert session_dir.name == entry["sessionId"]
    assert session_dir.is_relative_to(home)
    assert (session_dir / "agents" / "agent-0" / "wire.jsonl").is_file()


def test_find_latest_session_dir_honours_tombstones_and_falls_back(
    tmp_path: Path,
) -> None:
    """A ``{sessionId, deleted:true}`` line retires an entry; then glob wins."""

    home = tmp_path / "kimi-code-home"
    shutil.copytree(LIVE / "solo", home)
    index_path = home / "session_index.jsonl"
    entry = json.loads(index_path.read_text(encoding="utf-8"))
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"sessionId": entry["sessionId"], "deleted": True}) + "\n"
        )
        handle.write("{partial-torn-line\n")  # only the last line can ever be torn

    session_dir = find_kimi_code_latest_session_dir(home)

    # The index is now empty, so the tiered glob resolves the same session.
    assert session_dir is not None
    assert session_dir.name == entry["sessionId"]


def test_find_latest_session_dir_returns_none_when_home_is_empty(
    tmp_path: Path,
) -> None:
    assert find_kimi_code_latest_session_dir(tmp_path) is None
    (tmp_path / "sessions").mkdir()
    assert find_kimi_code_latest_session_dir(tmp_path) is None


def test_find_latest_session_dir_tolerates_upstream_fixture_layout() -> None:
    """``vis_sample`` uses ``sessions/<name>/`` rather than ``wd_*/session_*``."""

    session_dir = find_kimi_code_latest_session_dir(FIXTURES / "vis_sample")

    assert session_dir is not None
    assert session_dir.parent.name == "sessions"


# ── line parsing ─────────────────────────────────────────────────────────


def test_parse_wire_line_accepts_real_records() -> None:
    line = (LIVE / "solo").glob("sessions/*/*/agents/main/wire.jsonl")
    first = next(line).read_text(encoding="utf-8").splitlines()[0]

    record = parse_kimi_code_wire_line(first)

    assert record is not None
    assert record["type"] == "metadata"
    assert record["protocol_version"] == "1.5"


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        '{"type": "turn.prompt", "time": 1',  # torn final line
        "[1, 2, 3]",  # not an object
        '{"time": 1}',  # no dotted type
        "not json at all",
    ],
)
def test_parse_wire_line_rejects_unusable_lines(line: str) -> None:
    assert parse_kimi_code_wire_line(line) is None


# ── kind map ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"type": "metadata", "protocol_version": "1.5"}, ""),
        ({"type": "turn.prompt"}, "turn_begin"),
        ({"type": "turn.ended"}, "turn_end"),
        ({"type": "turn.cancel"}, "turn_end"),
        ({"type": "turn.steer"}, "status"),
        ({"type": "usage.record"}, "status"),
        ({"type": "token_counting.measured"}, "status"),
        ({"type": "full_compaction.begin"}, "compaction_begin"),
        ({"type": "full_compaction.complete"}, "compaction_end"),
        ({"type": "context.apply_compaction"}, "compaction_end"),
        ({"type": "context.clear"}, "status"),
        ({"type": "interaction.request"}, "approval_request"),
        ({"type": "interaction.resolved"}, "approval_response"),
        ({"type": "permission.record_approval_result"}, "approval_response"),
        ({"type": "permission.set_mode"}, "status"),
        ({"type": "profile.bind"}, "status"),
        ({"type": "config.update"}, "status"),
        ({"type": "tools.set_active_tools"}, "status"),
        ({"type": "plugin.session_start"}, "status"),
        ({"type": "mcp.tools_discovered"}, "status"),
        ({"type": "task.started"}, "background_task"),
        ({"type": "task.terminated"}, "background_task"),
        ({"type": "swarm_mode.enter"}, "status"),
        ({"type": "goal.create"}, "status"),
        ({"type": "forked"}, "status"),
        ({"type": "llm.request"}, ""),
        ({"type": "llm.tools_snapshot"}, ""),
        # loop events dispatch on the nested event type
        (
            {"type": "context.append_loop_event", "event": {"type": "step.begin"}},
            "step_begin",
        ),
        (
            {"type": "context.append_loop_event", "event": {"type": "step.end"}},
            "step_end",
        ),
        (
            {"type": "context.append_loop_event", "event": {"type": "tool.call"}},
            "tool_call",
        ),
        (
            {"type": "context.append_loop_event", "event": {"type": "tool.result"}},
            "tool_result",
        ),
        (
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "part": {"type": "text", "text": "hi"},
                },
            },
            "message",
        ),
        (
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "part": {"type": "think", "think": "hmm"},
                },
            },
            "reasoning",
        ),
        # messages dispatch on the role
        ({"type": "context.append_message", "message": {"role": "user"}}, "message"),
        ({"type": "context.append_message", "message": {"role": "system"}}, "message"),
        (
            {"type": "context.append_message", "message": {"role": "tool"}},
            "tool_result",
        ),
        ({"type": "context.append_message", "message": {"role": "assistant"}}, ""),
        # unknown members of a control family are status, everything else is a
        # message (the parser.py:37 fallback)
        ({"type": "tools.something_new"}, "status"),
        ({"type": "tower_mode.enter"}, "status"),
        ({"type": "runtime.set_binding"}, "status"),
        ({"type": "prompt.accepted"}, "status"),
        ({"type": "brand.new.thing"}, "message"),
    ],
)
def test_map_wire_kind(record: dict[str, Any], expected: str) -> None:
    assert map_wire_kind(record) == expected


def test_monitor_kind_overrides_cover_the_two_unrenderable_kinds() -> None:
    """The live monitor's kind set has no ``reasoning`` / ``background_task``."""

    assert MONITOR_KIND_OVERRIDES == {
        "reasoning": "message",
        "background_task": "status",
    }


# ── session reading ──────────────────────────────────────────────────────


def test_read_session_subagent_parent_links() -> None:
    session = read_kimi_code_session(_session_dir("subagent"))

    assert isinstance(session, KimiCodeSession)
    assert list(session.agents) == ["main", "agent-0"]  # main always first
    assert len(session.agents["main"]) == 20
    assert len(session.agents["agent-0"]) == 21
    assert session.parents == {"main": None, "agent-0": "main"}
    assert session.agent_types == {"main": "main", "agent-0": "sub"}
    assert session.protocol_versions == {"main": "1.5", "agent-0": "1.5"}
    # state.json createdAt is epoch ms on the v2 engine.
    assert session.created_at == pytest.approx(1786981802.683)


def test_read_session_swarm_labels_and_ordering() -> None:
    """``state.json`` key order is not sorted — the reader imposes one."""

    raw = json.loads((_session_dir("swarm") / "state.json").read_text(encoding="utf-8"))
    assert list(raw["agents"]) == ["main", "agent-1", "agent-0"]

    session = read_kimi_code_session(_session_dir("swarm"))

    assert list(session.agents) == ["main", "agent-0", "agent-1"]
    assert session.labels["agent-0"] == {"parentAgentId": "main", "swarmItem": "alpha"}
    assert session.labels["agent-1"] == {"parentAgentId": "main", "swarmItem": "beta"}


def test_read_session_tolerates_iso_state_timestamps() -> None:
    """The legacy engine writes ISO strings where v2 writes epoch-ms ints."""

    session = read_kimi_code_session(VIS_SESSIONS / "sample-main")

    assert session.created_at is not None
    assert session.created_at == pytest.approx(1779256791.085)
    assert session.parents == {"main": None, "agent-0": "main"}
    assert len(session.agents["main"]) == 10
    assert len(session.agents["agent-0"]) == 2


def test_read_session_without_state_json_assumes_main_as_parent(tmp_path: Path) -> None:
    session_dir = tmp_path / "session_x"
    _write_wire(
        session_dir / "agents" / "main" / "wire.jsonl",
        [{"type": "metadata", "protocol_version": "1.5", "created_at": 1000}],
    )
    _write_wire(
        session_dir / "agents" / "agent-0" / "wire.jsonl",
        [{"type": "metadata", "protocol_version": "1.5", "created_at": 2000}],
    )

    session = read_kimi_code_session(session_dir)

    assert session.parents == {"main": None, "agent-0": "main"}
    assert any("state.json missing" in note for note in session.notes)
    assert session.created_at == pytest.approx(1.0)  # from main's metadata header


# ── normalization ────────────────────────────────────────────────────────


def test_normalize_solo_main_events() -> None:
    session = read_kimi_code_session(_session_dir("solo"))
    events, info = normalize_kimi_code_records(
        session.agents["main"], run_id="run_root_001"
    )

    assert _kinds(events) == [
        "status",  # profile.bind
        "status",  # permission.set_mode
        "turn_begin",  # turn.prompt
        "message",  # user message
        "message",  # auto-permission system reminder (origin injection)
        "status",  # plugin.session_start
        "step_begin",
        "status",  # usage.record
        "tool_call",
        "tool_result",
        "step_end",
        "step_begin",
        "status",  # usage.record
        "message",  # content.part text
        "step_end",
        "turn_end",
    ]
    # metadata / llm.request / llm.tools_snapshot are dropped.
    assert len(events) == 16

    assert [event["seq"] for event in events] == list(range(1, 17))
    assert [event["event_id"] for event in events][:2] == [
        "run_root_001_evt_0001",
        "run_root_001_evt_0002",
    ]
    assert all(event["parent_event_id"] is None for event in events)
    assert {event["phase"] for event in events} <= {"begin", "update", "completed"}

    # ms -> float seconds, and the header's created_at is separate from the
    # first record's timestamp.
    assert info["created_at"] == pytest.approx(1786981621.294)
    assert events[0]["timestamp"] == pytest.approx(1786981621.359)
    assert all(isinstance(event["timestamp"], float) for event in events)

    # step_id restarts nowhere: one running counter per run.
    assert [event["step_id"] for event in events if event["kind"] == "step_begin"] == [
        "run_root_001_step_1",
        "run_root_001_step_2",
    ]
    assert info["n_steps"] == 2
    assert info["n_turns"] == 1
    assert info["protocol_version"] == "1.5"
    assert info["profile_name"] == "agent"
    assert info["model_alias"] == "__kimi_env_model__"
    assert info["model_name"] == "probe-model"
    assert info["model_provider"] == "openai"
    assert info["last_turn_reason"] == "completed"
    assert info["notes"] == []


def test_normalize_step_begin_payload_uses_running_counter() -> None:
    """``event.step`` restarts at 1 every turn, so ``n`` must not be it.

    The subagent fixture's child has ``turnId "0"`` step 1 then ``turnId "1"``
    step 1 — a gap-free sequential id has to come from our own counter.
    """

    session = read_kimi_code_session(_session_dir("subagent"))
    events, _info = normalize_kimi_code_records(
        session.agents["agent-0"], run_id="run_sub_agent-0"
    )

    step_begins = _by_kind(events, "step_begin")
    assert [event["payload"]["native_step"] for event in step_begins] == [1, 1]
    assert [event["payload"]["turn_id"] for event in step_begins] == ["0", "1"]
    assert [event["payload"]["n"] for event in step_begins] == [1, 2]
    assert [event["step_id"] for event in step_begins] == [
        "run_sub_agent-0_step_1",
        "run_sub_agent-0_step_2",
    ]


def test_normalize_counts_tokens_from_step_end_only() -> None:
    """``usage.record`` repeats ``step.end``'s TokenUsage — never count both."""

    session = read_kimi_code_session(_session_dir("solo"))
    events, info = normalize_kimi_code_records(
        session.agents["main"], run_id="run_root_001"
    )

    usage_records = [
        event
        for event in events
        if event["kind"] == "status" and "token_usage" in (event["payload"] or {})
    ]
    assert len(usage_records) == 2
    assert all(event["metrics"] == {} for event in usage_records)

    step_ends = _by_kind(events, "step_end")
    assert [event["metrics"] for event in step_ends] == [
        {"prompt_tokens": 123, "completion_tokens": 45},
        {"prompt_tokens": 123, "completion_tokens": 45},
    ]
    # 23 inputOther + 100 inputCacheRead + 0 inputCacheCreation, twice.
    assert info["prompt_tokens"] == 246
    assert info["completion_tokens"] == 90
    assert info["wall_time_ms"] == 141


def test_normalize_counts_out_of_turn_usage_records() -> None:
    """Source-derived: no fixture compacts. ``usageScope`` is ``"turn"`` only
    for the turn loop's own requests, which already carry their usage on a
    ``step.end``. A full compaction runs its summarization through the same
    requester with a non-turn source, so it writes a ``usage.record`` with scope
    ``session`` and **no** ``step.end`` — dropping it under-reports the run by
    the whole compaction cost."""

    records = [
        {"type": "metadata", "protocol_version": "1.5", "created_at": 1_000_000},
        {"type": "context.append_loop_event", "event": {"type": "step.begin"}},
        {
            "type": "usage.record",
            "usageScope": "turn",
            "usage": {"inputOther": 100, "inputCacheRead": 5, "output": 10},
            "time": 1_000_100,
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "step.end",
                "finishReason": "end_turn",
                "usage": {"inputOther": 100, "inputCacheRead": 5, "output": 10},
            },
            "time": 1_000_200,
        },
        {
            "type": "usage.record",
            "usageScope": "session",
            "usage": {"inputOther": 180_000, "output": 4_000},
            "time": 1_000_300,
        },
    ]
    events, info = normalize_kimi_code_records(records, run_id="run_root_001")

    charged = [event for event in events if event["metrics"]]
    assert [event["metrics"] for event in charged] == [
        {"prompt_tokens": 105, "completion_tokens": 10},
        {"prompt_tokens": 180_000, "completion_tokens": 4_000},
    ]
    # The turn-scoped record stays uncharged (it duplicates the step.end).
    assert info["prompt_tokens"] == 180_105
    assert info["completion_tokens"] == 4_010


def test_normalize_tool_payloads_match_the_frontend_contract() -> None:
    session = read_kimi_code_session(_session_dir("solo"))
    events, _info = normalize_kimi_code_records(
        session.agents["main"], run_id="run_root_001"
    )

    call = _by_kind(events, "tool_call")[0]
    assert call["payload"]["id"] == "call_probe_1"
    assert call["payload"]["tool_call_id"] == "call_probe_1"
    assert call["payload"]["name"] == "Bash"
    assert call["payload"]["function"]["name"] == "Bash"
    assert json.loads(call["payload"]["function"]["arguments"]) == {"command": "ls -1"}
    assert call["phase"] == "begin"

    result = _by_kind(events, "tool_result")[0]
    # Both id keys: app.js accepts either, monitor/tasks.py needs tool_call_id.
    assert result["payload"]["id"] == "call_probe_1"
    assert result["payload"]["tool_call_id"] == "call_probe_1"
    assert result["payload"]["return_value"] == {"output": "README.txt\n"}
    assert "error" not in result["payload"]
    assert result["phase"] == "completed"


def test_normalize_turn_and_message_payloads() -> None:
    session = read_kimi_code_session(_session_dir("solo"))
    events, _info = normalize_kimi_code_records(
        session.agents["main"], run_id="run_root_001"
    )

    turn_begin = _by_kind(events, "turn_begin")[0]
    # There is no ``user_input`` field on disk; it is synthesized from ``input``.
    assert turn_begin["payload"]["user_input"].startswith("List the files")
    assert turn_begin["payload"]["input"][0]["type"] == "text"
    assert turn_begin["payload"]["origin"] == {"kind": "user"}
    assert turn_begin["extra"]["origin_kind"] == "user"

    injected = [
        event
        for event in events
        if event["kind"] == "message"
        and event["extra"].get("origin_kind") == "injection"
    ]
    assert injected and injected[0]["extra"]["origin_variant"] == "permission_mode"

    assistant_text = [
        event
        for event in _by_kind(events, "message")
        if (event["payload"].get("text") or "").startswith("Files listed")
    ]
    assert assistant_text and assistant_text[0]["payload"]["type"] == "text"

    turn_end = _by_kind(events, "turn_end")[0]
    assert turn_end["payload"] == {
        "reason": "completed",
        "turn_id": 0,
        "duration_ms": 127,
    }


def test_normalize_marks_subagent_turns_via_origin() -> None:
    """``origin.kind == system_trigger`` + ``name == subagent`` is the tell."""

    session = read_kimi_code_session(_session_dir("subagent"))
    events, _info = normalize_kimi_code_records(
        session.agents["agent-0"], run_id="run_sub_agent-0"
    )

    turn_begins = _by_kind(events, "turn_begin")
    assert [event["extra"]["origin_kind"] for event in turn_begins] == [
        "system_trigger",
        "system_trigger",
    ]
    assert turn_begins[0]["extra"]["origin_name"] == "subagent"


def test_spawn_synthesis_binds_the_child_agent_id_from_the_tool_result() -> None:
    """Foreground ``Agent`` results begin with ``agent_id: <id>`` — exact join."""

    session = read_kimi_code_session(_session_dir("subagent"))
    events, info = normalize_kimi_code_records(
        session.agents["main"],
        run_id="run_root_001",
        synthesize_subagent_spawns=True,
    )

    spawns = _by_kind(events, "subagent_spawn")
    assert len(spawns) == 1
    spawn = spawns[0]
    assert spawn["phase"] == "begin"
    assert spawn["payload"]["child_run_id"] == "run_sub_agent-0"
    assert spawn["payload"]["task_id"] == "agent-0"
    assert spawn["payload"]["subagent_type"] == "coder"
    assert spawn["payload"]["description"] == "probe sub"
    assert spawn["payload"]["prompt"] == "Say hello and stop."
    assert spawn["payload"]["launch_mode"] == "foreground"
    assert spawn["payload"]["parent_tool_call_id"] == "call_agent_1"
    assert spawn["payload"]["spawn_tool"] == "Agent"
    # The spawn is emitted just before its own tool_call event.
    assert events[events.index(spawn) + 1]["kind"] == "tool_call"
    assert info["spawn_events"]["agent-0"] is spawn
    assert info["child_launch_modes"] == {"agent-0": "foreground"}
    assert info["child_subagent_types"] == {"agent-0": "coder"}
    assert info["tool_calls"] == {"call_agent_1": "run_root_001"}


def test_spawn_binding_keeps_two_calls_that_share_a_tool_call_id_distinct() -> None:
    """Source-derived: ``toolCallId`` is provider-generated and only unique
    *within* a step (upstream tolerates per-response counter ids), so two
    ``Agent`` calls can share ``call_0``. Binding ``spawns[0]`` for every result
    gave the first spawn the second child and left the second unbound — i.e. one
    child with the wrong prompt and one with no ``source_event_id``."""

    def _agent_call(uuid: str, prompt: str) -> dict[str, Any]:
        return {
            "type": "context.append_loop_event",
            "event": {
                "type": "tool.call",
                "uuid": uuid,
                "toolCallId": "call_0",
                "name": "Agent",
                "args": {"prompt": prompt, "subagent_type": "coder"},
            },
            "time": 1_000_000,
        }

    def _agent_result(uuid: str, agent_id: str) -> dict[str, Any]:
        return {
            "type": "context.append_loop_event",
            "event": {
                "type": "tool.result",
                "parentUuid": uuid,
                "toolCallId": "call_0",
                "result": {"output": f"agent_id: {agent_id}\nstatus: completed"},
            },
            "time": 1_000_100,
        }

    events, info = normalize_kimi_code_records(
        [
            _agent_call("u1", "first"),
            _agent_result("u1", "agent-0"),
            _agent_call("u2", "second"),
            _agent_result("u2", "agent-1"),
        ],
        run_id="run_root_001",
        synthesize_subagent_spawns=True,
    )

    spawns = _by_kind(events, "subagent_spawn")
    assert [spawn["payload"]["child_run_id"] for spawn in spawns] == [
        "run_sub_agent-0",
        "run_sub_agent-1",
    ]
    assert [spawn["payload"]["prompt"] for spawn in spawns] == ["first", "second"]
    assert set(info["spawn_events"]) == {"agent-0", "agent-1"}


def test_spawn_synthesis_fans_out_one_event_per_swarm_item() -> None:
    session = read_kimi_code_session(_session_dir("swarm"))
    events, info = normalize_kimi_code_records(
        session.agents["main"],
        run_id="run_root_001",
        synthesize_subagent_spawns=True,
    )

    spawns = _by_kind(events, "subagent_spawn")
    assert len(spawns) == 2
    assert [spawn["payload"]["swarm_item"] for spawn in spawns] == ["alpha", "beta"]
    # Children are matched by item, not by position, and both come from the
    # single AgentSwarm result's <subagent agent_id=… item=…> entries.
    assert [spawn["payload"]["child_run_id"] for spawn in spawns] == [
        "run_sub_agent-0",
        "run_sub_agent-1",
    ]
    assert all(spawn["payload"]["spawn_tool"] == "AgentSwarm" for spawn in spawns)
    assert info["child_swarm_items"] == {"agent-0": "alpha", "agent-1": "beta"}


def test_spawn_synthesis_uses_task_records_for_background_delegation() -> None:
    """``task.started.info`` names ``parentToolCallId`` and ``agentId``."""

    session = read_kimi_code_session(_session_dir("background"))
    events, info = normalize_kimi_code_records(
        session.agents["main"],
        run_id="run_root_001",
        synthesize_subagent_spawns=True,
    )

    spawn = _by_kind(events, "subagent_spawn")[0]
    assert spawn["payload"]["child_run_id"] == "run_sub_agent-0"
    assert spawn["payload"]["launch_mode"] == "background"
    assert spawn["payload"]["native_task_id"] == "agent-g7xgrry2"
    assert info["child_launch_modes"] == {"agent-0": "background"}

    task_events = _by_kind(events, "background_task")
    assert [event["phase"] for event in task_events] == ["begin", "completed"]
    assert task_events[0]["payload"]["task_id"] == "agent-g7xgrry2"
    assert task_events[0]["payload"]["status"] == "running"
    assert task_events[1]["payload"]["status"] == "completed"
    assert task_events[1]["payload"]["output_tail"] == "reply 4. done."
    assert set(info["background_tasks"]) == {"agent-g7xgrry2"}


def test_normalize_without_the_flag_emits_no_spawn_events() -> None:
    session = read_kimi_code_session(_session_dir("subagent"))
    events, info = normalize_kimi_code_records(
        session.agents["main"], run_id="run_root_001"
    )

    assert _by_kind(events, "subagent_spawn") == []
    assert info["spawn_events"] == {}


def test_normalize_records_mcp_discovery() -> None:
    """The one record that proves the aos_pool MCP server wired up."""

    session = read_kimi_code_session(_session_dir("mcp_legacy"))
    events, info = normalize_kimi_code_records(
        session.agents["main"], run_id="run_root_001"
    )

    discovery = [
        event
        for event in events
        if (event["payload"] or {}).get("server_name") == "aos_pool"
    ]
    assert len(discovery) == 1
    assert discovery[0]["kind"] == "status"
    assert discovery[0]["payload"]["tool_names"] == ["todo_next", "todo_status"]
    # The full JSON Schemas of the discovered tools are not carried over.
    assert "tools" not in discovery[0]["payload"]
    assert info["mcp_servers"] == ["aos_pool"]


def test_normalize_warns_on_old_protocol_instead_of_migrating() -> None:
    session = read_kimi_code_session(VIS_SESSIONS / "sample-main")
    _events, info = normalize_kimi_code_records(
        session.agents["main"], run_id="run_root_001"
    )

    assert info["protocol_version"] == "1.1"
    assert any("protocol_version 1.1 < 1.5" in note for note in info["notes"])


def test_normalize_skips_assistant_messages_and_keeps_compaction() -> None:
    """``context.append_message`` role=assistant would double-count content."""

    session = read_kimi_code_session(VIS_SESSIONS / "sample-compaction")
    events, _info = normalize_kimi_code_records(
        session.agents["main"], run_id="run_root_001"
    )

    assert _kinds(events) == ["status", "message", "compaction_end", "message"]
    compaction = _by_kind(events, "compaction_end")[0]
    assert compaction["payload"] == {
        "summary": "compacted summary",
        "compactedCount": 2,
        "tokensBefore": 100,
        "tokensAfter": 30,
    }
    assert [event["payload"]["text"] for event in _by_kind(events, "message")] == [
        "before compaction",
        "after compaction",
    ]


# The following record shapes have no fixture (see the fixture README's
# "nothing here exercises …" list), so they are exercised from the upstream
# schemas instead.


def test_normalize_source_derived_shapes(tmp_path: Path) -> None:
    records = [
        {"type": "metadata", "protocol_version": "1.5", "created_at": 1_000_000},
        # ``time`` is optional on WireRecord — carry the last known value.
        {"type": "permission.set_mode", "mode": "auto", "time": 1_000_100},
        {"type": "plugin.session_start", "content": None},
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.begin", "uuid": "s1", "turnId": "0", "step": 1},
            "time": 1_000_200,
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "stepUuid": "s1",
                "part": {"type": "think", "think": "let me think"},
            },
            "time": 1_000_300,
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "stepUuid": "s1",
                "part": {
                    "type": "image_url",
                    "imageUrl": {"url": "blobref:image/png;abc123"},
                },
            },
            "time": 1_000_350,
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "tool.result",
                "toolCallId": "call_1",
                "result": {"output": "boom", "isError": True},
            },
            "time": 1_000_400,
        },
        {
            "type": "token_counting.measured",
            "length": 12,
            "tokens": 3456,
            "time": 1_000_500,
        },
        {
            "type": "interaction.request",
            "id": "int_1",
            "kind": "approval",
            "toolCallId": "call_1",
            "request": {"question": "ok?"},
            "time": 1_000_600,
        },
        {
            "type": "interaction.resolved",
            "id": "int_1",
            "response": "yes",
            "time": 1_000_700,
        },
        {"type": "turn.ended", "turnId": 0, "reason": "failed", "time": 1_000_800},
    ]
    session_dir = tmp_path / "session_src"
    _write_wire(session_dir / "agents" / "main" / "wire.jsonl", records)

    session = read_kimi_code_session(session_dir)
    events, info = normalize_kimi_code_records(
        session.agents["main"], run_id="run_root_001"
    )

    assert _kinds(events) == [
        "status",
        "status",
        "step_begin",
        "reasoning",
        "message",
        "tool_result",
        "status",
        "approval_request",
        "approval_response",
        "turn_end",
    ]
    # plugin.session_start has no ``time`` — it inherits the previous record's.
    assert events[1]["timestamp"] == pytest.approx(1000.1)

    reasoning = _by_kind(events, "reasoning")[0]
    assert reasoning["payload"] == {"type": "think", "think": "let me think"}

    # Reasoning emissions are counted because kimi-code drops the upstream
    # reasoning TOKEN count: the CLI remaps usage into its own four-field
    # {inputOther, output, inputCacheRead, inputCacheCreation} and logs only
    # request metadata (hashes, no bodies), so reasoning_tokens is
    # unrecoverable from the journal even though the API returns it. The think
    # text survives, so the count is the available signal — a count of
    # emissions, never comparable to codex's reasoning_output_tokens.
    assert info["n_thinking_parts"] == 1

    image = [
        event
        for event in _by_kind(events, "message")
        if event["payload"]["type"] == "image_url"
    ]
    # blobref: strings are left unrehydrated in v1.
    assert image[0]["payload"]["part"]["imageUrl"]["url"] == "blobref:image/png;abc123"

    failed_result = _by_kind(events, "tool_result")[0]
    assert failed_result["payload"]["error"] is True
    assert failed_result["payload"]["return_value"]["is_error"] is True

    counting = _by_kind(events, "status")[-1]
    assert counting["payload"]["context_tokens"] == 3456
    assert counting["payload"]["context_length"] == 12

    approval = _by_kind(events, "approval_request")[0]
    assert approval["payload"]["interaction_kind"] == "approval"
    assert approval["phase"] == "update"

    turn_end = _by_kind(events, "turn_end")[0]
    assert turn_end["payload"]["error"] is True  # the error-detector marker
    assert info["last_turn_reason"] == "failed"


# ── AgentTrace assembly ──────────────────────────────────────────────────


def _assert_agent_trace_contract(trace: dict[str, Any]) -> None:
    """The §17 minimum subset plus the fields the SVG renderer needs."""

    for key in ("schema_version", "trace_id", "agent_system", "root_run_id", "runs"):
        assert trace[key], f"missing top-level {key}"
    assert trace["schema_version"] == "AgentTrace-v0.1"
    assert trace["agent_system"]["name"] == "kimi-code"
    assert trace["root_run_id"] == "run_root_001"

    run_ids = {run["run_id"] for run in trace["runs"]}
    assert "run_root_001" in run_ids

    for run in trace["runs"]:
        for key in ("run_id", "role", "agent_name", "status", "events"):
            assert run.get(key) is not None, f"{run['run_id']} missing {key}"
        assert run["role"] in {"root", "subagent", "system"}
        assert run["launch_mode"] in {"foreground", "background", "inline"}
        assert run["session_mode"] in {"same_session", "child_session", "unknown"}
        if run["parent_run_id"] is not None:
            assert run["parent_run_id"] in run_ids
        assert set(run["timing"]) == {"created_at", "started_at", "ended_at"}

        seqs = [event["seq"] for event in run["events"]]
        assert seqs == sorted(seqs), f"{run['run_id']} seq not monotonic"
        assert len({event["event_id"] for event in run["events"]}) == len(run["events"])
        for event in run["events"]:
            for key in ("event_id", "seq", "kind", "payload"):
                assert key in event
            assert event["kind"] in ALLOWED_KINDS
            assert event["phase"] in {"begin", "update", "completed"}
            # P11: non-float timestamps are silently dropped by the renderer.
            assert event["timestamp"] is None or isinstance(event["timestamp"], float)
            assert isinstance(event["payload"], dict)
            assert event["raw_ref"]["source"] in {"kimi-code-wire", "kimi-code-task"}


@pytest.mark.parametrize("case", LIVE_CASES)
def test_collect_agent_trace_contract_on_every_live_fixture(case: str) -> None:
    trace = collect_kimi_code_agent_trace(
        _session_dir(case),
        trace_id=f"trace_{case}",
        model_name="stub-model",
        agent_version="0.36.1",
    )

    _assert_agent_trace_contract(trace)
    assert trace["trace_id"] == f"trace_{case}"
    assert trace["agent_system"]["version"] == "0.36.1"
    root = trace["runs"][0]
    assert root["run_id"] == "run_root_001"
    assert root["role"] == "root"
    assert root["agent_name"] == "orchestrator"
    assert root["launch_mode"] == "inline"
    assert root["session_mode"] == "same_session"
    assert root["parent_run_id"] is None
    # Timing + resource usage must be populated or the SVG renders empty lanes.
    assert root["timing"]["started_at"] is not None
    assert root["resource_usage"]["prompt_tokens"] > 0
    assert root["resource_usage"]["completion_tokens"] > 0
    assert root["resource_usage"]["wall_time_ms"] >= 0


def test_collect_agent_trace_subagent_run() -> None:
    trace = collect_kimi_code_agent_trace(
        _session_dir("subagent"), model_name="stub-model"
    )

    assert [run["run_id"] for run in trace["runs"]] == [
        "run_root_001",
        "run_sub_agent-0",
    ]
    root, sub = trace["runs"]

    spawn = _by_kind(root["events"], "subagent_spawn")[0]
    assert sub["parent_run_id"] == "run_root_001"
    assert sub["source_event_id"] == spawn["event_id"]
    assert sub["role"] == "subagent"
    assert sub["agent_name"] == "coder"
    assert sub["launch_mode"] == "foreground"
    assert sub["session_mode"] == "child_session"
    assert sub["status"] == "completed"
    assert sub["task"] == {
        "description": "probe sub",
        "prompt": "Say hello and stop.",
        "task_id": "agent-0",
        "subagent_type": "coder",
    }
    assert sub["extra"]["native_agent_id"] == "agent-0"
    assert sub["extra"]["native_profile_name"] == "coder"
    assert sub["extra"]["native_protocol_version"] == "1.5"
    assert trace["summary"]["subagent_count_total"] == 1
    assert trace["notes"] == ""


def test_collect_agent_trace_status_is_per_turn_not_sticky(tmp_path: Path) -> None:
    """A run SIGKILLed mid-turn is ``running``, not ``completed``.

    ``turn.ended{reason:'completed'}`` fires after EVERY turn (the background
    fixture has two), and harbor's ``--agent-timeout-sec`` SIGKILL mid-turn is
    the normal termination path for this arm — so a journal that stops inside a
    later turn must not inherit the first turn's verdict.
    """

    source = _session_dir("background")
    records = [
        parse_kimi_code_wire_line(line)
        for line in (source / "agents" / "main" / "wire.jsonl").read_text().splitlines()
    ]
    records = [record for record in records if record is not None]
    # …/turn.ended(turn 0) / task.terminated / turn.prompt(turn 1) / step.begin
    turn_two_prompt = (
        next(
            index
            for index, record in enumerate(records)
            if record["type"] == "turn.prompt"
        )
        + 1
    )
    cut = next(
        index
        for index, record in enumerate(records[turn_two_prompt:], turn_two_prompt)
        if record["type"] == "turn.prompt"
    )

    killed = tmp_path / "killed"
    _write_wire(killed / "agents" / "main" / "wire.jsonl", records[: cut + 2])
    assert collect_kimi_code_agent_trace(killed)["runs"][0]["status"] == "running"

    # The same journal read whole still ends ``completed``.
    whole = tmp_path / "whole"
    _write_wire(whole / "agents" / "main" / "wire.jsonl", records)
    assert collect_kimi_code_agent_trace(whole)["runs"][0]["status"] == "completed"


def test_collect_agent_trace_swarm_lanes() -> None:
    trace = collect_kimi_code_agent_trace(_session_dir("swarm"))

    assert [run["run_id"] for run in trace["runs"]] == [
        "run_root_001",
        "run_sub_agent-0",
        "run_sub_agent-1",
    ]
    lane_names = [run["agent_name"] for run in trace["runs"][1:]]
    assert lane_names == ["coder:alpha", "coder:beta"]
    source_ids = [run["source_event_id"] for run in trace["runs"][1:]]
    assert len(set(source_ids)) == 2  # one spawn event each
    assert trace["runs"][1]["task"]["swarm_item"] == "alpha"
    assert trace["summary"]["subagent_count_total"] == 2


def test_collect_agent_trace_background_task_artifacts() -> None:
    trace = collect_kimi_code_agent_trace(_session_dir("background"))
    root, sub = trace["runs"]

    assert sub["launch_mode"] == "background"
    assert trace["summary"]["background_task_count"] == 1
    # The lifecycle came from the journal, so the sidecar adds artifacts only —
    # never a duplicate event for the same task id.
    task_events = _by_kind(root["events"], "background_task")
    assert len(task_events) == 2
    assert all(event["raw_ref"]["source"] == "kimi-code-wire" for event in task_events)

    kinds = {artifact["kind"] for artifact in root["artifacts"]}
    assert {
        "native_agent_wire",
        "native_background_task",
        "native_background_task_output",
    } <= kinds


def test_collect_agent_trace_reads_orphan_task_sidecars(tmp_path: Path) -> None:
    """A task whose ``task.*`` records never landed still shows up.

    That is what a crash or a torn final line looks like; ownership is the
    directory, so no owner-resolution chain is needed to attribute it.
    """

    session_dir = tmp_path / "session_orphan"
    _write_wire(
        session_dir / "agents" / "main" / "wire.jsonl",
        [
            {"type": "metadata", "protocol_version": "1.5", "created_at": 1_000_000},
            {
                "type": "turn.prompt",
                "input": [{"type": "text", "text": "go"}],
                "time": 1_000_100,
            },
        ],
    )
    tasks_dir = session_dir / "agents" / "main" / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "shell-1a2b3c4d.json").write_text(
        json.dumps(
            {
                "taskId": "shell-1a2b3c4d",
                "kind": "process",
                "description": "pytest -q",
                "status": "completed",
                "startedAt": 1_000_200,
                "endedAt": 1_002_200,
                "exitCode": 0,
                "command": "pytest -q",
            }
        ),
        encoding="utf-8",
    )
    (tasks_dir / "shell-1a2b3c4d").mkdir()
    (tasks_dir / "shell-1a2b3c4d" / "output.log").write_text("ok\n", encoding="utf-8")

    trace = collect_kimi_code_agent_trace(session_dir)
    _assert_agent_trace_contract(trace)

    task_events = _by_kind(trace["runs"][0]["events"], "background_task")
    assert len(task_events) == 1
    assert task_events[0]["raw_ref"]["source"] == "kimi-code-task"
    assert task_events[0]["payload"]["task_id"] == "shell-1a2b3c4d"
    assert task_events[0]["metrics"]["wall_time_ms"] == 2000
    assert trace["summary"]["background_task_count"] == 1


def test_collect_agent_trace_legacy_engine_run() -> None:
    trace = collect_kimi_code_agent_trace(_session_dir("mcp_legacy"))

    _assert_agent_trace_contract(trace)
    assert trace["summary"]["mcp_servers"] == ["aos_pool"]
    assert "protocol_version 1.4 < 1.5" in trace["notes"]
    assert "legacy-engine vocabulary detected" in trace["notes"]
    # No turn.ended on the legacy engine; the last step.end finish reason
    # settles the status instead.
    assert trace["runs"][0]["status"] == "completed"


def test_collect_agent_trace_creates_lanes_from_directories() -> None:
    """P2: a lane exists on first sight of the directory, spawn marker or not."""

    session = read_kimi_code_session(_session_dir("swarm"))
    trace = collect_kimi_code_agent_trace(_session_dir("swarm"))

    assert len(trace["runs"]) == len(session.agents)


def test_collect_agent_trace_renders_as_svg() -> None:
    trace = collect_kimi_code_agent_trace(
        _session_dir("background"), model_name="stub-model"
    )

    svg = render_agent_trace_svg(trace, title="kimi-code background")

    assert svg.startswith("<svg")
    assert "run_sub_agent-0" in svg
    assert "kimi-code" in svg


def test_collect_agent_trace_missing_session_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        collect_kimi_code_agent_trace(tmp_path / "nope")
