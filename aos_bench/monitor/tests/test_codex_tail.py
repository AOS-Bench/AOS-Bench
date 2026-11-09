"""Tests for the multi-rollout CodexSessionTail (root + spawned subagents)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.codex import CodexSessionTail


def _write_rollout(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _root_meta(session_id: str = "root-sess") -> dict:
    return {
        "type": "session_meta",
        "timestamp": "2026-06-24T07:34:00.0Z",
        "payload": {
            "session_id": session_id,
            "id": session_id,
            "cli_version": "0.142.0",
            "originator": "codex_exec",
            "thread_source": "user",
        },
    }


def _sub_meta(
    session_id: str = "root-sess",
    *,
    thread_id: str,
    nickname: str = "Linnaeus",
) -> dict:
    return {
        "type": "session_meta",
        "timestamp": "2026-06-24T07:34:01.0Z",
        "payload": {
            "session_id": session_id,
            "id": thread_id,
            "parent_thread_id": session_id,
            "agent_role": "worker",
            "agent_nickname": nickname,
            "cli_version": "0.142.0",
            "originator": "codex_exec",
            "thread_source": "subagent",
        },
    }


def _turn_context() -> dict:
    return {
        "type": "turn_context",
        "timestamp": "2026-06-24T07:34:00.2Z",
        "payload": {"model": "gpt-5.4-mini", "effort": "high", "summary": "detailed"},
    }


def _spawn_call(call_id: str, message: str) -> list[dict]:
    return [
        {
            "type": "response_item",
            "timestamp": "2026-06-24T07:34:00.3Z",
            "payload": {
                "type": "function_call",
                "call_id": call_id,
                "name": "spawn_agent",
                "arguments": json.dumps(
                    {
                        "agent_type": "worker",
                        "fork_context": False,
                        "reasoning_effort": "medium",
                        "message": message,
                    }
                ),
                "status": "completed",
            },
        },
    ]


def _spawn_output(call_id: str, child_id: str, nickname: str) -> list[dict]:
    return [
        {
            "type": "response_item",
            "timestamp": "2026-06-24T07:34:00.4Z",
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"agent_id": child_id, "nickname": nickname}),
            },
        }
    ]


def _exec_call(call_id: str, cmd: str = "ls") -> list[dict]:
    return [
        {
            "type": "response_item",
            "timestamp": "2026-06-24T07:34:02.0Z",
            "payload": {
                "type": "function_call",
                "call_id": call_id,
                "name": "exec_command",
                "arguments": json.dumps({"command": cmd}),
                "status": "completed",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-06-24T07:34:02.1Z",
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": "ok",
            },
        },
    ]


def _materialize_fanout_trial(
    tmp_path: Path,
    *,
    trial_id: str = "trial-fanout",
) -> Path:
    """Build a trial dir whose rollout structure matches what codex 0.142
    with spawn_agent produces: one root rollout that calls spawn_agent
    twice, plus two sub rollouts with matching ``agent_id`` thread ids."""
    trial_dir = tmp_path / "jobs" / trial_id
    sessions = trial_dir / "agent" / "sessions" / "2026" / "06" / "24"
    root_thread = "root-sess-aaaa"
    child_a = "child-aaa-1111"
    child_b = "child-bbb-2222"

    # Root rollout: turn_context, two spawn calls + matching outputs, one
    # exec_command after the dispatch.
    _write_rollout(
        sessions / f"rollout-2026-06-24T07-34-00-{root_thread}.jsonl",
        _root_meta(root_thread),
        _turn_context(),
        *_spawn_call("call_spawn_a", "work on task A"),
        *_spawn_output("call_spawn_a", child_a, "Linnaeus"),
        *_spawn_call("call_spawn_b", "work on task B"),
        *_spawn_output("call_spawn_b", child_b, "Meitner"),
        *_exec_call("call_root_exec"),
    )
    # Sub A: own session_meta + turn + 2 exec_command tool_calls.
    _write_rollout(
        sessions / f"rollout-2026-06-24T07-34-05-{child_a}.jsonl",
        _sub_meta(root_thread, thread_id=child_a, nickname="Linnaeus"),
        _turn_context(),
        *_exec_call("call_sub_a_exec1", "echo a1"),
        *_exec_call("call_sub_a_exec2", "echo a2"),
    )
    # Sub B: own session_meta + 1 exec_command.
    _write_rollout(
        sessions / f"rollout-2026-06-24T07-34-10-{child_b}.jsonl",
        _sub_meta(root_thread, thread_id=child_b, nickname="Meitner"),
        _turn_context(),
        *_exec_call("call_sub_b_exec1", "echo b1"),
    )
    # Sort mtimes so root is oldest and subs follow in spawn order.
    for path, ts in [
        (sessions / f"rollout-2026-06-24T07-34-00-{root_thread}.jsonl", 1000.0),
        (sessions / f"rollout-2026-06-24T07-34-05-{child_a}.jsonl", 1005.0),
        (sessions / f"rollout-2026-06-24T07-34-10-{child_b}.jsonl", 1010.0),
    ]:
        import os
        os.utime(path, (ts, ts))
    return trial_dir


def test_tail_registers_root_plus_each_subagent_rollout(tmp_path: Path) -> None:
    """CodexSessionTail must:
    (a) classify the oldest no-agent_role rollout as the root run
    (b) discover each subsequent rollout and register it as a separate
        subagent run with parent_run_id=root
    (c) feed every rollout's events into its own run (so per-sub token
        counts don't bleed into root)
    (d) emit subagent_spawn events on the root run for each spawn_agent
        call/output pair
    """
    trial_dir = _materialize_fanout_trial(tmp_path)
    jobs_dir = trial_dir.parent
    state = MonitorState(jobs_dir=str(jobs_dir))
    detector = ErrorDetector(state)
    tail = CodexSessionTail(
        trial_id="trial-fanout",
        trial_dir=trial_dir,
        state=state,
        error_detector=detector,
        interval_s=0.2,
    )

    async def run() -> None:
        await tail.start()
        # Two ticks: first registers root, second picks up subs (they
        # already exist on disk in this fixture, but the loop polls
        # sessions/ on every tick so all three lands within ~0.6s).
        await asyncio.sleep(1.0)
        await tail.stop()

    asyncio.run(run())

    # Trial registered + classified.
    assert "trial-fanout" in state.trials
    trial = state.trials["trial-fanout"]
    assert trial.agent == "codex"

    # Runs: root + 2 subs.
    run_ids = set(trial.runs.keys())
    assert "run_root_001" in run_ids
    sub_ids = run_ids - {"run_root_001"}
    assert len(sub_ids) == 2, f"expected 2 sub runs, got {sub_ids}"

    # Each sub points back at root.
    for sid in sub_ids:
        sub = trial.runs[sid]
        assert sub.role == "subagent"
        assert sub.parent_run_id == "run_root_001"
        # codex/<nickname> labeling (Linnaeus or Meitner).
        assert sub.agent_name.startswith("codex/")

    # Root run carries the two synthesized subagent_spawn events.
    root = trial.runs["run_root_001"]
    spawn_events = [e for e in root.events_tail if e["kind"] == "subagent_spawn"]
    assert len(spawn_events) == 2
    child_run_ids = {e["payload"]["child_run_id"] for e in spawn_events}
    assert child_run_ids == sub_ids


def test_tail_does_not_double_register_rollout(tmp_path: Path) -> None:
    """Ticking many times must be idempotent — each rollout file
    registers exactly one run regardless of how many polls happen."""
    trial_dir = _materialize_fanout_trial(tmp_path)
    state = MonitorState(jobs_dir=str(trial_dir.parent))
    tail = CodexSessionTail(
        trial_id="trial-fanout",
        trial_dir=trial_dir,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=0.1,
    )

    async def run() -> None:
        await tail.start()
        await asyncio.sleep(0.8)  # 8 ticks
        await tail.stop()

    asyncio.run(run())
    trial = state.trials["trial-fanout"]
    # Root + 2 subs == 3 runs total; if discovery wasn't idempotent
    # we'd see duplicate run records.
    assert len(trial.runs) == 3


def test_tail_handles_sub_rollout_appearing_before_root(tmp_path: Path) -> None:
    """Edge case: codex sometimes flushes a sub's session_meta header
    in the same tick that creates the root rollout. The tail orders
    discovery by mtime ascending so the older root file always wins
    the root classification — even if both rollouts are visible at
    the first scan."""
    trial_dir = _materialize_fanout_trial(tmp_path)
    state = MonitorState(jobs_dir=str(trial_dir.parent))
    tail = CodexSessionTail(
        trial_id="trial-fanout",
        trial_dir=trial_dir,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=0.1,
    )

    async def run() -> None:
        await tail.start()
        await asyncio.sleep(0.6)
        await tail.stop()

    asyncio.run(run())
    trial = state.trials["trial-fanout"]
    assert trial.runs["run_root_001"].role == "root"
    # The two subs must NOT have stolen the root run id.
    sub_roles = {
        rid: trial.runs[rid].role for rid in trial.runs if rid != "run_root_001"
    }
    assert all(r == "subagent" for r in sub_roles.values())


# ── Robustness fixes (bugs surfaced in the post-merge code review) ────


def test_find_all_rollouts_tolerates_vanished_file(tmp_path: Path) -> None:
    """A rollout file that disappears between glob and stat() must NOT
    raise out of the helper. The race happens on cloud-mounted disks
    where cleanup / fuse blips delete files mid-scan; the old code did
    ``rollouts.sort(key=lambda p: p.stat().st_mtime)`` with no try/except
    so a single FileNotFoundError aborted the entire tick."""
    from aos_bench.monitor.tail.codex import _find_all_rollouts

    share = tmp_path / "sessions"
    day = share / "2026" / "06" / "24"
    day.mkdir(parents=True)
    real = day / "rollout-2026-06-24T07-34-00-real.jsonl"
    real.write_text("{}\n")
    # Patch Path.stat to raise FNFE for one specific path on first call —
    # simulates a file being deleted between glob and stat.
    import os
    from unittest.mock import patch

    real_stat = os.stat
    target_str = str(real)
    raise_count = {"n": 0}

    def flaky_stat(path, *a, **kw):
        if str(path) == target_str and raise_count["n"] == 0:
            raise_count["n"] += 1
            raise FileNotFoundError(2, "No such file", target_str)
        return real_stat(path, *a, **kw)

    with patch("os.stat", side_effect=flaky_stat):
        out = _find_all_rollouts(share)
    # Helper returned without raising; the dropped file is simply absent
    # from the result (caller will pick it up on the next tick).
    assert isinstance(out, list)


def test_loop_does_not_overwrite_terminal_status_on_tick_exception(
    tmp_path: Path,
) -> None:
    """Once a trial reaches a terminal status (completed/failed/crashed),
    a transient exception inside _tick must NOT flip it to ``tail_error``.
    Downstream aggregators filter by terminal status; reverting a
    completed trial to tail_error makes the trial vanish from reports."""
    from unittest.mock import patch

    trial_dir = _materialize_fanout_trial(tmp_path)
    state = MonitorState(jobs_dir=str(trial_dir.parent))
    tail = CodexSessionTail(
        trial_id="trial-fanout",
        trial_dir=trial_dir,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=0.05,
    )

    # Pre-register the trial as completed (simulating a finished run).
    state.ensure_trial("trial-fanout", str(trial_dir), agent="codex")
    state.set_trial_status("trial-fanout", "completed")
    state.set_trial_reward("trial-fanout", 1.0)
    assert state.trials["trial-fanout"].status == "completed"

    async def run() -> None:
        await tail.start()
        # Force _tick to blow up — the guard should leave 'completed' alone.
        with patch.object(tail, "_tick", side_effect=RuntimeError("simulated tick failure")):
            await asyncio.sleep(0.3)
        await tail.stop()

    asyncio.run(run())
    # Terminal status preserved across the simulated exception storm.
    assert state.trials["trial-fanout"].status == "completed"


def test_maybe_load_reward_short_circuits_after_terminal(tmp_path: Path) -> None:
    """Once the trial is terminal, _maybe_load_reward must not re-read
    reward.txt and must not re-publish set_trial_reward. Without this
    guard, every tick (1.5s) re-publishes a ``trial_update`` to all
    SSE subscribers, conflating one completed trial with hundreds of
    completion events."""
    # Build a minimal trial dir with a real rollout + reward.txt so the
    # method's setup path can run.
    trial_dir = tmp_path / "jobs" / "t1"
    rollout_dir = trial_dir / "agent" / "sessions" / "2026" / "06" / "24"
    rollout_dir.mkdir(parents=True)
    rollout_path = rollout_dir / "rollout-2026-06-24T07-34-00-test.jsonl"
    rollout_path.write_text("{}\n")
    verifier_dir = trial_dir / "agent" / "verifier"
    verifier_dir.mkdir(parents=True)
    (verifier_dir / "reward.txt").write_text("1.0\n")

    state = MonitorState(jobs_dir=str(trial_dir.parent))
    detector = ErrorDetector(state)
    tail = CodexSessionTail(
        trial_id="t1",
        trial_dir=trial_dir,
        state=state,
        error_detector=detector,
        interval_s=0.05,
    )
    state.ensure_trial("t1", str(trial_dir), agent="codex")
    tail._root_rollout = rollout_path  # bypass discovery for this unit test

    # Count publish events fired during reward calls. Subscribers are
    # plain queues — _publish does ``q.put_nowait(event)`` and drops on
    # error. We pose as one with a hand-rolled inbox.
    captured: list[dict] = []

    class _FakeQueue:
        def put_nowait(self, event: dict) -> None:
            captured.append(event)

    state._subscribers.append(_FakeQueue())  # type: ignore[attr-defined]

    # First call: trial is 'discovered' → load runs, sets completed.
    tail._maybe_load_reward()
    n_after_first = len(captured)
    assert state.trials["t1"].status == "completed"
    assert state.trials["t1"].reward == 1.0
    assert n_after_first >= 1

    # Subsequent calls must short-circuit before any set_trial_* fires.
    for _ in range(10):
        tail._maybe_load_reward()
    n_after_many = len(captured)
    assert n_after_many == n_after_first, (
        f"_maybe_load_reward kept publishing after terminal status: "
        f"{n_after_first} → {n_after_many}"
    )


def test_rollouts_in_order_evicts_vanished_subagent_rollout(tmp_path: Path) -> None:
    """If a registered subagent rollout file vanishes mid-trial, the
    next call to _rollouts_in_order must drop it from self._rollouts
    rather than raise FileNotFoundError every subsequent tick — the
    old code would stick the trial in 'tail_error' forever."""
    trial_dir = _materialize_fanout_trial(tmp_path)
    state = MonitorState(jobs_dir=str(trial_dir.parent))
    state.ensure_trial("trial-fanout", str(trial_dir), agent="codex")
    tail = CodexSessionTail(
        trial_id="trial-fanout",
        trial_dir=trial_dir,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=0.05,
    )

    # Run one discovery tick to populate self._rollouts.
    async def discover_once() -> None:
        await tail.start()
        await asyncio.sleep(0.3)
        await tail.stop()

    asyncio.run(discover_once())
    sub_paths = [p for p, (_, _, _, role) in tail._rollouts.items() if role == "subagent"]
    assert len(sub_paths) == 2, f"expected 2 subs registered, got {sub_paths}"

    # Delete one sub rollout out from under the tail.
    victim = sub_paths[0]
    victim.unlink()
    assert not victim.exists()

    # Helper now stats only the survivors and evicts the dead path.
    ordered = tail._rollouts_in_order()
    assert victim not in ordered
    assert victim not in tail._rollouts  # evicted
    # The other sub + root are still there.
    survivors = [p for p in ordered]
    assert len(survivors) == 2

