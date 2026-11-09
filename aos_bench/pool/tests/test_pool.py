"""Unit tests for TaskPool and the pool MCP server.

All tests use an injected clock or tiny tick periods so the suite stays
fast (each test < 1s).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from aos_bench.pool.pool import (
    AGENT_SETTABLE_STATUSES,
    COMPLETED,
    FAILED,
    IN_PROGRESS,
    PENDING,
    READY,
    TIMEOUT,
    Arrival,
    TaskPool,
    load_schedule,
)


class _FakeClock:
    """Test-only monotonic clock; advance() controls time."""

    def __init__(self, t0: float = 1000.0) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make_arrivals() -> list[Arrival]:
    return [
        Arrival(task_id="t1", at_sec=0.0, instruction="Do A"),
        Arrival(task_id="t2", at_sec=10.0, instruction="Do B"),
        Arrival(task_id="t3", at_sec=30.0, instruction="Do C"),
    ]


def _new_pool(tmp_path: Path, clock: _FakeClock | None = None) -> TaskPool:
    pool = TaskPool(
        arrivals=_make_arrivals(),
        max_wall_sec=120.0,
        state_dir=tmp_path / "state",
        tick_sec=10_000.0,
        clock=clock,
    )
    return pool


def _start_manual(pool: TaskPool) -> None:
    """Initialise the pool without launching the background scheduler so
    tests can drive `tick()` deterministically against a `_FakeClock`."""
    pool.start(run_scheduler=False)


# ── Construction ──────────────────────────────────────────────────────────


class TestConstruction:
    def test_rejects_duplicate_task_ids(self, tmp_path):
        with pytest.raises(ValueError, match="duplicate"):
            TaskPool(
                arrivals=[
                    Arrival("dup", 0.0, "a"),
                    Arrival("dup", 1.0, "b"),
                ],
                max_wall_sec=10,
                state_dir=tmp_path,
            )

    def test_rejects_negative_at_sec(self, tmp_path):
        with pytest.raises(ValueError, match="at_sec"):
            TaskPool(
                arrivals=[Arrival("t", -1.0, "x")],
                max_wall_sec=10,
                state_dir=tmp_path,
            )

    def test_rejects_zero_max_wall_sec(self, tmp_path):
        with pytest.raises(ValueError, match="max_wall_sec"):
            TaskPool(
                arrivals=[Arrival("t", 0.0, "x")],
                max_wall_sec=0,
                state_dir=tmp_path,
            )

    def test_state_dir_created_on_start(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        try:
            pool.start()
            assert (tmp_path / "state").is_dir()
        finally:
            pool.stop()


# ── Scheduling ────────────────────────────────────────────────────────────


class TestScheduling:
    def test_initial_state_all_pending(self, tmp_path):
        pool = _new_pool(tmp_path, _FakeClock())
        for t in pool.todo_list():
            assert t["status"] == PENDING

    def test_at_sec_zero_arrival_ready_after_first_tick(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            assert pool.tick() is True
            assert pool.todo_get("t1")["status"] == READY
            # later arrivals stay pending
            assert pool.todo_get("t2")["status"] == PENDING
            assert pool.todo_get("t3")["status"] == PENDING
        finally:
            pool.stop()

    def test_arrivals_promote_at_their_at_sec(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            pool.tick()
            clock.advance(10.0)
            pool.tick()
            assert pool.todo_get("t2")["status"] == READY
            assert pool.todo_get("t3")["status"] == PENDING
            clock.advance(20.0)  # total 30s
            pool.tick()
            assert pool.todo_get("t3")["status"] == READY
        finally:
            pool.stop()

    def test_tick_is_idempotent(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            assert pool.tick() is True
            # second tick at same time → nothing changed
            assert pool.tick() is False
        finally:
            pool.stop()


class TestSerialMode:
    """`serial=True` (continual setting): arrival[i] becomes ready only
    after arrival[i-1] is marked completed/failed. at_sec is ignored."""

    def _serial_pool(self, tmp_path: Path) -> TaskPool:
        return TaskPool(
            arrivals=[
                # at_sec values are irrelevant in serial mode
                Arrival(task_id="t1", at_sec=999, instruction="Do A"),
                Arrival(task_id="t2", at_sec=999, instruction="Do B"),
                Arrival(task_id="t3", at_sec=999, instruction="Do C"),
            ],
            max_wall_sec=120.0,
            state_dir=tmp_path / "state",
            tick_sec=10_000.0,
            clock=_FakeClock(),
            serial=True,
        )

    def test_first_task_ready_on_tick(self, tmp_path):
        pool = self._serial_pool(tmp_path)
        _start_manual(pool)
        try:
            pool.tick()
            assert pool.todo_get("t1")["status"] == READY
            assert pool.todo_get("t2")["status"] == PENDING
            assert pool.todo_get("t3")["status"] == PENDING
        finally:
            pool.stop()

    def test_next_promotes_after_completion(self, tmp_path):
        pool = self._serial_pool(tmp_path)
        _start_manual(pool)
        try:
            pool.tick()
            # claim t1, t2 should still be pending
            pool.todo_next(timeout_sec=0.0)
            assert pool.todo_get("t2")["status"] == PENDING
            # complete t1 → t2 should become ready (todo_update calls tick)
            pool.todo_update("t1", "completed")
            assert pool.todo_get("t2")["status"] == READY
            assert pool.todo_get("t3")["status"] == PENDING
        finally:
            pool.stop()

    def test_failed_also_promotes_next(self, tmp_path):
        pool = self._serial_pool(tmp_path)
        _start_manual(pool)
        try:
            pool.tick()
            pool.todo_next(timeout_sec=0.0)
            pool.todo_update("t1", "failed")
            assert pool.todo_get("t2")["status"] == READY
        finally:
            pool.stop()


class TestInMemoryAndStaging:
    """v0.2: in_memory mode skips per-tick disk writes (state stays in
    process memory). flush() / stop() dump everything to state_dir.
    staging_hook fires just before PENDING → READY transition."""

    def _in_memory_pool(self, tmp_path: Path, *, hook=None) -> TaskPool:
        return TaskPool(
            arrivals=[
                Arrival(task_id="t1", at_sec=0.0, instruction="A"),
                Arrival(task_id="t2", at_sec=10.0, instruction="B"),
            ],
            max_wall_sec=60.0,
            state_dir=tmp_path / "state",
            tick_sec=10_000.0,
            clock=_FakeClock(),
            in_memory=True,
            staging_hook=hook,
        )

    def test_in_memory_skips_per_tick_writes(self, tmp_path):
        pool = self._in_memory_pool(tmp_path)
        _start_manual(pool)
        try:
            pool.tick()  # promote t1 to READY
            # No tasks.json / events.jsonl on disk yet
            assert not (tmp_path / "state" / "tasks.json").exists()
            assert not (tmp_path / "state" / "events.jsonl").exists()
            # state_dir itself should NOT be auto-created in in-memory mode
            assert not (tmp_path / "state").exists()
        finally:
            pool.stop()

    def test_flush_writes_state_and_events(self, tmp_path):
        pool = self._in_memory_pool(tmp_path)
        _start_manual(pool)
        try:
            pool.tick()
            pool.todo_next(timeout_sec=0.0)
            pool.todo_update("t1", "completed")
            pool.flush()
            tasks = json.loads(
                (tmp_path / "state" / "tasks.json").read_text()
            )
            assert any(t["task_id"] == "t1" for t in tasks["tasks"])
            events = (
                tmp_path / "state" / "events.jsonl"
            ).read_text().strip().splitlines()
            kinds = [json.loads(e)["event"] for e in events]
            assert "pool_start" in kinds
            assert "ready" in kinds
            assert "claimed" in kinds
            assert "update" in kinds
        finally:
            pool.stop()

    def test_stop_calls_flush(self, tmp_path):
        pool = self._in_memory_pool(tmp_path)
        _start_manual(pool)
        pool.tick()
        pool.todo_next(timeout_sec=0.0)
        pool.todo_update("t1", "completed")
        pool.stop()
        # stop() should have flushed
        assert (tmp_path / "state" / "tasks.json").exists()
        assert (tmp_path / "state" / "events.jsonl").exists()

    def test_staging_hook_fires_before_ready(self, tmp_path):
        seen: list[str] = []

        def hook(task_id: str) -> None:
            seen.append(task_id)

        pool = self._in_memory_pool(tmp_path, hook=hook)
        _start_manual(pool)
        try:
            pool.tick()
            assert seen == ["t1"]  # only t1 ready (t2 at_sec=10)
            assert pool.todo_get("t1")["status"] == READY
        finally:
            pool.stop()

    def test_staging_hook_failure_does_not_block(self, tmp_path):
        def bad_hook(task_id: str) -> None:
            raise RuntimeError("boom")

        pool = self._in_memory_pool(tmp_path, hook=bad_hook)
        _start_manual(pool)
        try:
            pool.tick()
            # Promotion still happens despite hook failure
            assert pool.todo_get("t1")["status"] == READY
            pool.flush()
            events = (
                tmp_path / "state" / "events.jsonl"
            ).read_text().strip().splitlines()
            kinds = [json.loads(e)["event"] for e in events]
            assert "stage_failed" in kinds
        finally:
            pool.stop()


# ── todo_next ─────────────────────────────────────────────────────────────


class TestTodoNext:
    def test_returns_ready_task(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            pool.tick()
            res = pool.todo_next(timeout_sec=0.5)
            assert res["task_id"] == "t1"
            assert res["instruction"] == "Do A"
            assert res["stream_done"] is False
            # marks claimed
            assert pool.todo_get("t1")["status"] == IN_PROGRESS
            assert pool.todo_get("t1")["claimed_at"] is not None
        finally:
            pool.stop()

    def test_returns_in_schedule_order(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            clock.advance(15.0)  # t1 + t2 both ready
            pool.tick()
            assert pool.todo_next(timeout_sec=0.1)["task_id"] == "t1"
            assert pool.todo_next(timeout_sec=0.1)["task_id"] == "t2"
            res = pool.todo_next(timeout_sec=0.1)
            assert res["task_id"] is None
            assert res.get("timed_out") is True
        finally:
            pool.stop()

    def test_blocks_until_arrival(self, tmp_path):
        clock = _FakeClock()
        # Use the real ticker for this test so we exercise the blocking path.
        pool = TaskPool(
            arrivals=[Arrival("only", at_sec=0.05, instruction="hi")],
            max_wall_sec=10,
            state_dir=tmp_path / "state",
            tick_sec=0.01,
        )
        pool.start()
        try:
            t0 = time.monotonic()
            res = pool.todo_next(timeout_sec=2.0)
            elapsed = time.monotonic() - t0
            assert res["task_id"] == "only"
            # took at least the arrival delay, well under the timeout
            assert 0.04 <= elapsed < 1.0
        finally:
            pool.stop()

    def test_returns_timed_out_when_no_progress(self, tmp_path):
        pool = TaskPool(
            arrivals=[Arrival("late", at_sec=10.0, instruction="hi")],
            max_wall_sec=60,
            state_dir=tmp_path / "state",
            tick_sec=0.01,
        )
        pool.start()
        try:
            res = pool.todo_next(timeout_sec=0.05)
            assert res["task_id"] is None
            assert res["stream_done"] is False
            assert res["timed_out"] is True
        finally:
            pool.stop()

    def test_returns_stream_done_after_all_terminal(self, tmp_path):
        pool = TaskPool(
            arrivals=[Arrival("only", at_sec=0.0, instruction="hi")],
            max_wall_sec=60,
            state_dir=tmp_path / "state",
            tick_sec=0.01,
        )
        pool.start()
        try:
            res = pool.todo_next(timeout_sec=1.0)
            assert res["task_id"] == "only"
            pool.todo_update("only", COMPLETED)
            res = pool.todo_next(timeout_sec=0.5)
            assert res["task_id"] == "<FINISH>"
            assert res["instruction"] == "<FINISH>"
            assert res["stream_done"] is True
        finally:
            pool.stop()

    def test_blocking_wakes_on_completion(self, tmp_path):
        # Single-task stream: a blocked todo_next call must wake up as soon
        # as the task is marked completed (because that flips stream_done).
        pool = TaskPool(
            arrivals=[Arrival("only", at_sec=0.0, instruction="hi")],
            max_wall_sec=60,
            state_dir=tmp_path / "state",
            tick_sec=0.01,
        )
        pool.start()
        try:
            # claim it first
            pool.todo_next(timeout_sec=1.0)

            results: list[dict] = []

            def waiter():
                results.append(pool.todo_next(timeout_sec=2.0))

            th = threading.Thread(target=waiter)
            th.start()
            time.sleep(0.05)  # let waiter actually block
            pool.todo_update("only", COMPLETED)
            th.join(timeout=1.0)
            assert not th.is_alive()
            assert results[0]["stream_done"] is True
        finally:
            pool.stop()


# ── todo_update ───────────────────────────────────────────────────────────


class TestTodoUpdate:
    def test_only_completed_and_failed_accepted(self, tmp_path):
        # Spec: agent reports only the final outcome. The pool sets
        # in_progress automatically when a task is claimed.
        assert AGENT_SETTABLE_STATUSES == frozenset({COMPLETED, FAILED})
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            pool.tick()
            pool.todo_next(timeout_sec=0.1)  # claim t1
            assert pool.todo_update("t1", COMPLETED)["ok"] is True
        finally:
            pool.stop()

    def test_intermediate_status_rejected(self, tmp_path):
        # Agents may not set in_progress — that's the pool's job.
        pool = _new_pool(tmp_path, _FakeClock())
        _start_manual(pool)
        try:
            res = pool.todo_update("t1", IN_PROGRESS)
            assert res["ok"] is False
            assert "invalid" in res["error"]
        finally:
            pool.stop()

    def test_invalid_status_rejected(self, tmp_path):
        pool = _new_pool(tmp_path, _FakeClock())
        _start_manual(pool)
        try:
            res = pool.todo_update("t1", "donezo")
            assert res["ok"] is False
            assert "invalid" in res["error"]
        finally:
            pool.stop()

    def test_unknown_task_id_rejected(self, tmp_path):
        pool = _new_pool(tmp_path, _FakeClock())
        _start_manual(pool)
        try:
            res = pool.todo_update("nope", COMPLETED)
            assert res["ok"] is False
            assert "unknown" in res["error"]
        finally:
            pool.stop()

    def test_terminal_status_sets_completed_at(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            pool.tick()
            pool.todo_next(timeout_sec=0.1)
            clock.advance(5.0)
            pool.todo_update("t1", COMPLETED, note="done")
            t = pool.todo_get("t1")
            assert t["status"] == COMPLETED
            assert t["completed_at"] == pytest.approx(5.0, abs=0.1)
            assert t["note"] == "done"
        finally:
            pool.stop()


# ── stream_done semantics ─────────────────────────────────────────────────


class TestFreeze:
    """Work an agent does after its execution limit must not be recorded."""

    def _events(self, tmp_path):
        lines = (tmp_path / "state" / "events.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines]

    def test_rejects_update_after_freeze(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            pool.tick()
            assert pool.todo_next(timeout_sec=0.1)["task_id"] == "t1"
            pool.freeze()
            res = pool.todo_update("t1", COMPLETED, note="late")
            assert res["ok"] is False
            assert pool.todo_get("t1")["status"] == IN_PROGRESS
            assert pool.todo_get("t1")["completed_at"] is None
            kinds = [e["event"] for e in self._events(tmp_path)]
            assert "frozen" in kinds
            assert "rejected_after_freeze" in kinds
            assert kinds.count("update") == 0
        finally:
            pool.stop()

    def test_next_returns_finish_without_claiming(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            pool.tick()
            pool.freeze()
            res = pool.todo_next(timeout_sec=0.1)
            assert res["task_id"] == "<FINISH>"
            assert res["frozen"] is True
            assert pool.todo_get("t1")["status"] == READY
            assert pool.todo_get("t1")["claimed_at"] is None
        finally:
            pool.stop()

    def test_no_promotion_after_freeze(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            pool.tick()
            pool.freeze()
            clock.advance(60)
            assert pool.tick() is False
            assert pool.todo_get("t2")["status"] == PENDING
            assert pool.todo_get("t3")["status"] == PENDING
        finally:
            pool.stop()

    def test_freeze_wakes_a_blocked_claim(self, tmp_path):
        pool = TaskPool(
            arrivals=[Arrival("late", at_sec=3600.0, instruction="x")],
            max_wall_sec=7200,
            state_dir=tmp_path / "state",
            tick_sec=0.01,
        )
        pool.start()
        try:
            out = {}
            waiter = threading.Thread(
                target=lambda: out.update(pool.todo_next(timeout_sec=30.0))
            )
            waiter.start()
            time.sleep(0.1)
            pool.freeze()
            waiter.join(timeout=2.0)
            assert not waiter.is_alive()
            assert out["task_id"] == "<FINISH>"
        finally:
            pool.stop()

    def test_freeze_is_idempotent(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            pool.freeze()
            pool.freeze()
            kinds = [e["event"] for e in self._events(tmp_path)]
            assert kinds.count("frozen") == 1
        finally:
            pool.stop()


class TestStreamDone:
    def test_marks_done_when_all_terminal(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            # Drive all three to completed.
            for t_id in ["t1", "t2", "t3"]:
                if t_id == "t2":
                    clock.advance(10)
                if t_id == "t3":
                    clock.advance(20)
                pool.tick()
                pool.todo_next(timeout_sec=0.1)
                pool.todo_update(t_id, COMPLETED)
            assert pool.todo_status()["stream_done"] is True
            assert (tmp_path / "state" / "done").exists()
        finally:
            pool.stop()

    def test_failed_counts_as_terminal(self, tmp_path):
        pool = TaskPool(
            arrivals=[Arrival("only", at_sec=0.0, instruction="x")],
            max_wall_sec=60,
            state_dir=tmp_path / "state",
            tick_sec=0.01,
        )
        pool.start()
        try:
            pool.todo_next(timeout_sec=1.0)
            pool.todo_update("only", FAILED, note="exploded")
            res = pool.todo_next(timeout_sec=0.5)
            assert res["stream_done"] is True
        finally:
            pool.stop()

    def test_in_progress_alone_does_not_finish_stream(self, tmp_path):
        # If an agent claims a task but never reports completed/failed,
        # the stream stays open until max_wall_sec forces a timeout.
        pool = TaskPool(
            arrivals=[Arrival("only", at_sec=0.0, instruction="x")],
            max_wall_sec=60,
            state_dir=tmp_path / "state",
            tick_sec=0.01,
        )
        pool.start()
        try:
            pool.todo_next(timeout_sec=1.0)  # claimed -> in_progress
            res = pool.todo_next(timeout_sec=0.05)
            assert res["stream_done"] is False
            assert res.get("timed_out") is True
        finally:
            pool.stop()

    def test_max_wall_sec_timeouts_unfinished(self, tmp_path):
        clock = _FakeClock()
        pool = TaskPool(
            arrivals=[
                Arrival("a", at_sec=0.0, instruction="x"),
                Arrival("b", at_sec=5.0, instruction="y"),
            ],
            max_wall_sec=10.0,
            state_dir=tmp_path / "state",
            tick_sec=10_000.0,
            clock=clock,
        )
        pool.start()
        try:
            pool.tick()
            pool.todo_next(timeout_sec=0.1)
            pool.todo_update("a", COMPLETED)
            clock.advance(15.0)  # past max_wall_sec
            pool.tick()
            assert pool.todo_get("b")["status"] == TIMEOUT
            assert pool.todo_status()["stream_done"] is True
        finally:
            pool.stop()


# ── persistence ───────────────────────────────────────────────────────────


class TestPersistence:
    def test_tasks_json_reflects_state(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            pool.tick()
            pool.todo_next(timeout_sec=0.1)
            pool.todo_update("t1", COMPLETED)
            data = json.loads((tmp_path / "state" / "tasks.json").read_text())
            by_id = {t["task_id"]: t for t in data["tasks"]}
            assert by_id["t1"]["status"] == COMPLETED
            assert by_id["t2"]["status"] == PENDING
            assert data["stream_done"] is False
        finally:
            pool.stop()

    def test_events_jsonl_records_lifecycle(self, tmp_path):
        clock = _FakeClock()
        pool = _new_pool(tmp_path, clock)
        _start_manual(pool)
        try:
            pool.tick()
            pool.todo_next(timeout_sec=0.1)
            pool.todo_update("t1", COMPLETED)
        finally:
            pool.stop()
        lines = (tmp_path / "state" / "events.jsonl").read_text().splitlines()
        events = [json.loads(line) for line in lines]
        names = [e["event"] for e in events]
        assert "pool_start" in names
        assert "ready" in names
        assert "claimed" in names
        assert "update" in names

    def test_done_sentinel_touched(self, tmp_path):
        pool = TaskPool(
            arrivals=[Arrival("only", at_sec=0.0, instruction="x")],
            max_wall_sec=60,
            state_dir=tmp_path / "state",
            tick_sec=0.01,
        )
        pool.start()
        try:
            pool.todo_next(timeout_sec=1.0)
            pool.todo_update("only", COMPLETED)
            # Wait briefly for sentinel write
            for _ in range(50):
                if (tmp_path / "state" / "done").exists():
                    break
                time.sleep(0.01)
        finally:
            pool.stop()
        assert (tmp_path / "state" / "done").exists()


# ── load_schedule ─────────────────────────────────────────────────────────


class TestLoadSchedule:
    def test_loads_minimal_schedule(self, tmp_path):
        path = tmp_path / "schedule.json"
        path.write_text(
            json.dumps(
                {
                    "max_wall_sec": 300,
                    "arrivals": [
                        {"at_sec": 0, "task_id": "a", "instruction": "x"},
                        {"at_sec": 5.5, "task_id": "b", "instruction": "y"},
                    ],
                }
            )
        )
        arrivals, max_wall_sec, serial = load_schedule(path)
        assert max_wall_sec == 300.0
        assert serial is False
        assert [a.task_id for a in arrivals] == ["a", "b"]
        assert arrivals[1].at_sec == 5.5

    def test_loads_deadline_sec(self, tmp_path):
        path = tmp_path / "schedule.json"
        path.write_text(
            json.dumps(
                {
                    "max_wall_sec": 60,
                    "arrivals": [
                        {
                            "at_sec": 0,
                            "task_id": "a",
                            "instruction": "x",
                            "deadline_sec": 30,
                        }
                    ],
                }
            )
        )
        arrivals, _, _ = load_schedule(path)
        assert arrivals[0].deadline_sec == 30.0

    def test_loads_serial_flag(self, tmp_path):
        path = tmp_path / "schedule.json"
        path.write_text(
            json.dumps(
                {
                    "max_wall_sec": 60,
                    "serial": True,
                    "arrivals": [
                        {"at_sec": 999, "task_id": "a", "instruction": "x"},
                        {"at_sec": 999, "task_id": "b", "instruction": "y"},
                    ],
                }
            )
        )
        arrivals, _, serial = load_schedule(path)
        assert serial is True
        assert [a.task_id for a in arrivals] == ["a", "b"]


# ── MCP server wiring ─────────────────────────────────────────────────────


class TestServer:
    @pytest.fixture
    def schedule_path(self, tmp_path):
        path = tmp_path / "schedule.json"
        path.write_text(
            json.dumps(
                {
                    "max_wall_sec": 60,
                    "arrivals": [
                        {"at_sec": 0, "task_id": "a", "instruction": "do a"},
                    ],
                }
            )
        )
        return path

    def test_create_server_registers_all_tools(self, tmp_path, schedule_path):
        from aos_bench.pool.pool import load_schedule
        from aos_bench.pool.server import create_server

        arrivals, max_wall_sec, serial = load_schedule(schedule_path)
        mcp, pool = create_server(
            arrivals=arrivals,
            max_wall_sec=max_wall_sec,
            serial=serial,
            state_dir=tmp_path / "state",
        )
        try:
            tool_names = {t.name for t in mcp._tool_manager.list_tools()}
            assert tool_names == {
                "todo_next",
                "todo_list",
                "todo_update",
                "todo_get",
                "todo_status",
            }
        finally:
            pool.stop()

    def test_tools_return_valid_json(self, tmp_path, schedule_path):
        from aos_bench.pool.pool import load_schedule
        from aos_bench.pool.server import create_server

        arrivals, max_wall_sec, serial = load_schedule(schedule_path)
        mcp, pool = create_server(
            arrivals=arrivals,
            max_wall_sec=max_wall_sec,
            serial=serial,
            state_dir=tmp_path / "state",
            tick_sec=0.01,
        )
        pool.start()
        try:
            tools = {t.name: t for t in mcp._tool_manager.list_tools()}
            res = json.loads(tools["todo_next"].fn(timeout_sec=1))
            assert res["task_id"] == "a"
            res = json.loads(
                tools["todo_update"].fn(task_id="a", status="completed")
            )
            assert res["ok"] is True
            res = json.loads(tools["todo_status"].fn())
            assert res["stream_done"] is True
        finally:
            pool.stop()


# ── v0.2 staged reveal ────────────────────────────────────────────────────


class TestStagingHook:
    """The reveal hook must land a task's payload exactly where the
    assembler rewrote that task's paths to: the *flat* workspace target.

    `tests/test_outputs.py` hardcodes `app_dir = Path("/workspace")` and
    instructions name `/workspace/eval.py`, so revealing into a per-task
    subdirectory would put every payload one level below where the task's
    own tests and oracle look for it.
    """

    def _hook(self, tmp_path, **kw):
        from aos_bench.pool.server import _make_staging_hook

        return _make_staging_hook(
            kw.get("workspace_stage", tmp_path / "stage"),
            kw.get("workspace_target", tmp_path / "target"),
            kw.get("skills_stage"),
            kw.get("skills_target"),
            kw.get("agent_uid"),
        )

    def test_reveals_contents_flat_not_nested(self, tmp_path):
        stage = tmp_path / "stage" / "t1"
        stage.mkdir(parents=True)
        (stage / "model.xml").write_text("x")
        (stage / "src").mkdir()
        (stage / "src" / "eval.py").write_text("y")
        target = tmp_path / "target"

        self._hook(tmp_path)("t1")

        # Flat: /target/model.xml, NOT /target/t1/model.xml.
        assert (target / "model.xml").read_text() == "x"
        assert (target / "src" / "eval.py").read_text() == "y"
        assert not (target / "t1").exists()
        # Stage dir is consumed so a second reveal is a no-op.
        assert not stage.exists()

    def test_second_task_merges_into_same_target(self, tmp_path):
        for task_id, name in (("t1", "a.txt"), ("t2", "b.txt")):
            d = tmp_path / "stage" / task_id
            d.mkdir(parents=True)
            (d / name).write_text(task_id)
        target = tmp_path / "target"
        hook = self._hook(tmp_path)

        hook("t1")
        hook("t2")

        assert (target / "a.txt").read_text() == "t1"
        assert (target / "b.txt").read_text() == "t2"

    def test_existing_path_is_not_clobbered(self, tmp_path):
        # Assembly-time collision renaming should prevent this, but a live
        # task's file must never be overwritten by a later reveal.
        d = tmp_path / "stage" / "t2"
        d.mkdir(parents=True)
        (d / "shared.txt").write_text("new")
        target = tmp_path / "target"
        target.mkdir()
        (target / "shared.txt").write_text("in-use")

        self._hook(tmp_path)("t2")

        assert (target / "shared.txt").read_text() == "in-use"

    def test_missing_stage_dir_is_tolerated(self, tmp_path):
        # A task with no payload at all (nothing staged) must not raise.
        (tmp_path / "stage").mkdir()
        self._hook(tmp_path)("nonexistent")

    def test_no_hook_when_nothing_to_stage(self, tmp_path):
        assert (
            self._hook(tmp_path, workspace_stage=None, workspace_target=None)
            is None
        )
