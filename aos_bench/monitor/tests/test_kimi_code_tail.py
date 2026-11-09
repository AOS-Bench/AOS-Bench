"""Tests for :class:`KimiCodeSessionTail` — the live tail of a kimi-code trial.

The record shapes below are the ones the published 0.36.1 CLI actually writes
(cross-checked against the landed ground-truth fixtures under
``aos_bench/tests/fixtures/kimi_code/live_probe/``); they are hand-written here
rather than copied so a single test can exercise the root lane, a delegated
child lane, a dispatcher ``todo_next`` round-trip and a resume in one tree.

Each test names the parity item from ``docs/tasks/kimi_code_integration.md`` §4
it pins (P1..P7). ``_materialize_kimi_code_trial`` builds the full harbor
nesting — ``jobs/<trial>/<trial>/<task>__<hash>/agent/kimi-code-home/…`` — so
the home-dir glob, the ``session_index.jsonl`` re-rooting (the recorded path is
the *container* path ``/logs/agent/kimi-code-home/…``) and the reward walk-up
are all exercised the way they will be on a real run.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.kimi_code import KimiCodeSessionTail, _find_home_dir

# The landed ground-truth fixtures: real ``kimi -p`` runs of the published
# 0.36.1 CLI against a local stub model (``live_probe/README.md``).
FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures" / "kimi_code"

# The workspace key is ``wd_<slug>_<sha256(path)[:12]>``; the digest is opaque
# to the monitor, so a fixed stand-in keeps the tree readable.
WORKDIR_KEY = "wd_app_000000000000"
SESSION_ID = "session_11111111-2222-3333-4444-555555555555"
TASK_DIRNAME = "mixed-5-1__abcd"
# Container-side home, as the CLI records it in ``session_index.jsonl``.
REMOTE_HOME = "/logs/agent/kimi-code-home"


# ── record builders ────────────────────────────────────────────────────


def _loop(event: dict[str, Any], time_ms: int) -> dict[str, Any]:
    return {"type": "context.append_loop_event", "event": event, "time": time_ms}


def _main_records() -> list[dict[str, Any]]:
    """A root journal: one turn that claims a task, delegates it, and ends.

    ``mcp__aos_pool__todo_next`` is the dispatcher round-trip the task panel is
    rebuilt from (P5); the ``Agent`` call plus its result is the exact
    tool-call → child-agent join (``agent_id: agent-0`` in the result text).
    """

    return [
        {"type": "metadata", "protocol_version": "1.5", "created_at": 1786981621294},
        {
            "type": "profile.bind",
            "profileName": "agent",
            "modelAlias": "__kimi_env_model__",
            "activeToolNames": ["Agent", "Bash", "mcp__*"],
            "time": 1786981621300,
        },
        {
            "type": "llm.request",
            "provider": "kimi",
            "model": "kimi-k2-0905-preview",
            "modelAlias": "__kimi_env_model__",
            "time": 1786981621400,
        },
        {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "work the stream"}],
            "origin": {"kind": "user"},
            "time": 1786981621500,
        },
        _loop({"type": "step.begin", "turnId": "0", "step": 1}, 1786981621600),
        _loop(
            {
                "type": "tool.call",
                "toolCallId": "call_todo_1",
                "name": "mcp__aos_pool__todo_next",
                "args": {},
            },
            1786981621700,
        ),
        _loop(
            {
                "type": "tool.result",
                "toolCallId": "call_todo_1",
                "result": {
                    "output": json.dumps(
                        {"task_id": "task-1", "prompt": "fix the build"}
                    )
                },
            },
            1786981621800,
        ),
        _loop(
            {
                "type": "tool.call",
                "toolCallId": "call_agent_1",
                "name": "Agent",
                "args": {
                    "subagent_type": "coder",
                    "description": "fix the build",
                    "prompt": "fix the build in /app",
                },
            },
            1786981621900,
        ),
        _loop(
            {
                "type": "tool.result",
                "toolCallId": "call_agent_1",
                "result": {
                    "output": (
                        "agent_id: agent-0\n"
                        "actual_subagent_type: coder\n"
                        "status: completed\n\n"
                        "[summary]\nbuild fixed."
                    )
                },
            },
            1786981622000,
        ),
        _loop(
            {
                "type": "step.end",
                "turnId": "0",
                "step": 1,
                "finishReason": "end_turn",
                "usage": {
                    "inputOther": 100,
                    "inputCacheRead": 50,
                    "inputCacheCreation": 0,
                    "output": 20,
                },
            },
            1786981622100,
        ),
        {
            "type": "turn.ended",
            "turnId": 0,
            "reason": "completed",
            "durationMs": 800,
            "time": 1786981622200,
        },
    ]


def _child_records(profile_name: str = "coder") -> list[dict[str, Any]]:
    return [
        {"type": "metadata", "protocol_version": "1.5", "created_at": 1786981621950},
        {
            "type": "profile.bind",
            "profileName": profile_name,
            "modelAlias": "__kimi_env_model__",
            "time": 1786981621960,
        },
        _loop({"type": "step.begin", "turnId": "0", "step": 1}, 1786981621970),
        _loop(
            {"type": "content.part", "part": {"type": "text", "text": "build fixed."}},
            1786981621980,
        ),
        _loop(
            {
                "type": "step.end",
                "turnId": "0",
                "step": 1,
                "finishReason": "end_turn",
                "usage": {"inputOther": 40, "output": 8},
            },
            1786981621990,
        ),
        {
            "type": "turn.ended",
            "turnId": 0,
            "reason": "completed",
            "time": 1786981621995,
        },
    ]


def _state_json(
    *,
    children: dict[str, dict[str, Any]],
    session_id: str = SESSION_ID,
) -> dict[str, Any]:
    """A v2 ``state.json``: epoch-ms ints, per-child ``labels.parentAgentId``.

    ``agents`` key order is deliberately NOT sorted (the real swarm fixture
    lists ``agent-1`` before ``agent-0``) — ordering is the reader's job.
    """

    agents: dict[str, Any] = {"main": {"homedir": "…/agents/main", "type": "main"}}
    for agent_id, labels in children.items():
        agents[agent_id] = {
            "homedir": f"…/agents/{agent_id}",
            "type": "sub",
            "parentAgentId": "main",
            "labels": {"parentAgentId": "main", **labels},
        }
    return {
        "id": session_id,
        "version": 2,
        "cwd": "/app",
        "createdAt": 1786981621294,
        "updatedAt": 1786981622200,
        "archived": False,
        "agents": agents,
        "custom": {},
        "lastTurnReason": "completed",
        "isCustomTitle": False,
    }


# ── the trial materializer ─────────────────────────────────────────────


def _write_journal(
    session_dir: Path, agent_id: str, records: list[dict[str, Any]]
) -> None:
    path = session_dir / "agents" / agent_id / "wire.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _materialize_kimi_code_trial(
    jobs_dir: Path,
    trial_id: str,
    *,
    with_child: bool = True,
    child_in_state: bool = True,
    session_id: str = SESSION_ID,
    write_index: bool = True,
) -> Path:
    """Build the full harbor nesting for one kimi-code trial and return its dir.

    ``jobs/<trial>/<trial>/<task>__<hash>/agent/kimi-code-home/sessions/
    wd_app_000000000000/session_…/agents/{main,agent-0}/wire.jsonl`` plus the
    session's ``state.json`` and the home-level ``session_index.jsonl``.

    ``child_in_state=False`` drops ``agent-0`` from ``state.json`` while keeping
    its directory — the real race where a journal exists before the atomically
    rewritten ``state.json`` mentions it.
    """

    trial_dir = jobs_dir / trial_id
    home = trial_dir / trial_id / TASK_DIRNAME / "agent" / "kimi-code-home"
    session_dir = home / "sessions" / WORKDIR_KEY / session_id
    session_dir.mkdir(parents=True)

    _write_journal(session_dir, "main", _main_records())
    children: dict[str, dict[str, Any]] = {}
    if with_child:
        _write_journal(session_dir, "agent-0", _child_records())
        if child_in_state:
            children["agent-0"] = {}
    (session_dir / "state.json").write_text(
        json.dumps(_state_json(children=children, session_id=session_id))
    )
    if write_index:
        # The recorded ``sessionDir`` is the path the *agent* saw; the tail has
        # to re-root it onto the host-side home before trusting it.
        (home / "session_index.jsonl").write_text(
            json.dumps(
                {
                    "sessionId": session_id,
                    "sessionDir": f"{REMOTE_HOME}/sessions/{WORKDIR_KEY}/{session_id}",
                    "workDir": "/app",
                }
            )
            + "\n"
        )
    return trial_dir


def _tail(trial_dir: Path, trial_id: str, state: MonitorState) -> KimiCodeSessionTail:
    return KimiCodeSessionTail(
        trial_id=trial_id,
        trial_dir=trial_dir,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=0.1,
    )


def _drive(tail: KimiCodeSessionTail, seconds: float = 1.0) -> None:
    async def run() -> None:
        await tail.start()
        await asyncio.sleep(seconds)
        await tail.stop()

    asyncio.run(run())


# ── home-dir resolution ────────────────────────────────────────────────


def test_find_home_dir_prefers_the_shallowest_match(tmp_path: Path) -> None:
    trial = tmp_path / "jobs" / "t"
    shallow = trial / "agent" / "kimi-code-home"
    shallow.mkdir(parents=True)
    # A deeper decoy (e.g. an archived copy inside the agent's own workspace).
    (trial / "agent" / "workspace" / "copy" / "kimi-code-home").mkdir(parents=True)
    assert _find_home_dir(trial) == shallow


def test_find_home_dir_accepts_an_empty_home(tmp_path: Path) -> None:
    """The home dir is the arm's marker and the adapter creates it before the
    CLI writes anything — an empty one must still resolve so
    ``_maybe_detect_crashed`` can report an install failure."""
    trial = tmp_path / "jobs" / "t"
    (trial / "agent" / "kimi-code-home").mkdir(parents=True)
    assert _find_home_dir(trial) is not None
    assert _find_home_dir(tmp_path / "jobs" / "nothing-here") is None


# ── the byte-real session ──────────────────────────────────────────────


def test_tail_reads_the_byte_real_swarm_fixture(tmp_path: Path) -> None:
    """The whole tail over an unedited ``kimi -p`` session (0.36.1, default v2
    engine): ``live_probe/swarm`` is one ``AgentSwarm`` call with items
    ``[alpha, beta]`` and the two child journals it produced.

    This is the test that would catch a shape assumption the hand-written
    records above quietly get right — the ``session_index.jsonl`` in the fixture
    still records the *probe host's* absolute path, so it also exercises the
    re-rooting onto our home dir.
    """

    jobs = tmp_path / "jobs"
    trial_dir = jobs / "t-real"
    agent_dir = trial_dir / "t-real" / TASK_DIRNAME / "agent"
    agent_dir.mkdir(parents=True)
    shutil.copytree(FIXTURES / "live_probe" / "swarm", agent_dir / "kimi-code-home")

    state = MonitorState(jobs_dir=str(jobs))
    _drive(_tail(trial_dir, "t-real", state))

    trial = state.trials["t-real"]
    assert set(trial.runs) == {
        "run_root_001",
        "run_sub_agent-0",
        "run_sub_agent-1",
    }
    # P3: profile name from the child's own ``profile.bind``, suffixed with the
    # swarm item from ``state.json``'s labels.
    assert trial.runs["run_sub_agent-0"].agent_name == "coder:alpha"
    assert trial.runs["run_sub_agent-1"].agent_name == "coder:beta"
    assert trial.runs["run_sub_agent-1"].parent_run_id == "run_root_001"

    root = trial.runs["run_root_001"]
    # One spawn per ``items[]`` entry, each bound to its child by the
    # ``<subagent agent_id=… item=…>`` result text.
    spawns = [e for e in root.structural_events if e["kind"] == "subagent_spawn"]
    assert [e["payload"]["child_run_id"] for e in spawns] == [
        "run_sub_agent-0",
        "run_sub_agent-1",
    ]
    assert [e["payload"]["swarm_item"] for e in spawns] == ["alpha", "beta"]
    # P10: the lane header's stats are all populated from the real journal.
    assert root.n_llm_calls == 2
    assert root.n_tool_calls == 1
    assert root.prompt_tokens > 0
    assert root.completion_tokens > 0
    assert all(run.status == "completed" for run in trial.runs.values())


# ── P2 / P3: lane creation, routing and labels ─────────────────────────


def test_tail_routes_root_and_child_lanes(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_id = "mixed-5-1__abcd"
    trial_dir = _materialize_kimi_code_trial(jobs, trial_id)

    state = MonitorState(jobs_dir=str(jobs))
    _drive(_tail(trial_dir, trial_id, state))

    trial = state.trials[trial_id]
    assert trial.agent == "kimi-code"
    assert set(trial.runs) == {"run_root_001", "run_sub_agent-0"}

    root = trial.runs["run_root_001"]
    assert root.role == "root"
    assert root.parent_run_id is None
    # P3: the root is always ``orchestrator`` — every kimi-code root binds the
    # profile ``agent``, which says nothing.
    assert root.agent_name == "orchestrator"
    assert root.n_tool_calls == 2  # todo_next + Agent
    assert root.n_llm_calls == 1  # one step.begin
    # P10: tokens come from step.end only (usage.record repeats the same object).
    assert root.prompt_tokens == 150
    assert root.completion_tokens == 20
    assert root.model_name == "kimi-k2-0905-preview"

    child = trial.runs["run_sub_agent-0"]
    assert child.role == "subagent"
    assert child.parent_run_id == "run_root_001"
    # P3: a child is labelled with its own bound profile.
    assert child.agent_name == "coder"
    assert child.n_llm_calls == 1
    assert child.completion_tokens == 8


def test_root_lane_carries_the_backfilled_spawn_marker(tmp_path: Path) -> None:
    """P13: ``subagent_spawn`` lands in the uncapped structural skeleton, and the
    child id is back-filled in place from the ``Agent`` result text."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-spawn")
    state = MonitorState(jobs_dir=str(jobs))
    _drive(_tail(trial_dir, "t-spawn", state))

    root = state.trials["t-spawn"].runs["run_root_001"]
    spawns = [e for e in root.structural_events if e["kind"] == "subagent_spawn"]
    assert len(spawns) == 1
    assert spawns[0]["payload"]["child_run_id"] == "run_sub_agent-0"
    assert spawns[0]["payload"]["subagent_type"] == "coder"
    # turn_begin / turn_end are structural too, so the skeleton keeps the
    # boundaries even after events_tail rolls over.
    assert {e["kind"] for e in root.structural_events} >= {
        "subagent_spawn",
        "turn_begin",
        "turn_end",
    }


def test_child_lane_appears_without_a_state_json_entry(tmp_path: Path) -> None:
    """P2: the lane comes from the *directory listing*, so a child whose
    ``state.json`` entry has not been written yet still gets a lane, parented on
    the root (which is what every observed session does anyway)."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-race", child_in_state=False)
    state = MonitorState(jobs_dir=str(jobs))
    _drive(_tail(trial_dir, "t-race", state))

    child = state.trials["t-race"].runs["run_sub_agent-0"]
    assert child.role == "subagent"
    assert child.parent_run_id == "run_root_001"


def test_agent_dir_created_later_is_picked_up_on_a_later_tick(tmp_path: Path) -> None:
    """P1 / P2: ``agents/*`` is re-scanned every tick, so a child that starts
    after the first poll still gets a lane (``KimiSessionTail`` re-scans
    ``subagents/`` but binds its session dir once; both must be re-checked)."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-late", with_child=False)
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-late", state)

    state.ensure_trial("t-late", str(trial_dir), agent="kimi-code")
    tail._tick()
    assert set(state.trials["t-late"].runs) == {"run_root_001"}

    session_dir = tail._session_dir
    assert session_dir is not None
    _write_journal(session_dir, "agent-1", _child_records("explore"))
    tail._tick()

    child = state.trials["t-late"].runs["run_sub_agent-1"]
    assert child.agent_name == "explore"
    assert child.parent_run_id == "run_root_001"


def test_state_json_reread_refreshes_the_swarm_lane_label(tmp_path: Path) -> None:
    """P3 / P4: ``state.json`` is re-read whenever its mtime moves, so a swarm
    child picks up ``labels.swarmItem`` on the tick after the rewrite — the lane
    label is updated in place on the existing run."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-swarm")
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-swarm", state)

    state.ensure_trial("t-swarm", str(trial_dir), agent="kimi-code")
    tail._tick()
    assert state.trials["t-swarm"].runs["run_sub_agent-0"].agent_name == "coder"

    session_dir = tail._session_dir
    assert session_dir is not None
    state_path = session_dir / "state.json"
    state_path.write_text(
        json.dumps(_state_json(children={"agent-0": {"swarmItem": "alpha"}}))
    )
    # The mtime gate is what triggers the re-read; tmpfs mtimes can be coarse
    # enough that two writes in the same test share one stamp.
    stat = state_path.stat()
    os.utime(state_path, (stat.st_atime + 2, stat.st_mtime + 2))
    tail._tick()

    assert state.trials["t-swarm"].runs["run_sub_agent-0"].agent_name == "coder:alpha"


def test_state_json_reread_survives_a_frozen_change_stamp(tmp_path: Path) -> None:
    """P4, defensively: a filesystem with coarse mtime granularity can land the
    rewrite that *introduces* a child on the very stamp we already read. While a
    registered lane is still missing from the parsed copy the stamp is ignored,
    so that rewrite is never lost — the collision is simulated here by pinning
    the tail's stamp to the post-write value."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-frozen", child_in_state=False)
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-frozen", state)

    state.ensure_trial("t-frozen", str(trial_dir), agent="kimi-code")
    tail._tick()
    assert tail._lanes_missing_from_state() is True

    session_dir = tail._session_dir
    assert session_dir is not None
    state_path = session_dir / "state.json"
    state_path.write_text(
        json.dumps(_state_json(children={"agent-0": {"swarmItem": "beta"}}))
    )
    stat = state_path.stat()
    tail._state_stamp = (stat.st_mtime, stat.st_size)  # indistinguishable stamp
    tail._tick()

    assert tail._lanes_missing_from_state() is False
    assert state.trials["t-frozen"].runs["run_sub_agent-0"].agent_name == "coder:beta"


# ── P4: per-tick status refresh ────────────────────────────────────────


def test_lane_status_comes_from_the_journal(tmp_path: Path) -> None:
    """P4: ``state.json`` has no per-agent status field, so status is derived
    from ``turn.ended.reason`` (with the last ``step.end.finishReason`` as the
    legacy-engine fallback) exactly as the offline trace derives it."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-status")
    state = MonitorState(jobs_dir=str(jobs))
    _drive(_tail(trial_dir, "t-status", state))

    trial = state.trials["t-status"]
    assert trial.runs["run_root_001"].status == "completed"
    assert trial.runs["run_sub_agent-0"].status == "completed"


def test_lane_status_is_running_before_a_turn_ends(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-live", with_child=False)
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-live", state)
    state.ensure_trial("t-live", str(trial_dir), agent="kimi-code")

    # Truncate the root journal back to "mid-turn": header … first tool call.
    tail._tick()
    session_dir = tail._session_dir
    assert session_dir is not None
    _write_journal(session_dir, "agent-9", _main_records()[:6])
    tail._tick()

    assert state.trials["t-live"].runs["run_sub_agent-9"].status == "running"


def test_lane_status_returns_to_running_on_the_next_turn(tmp_path: Path) -> None:
    """P4 is a *liveness* signal: ``turn.ended{reason:'completed'}`` fires after
    EVERY turn, so a lane that latched on the first turn boundary would read
    ``completed`` for the rest of an hour-long trial while it is still
    scheduling. The second turn here is the background-completion turn the real
    ``live_probe/background`` journal has (``origin.kind == 'task'``)."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-turn2", with_child=False)
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-turn2", state)
    state.ensure_trial("t-turn2", str(trial_dir), agent="kimi-code")

    tail._tick()
    assert state.trials["t-turn2"].runs["run_root_001"].status == "completed"

    # Turn 2 opens (a background task completed and injected a prompt) and the
    # lane is working again.
    session_dir = tail._session_dir
    assert session_dir is not None
    journal = session_dir / "agents" / "main" / "wire.jsonl"
    with journal.open("a") as handle:
        for record in (
            {
                "type": "turn.prompt",
                "input": [{"type": "text", "text": "<notification …>"}],
                "origin": {"kind": "task", "taskId": "agent-xyz"},
                "time": 1786981623000,
            },
            _loop({"type": "step.begin", "turnId": "1", "step": 1}, 1786981623100),
        ):
            handle.write(json.dumps(record) + "\n")
    tail._tick()

    assert state.trials["t-turn2"].runs["run_root_001"].status == "running"


def test_lane_status_is_discovered_before_any_record(tmp_path: Path) -> None:
    """A lane whose directory exists but whose journal has not been flushed
    shows as ``discovered`` — AgentTrace's ``created`` has no dashboard colour,
    so it is spelled the way the other tails spell an empty lane."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-empty", with_child=False)
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-empty", state)
    state.ensure_trial("t-empty", str(trial_dir), agent="kimi-code")

    tail._tick()
    session_dir = tail._session_dir
    assert session_dir is not None
    (session_dir / "agents" / "agent-7").mkdir(parents=True)
    tail._tick()

    assert state.trials["t-empty"].runs["run_sub_agent-7"].status == "discovered"


# ── P1: the session dir is re-resolved every tick ─────────────────────


def test_resume_into_a_new_session_dir_keeps_the_lane_and_its_seq(
    tmp_path: Path,
) -> None:
    """P1: a ``kimi -r`` resume / fork writes a second ``session_<uuid>``. The
    newest session wins, the lane keeps its run_id *and* its parser, so event
    ids continue past the ones already pushed instead of colliding."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-resume", with_child=False)
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-resume", state)
    state.ensure_trial("t-resume", str(trial_dir), agent="kimi-code")

    tail._tick()
    first_session = tail._session_dir
    assert first_session is not None
    root = state.trials["t-resume"].runs["run_root_001"]
    events_after_first = root.n_events
    assert events_after_first > 0

    # Second session dir under the same workspace key, newer mtime, and the
    # index line the resume appends.
    home = first_session.parents[2]
    resumed_id = "session_99999999-8888-7777-6666-555555555555"
    resumed = home / "sessions" / WORKDIR_KEY / resumed_id
    resumed.mkdir(parents=True)
    _write_journal(resumed, "main", _main_records())
    (resumed / "state.json").write_text(
        json.dumps(_state_json(children={}, session_id=resumed_id))
    )
    with (home / "session_index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "sessionId": resumed_id,
                    "sessionDir": f"{REMOTE_HOME}/sessions/{WORKDIR_KEY}/{resumed_id}",
                    "workDir": "/app",
                }
            )
            + "\n"
        )

    tail._tick()

    assert tail._session_dir == resumed
    assert set(state.trials["t-resume"].runs) == {"run_root_001"}
    # The resumed journal was read from byte 0 into the SAME parser, so every
    # event id stayed unique — and the one event the second pass does NOT repeat
    # is the ``subagent_spawn``: the parser dedups spawn synthesis by tool-call
    # id, which is exactly the replay protection it exists for.
    assert root.n_events == 2 * events_after_first - 1
    ids = [e["event_id"] for e in root.events_tail]
    assert len(ids) == len(set(ids))


# ── P5: live dispatcher task-lifecycle extraction ─────────────────────


def test_root_events_feed_the_task_snapshot(tmp_path: Path) -> None:
    """P5: only ``KimiSessionTail`` keeps an uncapped root-event buffer +
    ``extract_task_lifecycle_from_events`` today; without it the dispatcher
    panel falls back to re-parsing the capped ``events_tail``."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-tasks")
    state = MonitorState(jobs_dir=str(jobs))
    _drive(_tail(trial_dir, "t-tasks", state))

    snapshot = state.trials["t-tasks"].latest_task_snapshot
    assert snapshot is not None
    assert [row["task_id"] for row in snapshot] == ["task-1"]


def test_root_event_buffer_is_bounded(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-buf", with_child=False)
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-buf", state)
    tail._all_root_events = [{"kind": "message"} for _ in range(10001)]
    tail._absorb_root_events([])
    assert len(tail._all_root_events) == 8000


# ── P6 / P7: finalization + the terminal-status guard ─────────────────


def test_crashed_when_harbor_dies_before_a_session_exists(tmp_path: Path) -> None:
    """P6: the pinned ``npm i -g @moonshot-ai/kimi-code`` failing leaves an
    ``exception.txt`` and no session dir; without this the trial would sit at
    ``discovered`` forever."""
    jobs = tmp_path / "jobs"
    trial_dir = jobs / "t-crash"
    (trial_dir / "agent" / "kimi-code-home").mkdir(parents=True)
    (trial_dir / "exception.txt").write_text("AgentSetupError: npm exited 1\n")

    state = MonitorState(jobs_dir=str(jobs))
    _drive(_tail(trial_dir, "t-crash", state), seconds=0.4)

    assert state.trials["t-crash"].status == "crashed"
    assert [e.rule_id for e in state.errors] == ["harbor_setup_crash"]


def test_reward_txt_is_found_by_walking_up_from_the_session_dir(
    tmp_path: Path,
) -> None:
    """P6: the stream verifier writes ``<trial>/verifier/reward.txt`` — eight
    levels above ``session_<uuid>`` in the harbor nesting."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-reward", with_child=False)
    (trial_dir / "verifier").mkdir()
    (trial_dir / "verifier" / "reward.txt").write_text("1\n")

    state = MonitorState(jobs_dir=str(jobs))
    _drive(_tail(trial_dir, "t-reward", state), seconds=0.4)

    trial = state.trials["t-reward"]
    assert trial.status == "completed"
    assert trial.reward == 1.0


def test_result_json_finalizes_a_timed_out_but_graded_trial(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-result", with_child=False)
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "verifier_result": {"rewards": {"reward": 1.0, "other": 0.0}},
                "exception_info": {"exception_type": "AgentTimeoutError"},
            }
        )
    )
    state = MonitorState(jobs_dir=str(jobs))
    _drive(_tail(trial_dir, "t-result", state), seconds=0.4)

    trial = state.trials["t-result"]
    assert trial.status == "completed"
    assert trial.reward == 0.5


def test_tick_exception_does_not_overwrite_a_terminal_status(tmp_path: Path) -> None:
    """P7: the codex guard (``codex.py:186-197``) — kimi and claude will flip a
    ``completed`` trial to ``tail_error`` on a late raising tick, which makes it
    vanish from any report that filters on terminal status."""
    from unittest.mock import patch

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-guard", with_child=False)
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-guard", state)
    state.ensure_trial("t-guard", str(trial_dir), agent="kimi-code")
    state.set_trial_status("t-guard", "completed")

    async def run() -> None:
        await tail.start()
        with patch.object(tail, "_tick", side_effect=RuntimeError("boom")):
            await asyncio.sleep(0.3)
        await tail.stop()

    asyncio.run(run())
    assert state.trials["t-guard"].status == "completed"


def test_tick_exception_is_reported_while_the_trial_is_live(tmp_path: Path) -> None:
    """The guard must not swallow a real fault on a non-terminal trial."""
    from unittest.mock import patch

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-fault", with_child=False)
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-fault", state)

    async def run() -> None:
        await tail.start()
        with patch.object(tail, "_tick", side_effect=RuntimeError("boom")):
            await asyncio.sleep(0.3)
        await tail.stop()

    asyncio.run(run())
    assert state.trials["t-fault"].status.startswith("tail_error:")


# ── idempotence ───────────────────────────────────────────────────────


def test_repeated_ticks_do_not_duplicate_runs_or_events(tmp_path: Path) -> None:
    """Ten polls over a finished session must leave exactly the events one pass
    produces: ``_FileTail`` tracks the byte offset and the parser dedups spawn
    synthesis by tool-call id."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    trial_dir = _materialize_kimi_code_trial(jobs, "t-idem")
    state = MonitorState(jobs_dir=str(jobs))
    tail = _tail(trial_dir, "t-idem", state)
    state.ensure_trial("t-idem", str(trial_dir), agent="kimi-code")

    for _ in range(10):
        tail._tick()

    trial = state.trials["t-idem"]
    assert set(trial.runs) == {"run_root_001", "run_sub_agent-0"}
    root = trial.runs["run_root_001"]
    assert root.n_events == len(set(e["event_id"] for e in root.events_tail))
    assert (
        len([e for e in root.structural_events if e["kind"] == "subagent_spawn"]) == 1
    )


def test_missing_home_dir_leaves_the_trial_at_discovered(tmp_path: Path) -> None:
    """A trial whose agent dir exists but has no ``kimi-code-home`` yet must not
    raise and must not be given a status it hasn't earned."""
    jobs = tmp_path / "jobs"
    trial_dir = jobs / "t-nohome"
    (trial_dir / "agent").mkdir(parents=True)
    state = MonitorState(jobs_dir=str(jobs))
    _drive(_tail(trial_dir, "t-nohome", state), seconds=0.3)

    assert state.trials["t-nohome"].status == "discovered"
    assert state.trials["t-nohome"].runs == {}
