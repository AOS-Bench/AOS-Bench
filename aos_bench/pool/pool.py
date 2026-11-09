"""TaskPool: wall-clock-driven dispenser for AOS-Bench stream tasks.

The pool owns the schedule and publishes tasks at their `at_sec` arrival
times. Agents discover work by calling `todo_next` (which blocks until a
task is ready or the stream finishes), report status via `todo_update`,
and read overall progress with `todo_list` / `todo_get` / `todo_status`.

Files written under `state_dir`:

  tasks.json     latest snapshot of all task states (overwritten each tick)
  events.jsonl   append-only event log (ready / claimed / update / done)
  done           sentinel touched once `stream_done` flips to True

v0.2 (in-memory mode, see docs/framework/task_assign.md):

  * If ``in_memory=True``, the pool does NOT write tasks.json or
    events.jsonl per-tick, and the ``done`` sentinel is also skipped.
    Events accumulate in memory; call ``flush()`` (or stop the pool)
    to write them once to ``state_dir``. This is used when the pool
    runs as an isolated sidecar process and we don't want the agent
    container's filesystem to leak schedule + history.
  * ``staging_hook(task_id)`` (optional) is invoked when an arrival is
    promoted to READY, BEFORE the task is handed to an agent via
    todo_next. The hook can stage per-task workspace assets (mv from
    a private staging dir to ``/workspace/<task>/``, chown to agent
    uid). Failures in the hook are logged but do not block promotion.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Status lifecycle (see docs/aos_bench.md#v260426):
#   PENDING -> READY (pool: at scheduled at_sec)
#   READY   -> IN_PROGRESS (pool: agent claimed via todo_next)
#   IN_PROGRESS -> COMPLETED | FAILED (agent reports)
#   any non-terminal -> TIMEOUT (pool: wall-clock exceeded)
#
# Agents only report final results — `in_progress` is set automatically when
# the pool hands a task to the agent, and there is no intermediate status.
PENDING = "pending"
READY = "ready"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"
TIMEOUT = "timeout"

TERMINAL_STATUSES: frozenset[str] = frozenset({COMPLETED, FAILED, TIMEOUT})
AGENT_SETTABLE_STATUSES: frozenset[str] = frozenset({COMPLETED, FAILED})

DEFAULT_TICK_SEC = 0.2


@dataclass(frozen=True)
class Arrival:
    task_id: str
    at_sec: float
    instruction: str
    deadline_sec: float | None = None


@dataclass
class _TaskState:
    task_id: str
    instruction: str
    at_sec: float
    status: str = PENDING
    ready_at: float | None = None
    claimed_at: float | None = None
    completed_at: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "at_sec": self.at_sec,
            "ready_at": self.ready_at,
            "claimed_at": self.claimed_at,
            "completed_at": self.completed_at,
            "note": self.note,
        }


class TaskPool:
    """Thread-safe pool. Background ticker promotes pending arrivals to ready."""

    def __init__(
        self,
        arrivals: list[Arrival],
        max_wall_sec: float,
        state_dir: Path,
        tick_sec: float = DEFAULT_TICK_SEC,
        clock: "callable[[], float] | None" = None,
        serial: bool = False,
        in_memory: bool = False,
        staging_hook: Callable[[str], None] | None = None,
    ) -> None:
        seen: set[str] = set()
        for a in arrivals:
            if a.task_id in seen:
                raise ValueError(f"duplicate task_id in schedule: {a.task_id!r}")
            if a.at_sec < 0:
                raise ValueError(f"at_sec must be >= 0 for {a.task_id!r}")
            seen.add(a.task_id)
        # max_wall_sec controls the *pool-internal* TIMEOUT path: when the
        # tick clock crosses it, the pool marks remaining non-terminal tasks
        # as TIMEOUT and flips stream_done. Pass a large sentinel value
        # (e.g. 2**31-1 seconds ≈ 68 years) to disable this path in practice
        # — the only wall budget is then enforced externally (launcher
        # `timeout(1)` / harbor trial-level kill). Disabled is the
        # recommended setup (see docs/framework/task_assign.md "wall control"
        # section) because:
        #   * Pool's TIMEOUT signal is delivered to the agent ONLY on its
        #     next todo_next call; agents that aren't polling (stuck in an
        #     LLM call / sub-agent loop) never see it → external timeout
        #     ends up doing the real work anyway.
        #   * Two layers of wall caps with different defaults invited the
        #     "layer 2 is shorter than layer 3 but unreliable" footgun
        #     observed in practice (opencode burst trials ran 5-10× their
        #     pool max_wall_sec before being killed externally).
        # Finite max_wall_sec is kept for unit tests + future explicit use.
        # We don't accept math.inf because the schedule travels over JSON
        # (dispatcher HTTP) and `Infinity` is not standard JSON.
        if max_wall_sec <= 0:
            raise ValueError(
                "max_wall_sec must be > 0 "
                "(use a large finite value like 2**31-1 to disable in practice)"
            )

        # In serial mode, arrivals are gated on the previous arrival reaching
        # a terminal status (completed/failed/timeout), not on wall clock.
        # Preserve schedule order rather than sorting by at_sec.
        self._serial = bool(serial)
        self._arrivals = (
            list(arrivals) if self._serial
            else sorted(arrivals, key=lambda a: a.at_sec)
        )
        self._max_wall_sec = max_wall_sec
        self._state_dir = state_dir
        self._tick_sec = tick_sec
        # v0.2: when in_memory, skip per-tick disk writes; events are
        # buffered and flushed on demand (flush() / stop()).
        self._in_memory = bool(in_memory)
        self._events_buffer: list[dict[str, Any]] = [] if self._in_memory else []
        # Optional callback invoked just before a task transitions PENDING
        # → READY. Used by the sidecar pool to stage workspace assets.
        self._staging_hook = staging_hook
        # Inject a clock for tests; default is monotonic for real timing.
        self._clock = clock or time.monotonic

        self._tasks: dict[str, _TaskState] = {
            a.task_id: _TaskState(
                task_id=a.task_id,
                instruction=a.instruction,
                at_sec=a.at_sec,
            )
            for a in self._arrivals
        }
        self._tasks_path = state_dir / "tasks.json"
        self._events_path = state_dir / "events.jsonl"
        self._done_sentinel = state_dir / "done"

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._t0: float = 0.0
        self._stream_done = False
        # Set by freeze() once the agent's execution phase has ended. From
        # then on nothing is promoted, claimed or updated.
        self._frozen = False
        self._stop = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self, run_scheduler: bool = True) -> None:
        """Initialise state and (optionally) launch the background scheduler.

        ``run_scheduler=False`` is for tests that want to advance time
        deterministically by calling :meth:`tick` directly.
        """
        # In v0.2 in-memory mode, state_dir creation is deferred to flush()
        # — we must not write or even mkdir the agent-visible state path
        # during normal operation.
        if not self._in_memory:
            self._state_dir.mkdir(parents=True, exist_ok=True)
        self._t0 = self._clock()
        with self._lock:
            self._event_locked("pool_start", schedule_size=len(self._arrivals))
            self._flush_tasks_locked()
        if run_scheduler:
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="aos_pool_scheduler",
                daemon=True,
            )
            self._scheduler_thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=2.0)
            self._scheduler_thread = None
        # v0.2: ensure events.jsonl + tasks.json get to disk before exit.
        if self._in_memory:
            self.flush()

    def freeze(self, reason: str = "execution_limit") -> dict[str, Any]:
        """Close the pool when the agent's execution phase ends.

        Harbor calls this before verification. Afterwards no arrival is
        promoted, ``todo_next`` hands back the FINISH sentinel without
        claiming anything, and ``todo_update`` is rejected and logged as
        ``rejected_after_freeze``. An agent process that outlives its
        execution limit therefore cannot change the recorded task states.
        Idempotent.
        """
        with self._cv:
            if not self._frozen:
                self._frozen = True
                self._event_locked(
                    "frozen", reason=reason, at_sec=self._now_sec_locked()
                )
                self._flush_tasks_locked()
            self._cv.notify_all()
            return {"ok": True, "frozen": True}

    # ── public tool implementations ────────────────────────────────────────

    def todo_next(self, timeout_sec: float = 60.0) -> dict[str, Any]:
        """Block up to ``timeout_sec`` for the next ready task.

        Returns one of:
          {task_id, instruction, stream_done: false}      task ready
          {task_id: null, stream_done: true}              stream finished
          {task_id: null, stream_done: false,
           timed_out: true}                               timeout, more later
        """
        deadline = time.monotonic() + max(0.0, timeout_sec)
        with self._cv:
            while True:
                if self._frozen:
                    return {
                        "task_id": "<FINISH>",
                        "instruction": "<FINISH>",
                        "stream_done": True,
                        "frozen": True,
                    }
                ready = self._first_ready_locked()
                if ready is not None:
                    ready.status = IN_PROGRESS
                    ready.claimed_at = self._now_sec_locked()
                    self._event_locked(
                        "claimed",
                        task_id=ready.task_id,
                        at_sec=ready.claimed_at,
                    )
                    self._flush_tasks_locked()
                    return {
                        "task_id": ready.task_id,
                        "instruction": ready.instruction,
                        "stream_done": False,
                    }
                # No ready task. If the stream is done, return that.
                if self._stream_done:
                    # Sentinel "task" the agent must claim before
                    # terminating. The agent contract (see
                    # docs/framework/agent_contract.md) is: keep calling
                    # todo_next until you receive task_id == "<FINISH>",
                    # then exit. This puts the termination signal on the
                    # same channel the agent is already polling, so we
                    # don't need an agent-framework-specific wrapper to
                    # keep the agent alive — the dispatcher just hands
                    # back FINISH and the agent itself decides to stop.
                    return {
                        "task_id": "<FINISH>",
                        "instruction": "<FINISH>",
                        "stream_done": True,
                    }
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "task_id": None,
                        "stream_done": False,
                        "timed_out": True,
                    }
                self._cv.wait(remaining)

    def todo_list(self) -> list[dict[str, Any]]:
        """Full task list — internal/dispatcher view. Includes PENDING.

        For the agent-facing view (which must NOT leak future arrivals
        or the total task count), call ``todo_list_agent_view`` instead.
        """
        with self._lock:
            return [t.to_dict() for t in self._tasks.values()]

    def todo_list_agent_view(self) -> list[dict[str, Any]]:
        """Agent-facing snapshot. Hides PENDING tasks (future arrivals)
        and strips ``at_sec`` (the planned arrival time) so the agent
        cannot peek at future schedule entries.

        Returned entries cover only tasks the agent has already had
        visibility on: READY / IN_PROGRESS / COMPLETED / FAILED /
        TIMEOUT.
        """
        with self._lock:
            out: list[dict[str, Any]] = []
            for t in self._tasks.values():
                if t.status == PENDING:
                    continue
                d = t.to_dict()
                d.pop("at_sec", None)
                out.append(d)
            return out

    def todo_update(
        self, task_id: str, status: str, note: str = ""
    ) -> dict[str, Any]:
        """Report a final result for a claimed task.

        ``status`` must be ``completed`` or ``failed``. The pool does not
        accept intermediate statuses — agents simply work on a task after
        ``todo_next`` returns it and report once when done.
        """
        if status not in AGENT_SETTABLE_STATUSES:
            return {
                "ok": False,
                "error": (
                    f"invalid status {status!r}; must be one of "
                    f"{sorted(AGENT_SETTABLE_STATUSES)}"
                ),
            }
        with self._cv:
            t = self._tasks.get(task_id)
            if t is None:
                return {"ok": False, "error": f"unknown task_id {task_id!r}"}
            now = self._now_sec_locked()
            if self._frozen:
                self._event_locked(
                    "rejected_after_freeze",
                    task_id=task_id,
                    status=status,
                    at_sec=now,
                )
                return {
                    "ok": False,
                    "error": "the stream is closed: the execution limit was reached",
                }
            t.status = status
            if note:
                t.note = note
            # All agent-settable statuses are terminal; record completion ts.
            t.completed_at = now
            self._event_locked(
                "update",
                task_id=task_id,
                status=status,
                note=note,
                at_sec=now,
            )
            # In serial mode, promote the next pending task immediately rather
            # than waiting for the next scheduler tick. _tick_locked already
            # flushes state on change.
            self._tick_locked()
            self._maybe_mark_done_locked()
            self._flush_tasks_locked()
            self._cv.notify_all()
            return {"ok": True, "task_id": task_id, "status": status}

    def todo_get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return {"ok": False, "error": f"unknown task_id {task_id!r}"}
            return {"ok": True, **t.to_dict()}

    def todo_status(self) -> dict[str, Any]:
        """Full status — internal/dispatcher view. Includes total task count.

        Agent-facing callers should use ``todo_status_agent_view`` which
        hides ``n_total`` and the PENDING count so the agent cannot infer
        how many tasks are still queued for the future.
        """
        with self._lock:
            counts: dict[str, int] = {}
            for t in self._tasks.values():
                counts[t.status] = counts.get(t.status, 0) + 1
            return {
                "now_sec": self._now_sec_locked(),
                "n_total": len(self._tasks),
                "stream_done": self._stream_done,
                "deadline_sec": self._max_wall_sec,
                "counts_by_status": counts,
            }

    def todo_status_agent_view(self) -> dict[str, Any]:
        """Agent-facing status. Hides n_total and PENDING count so the
        agent cannot peek at how many tasks are still in the future.
        """
        with self._lock:
            counts: dict[str, int] = {}
            for t in self._tasks.values():
                if t.status == PENDING:
                    continue
                counts[t.status] = counts.get(t.status, 0) + 1
            return {
                "now_sec": self._now_sec_locked(),
                "stream_done": self._stream_done,
                "counts_by_status": counts,
            }

    # ── scheduler ──────────────────────────────────────────────────────────

    def tick(self) -> bool:
        """Single scheduler tick. Returns True if any state changed.

        Exposed for tests so they can drive the scheduler deterministically
        without waiting for the background thread.
        """
        with self._cv:
            return self._tick_locked()

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                changed = self._tick_locked()
                if changed:
                    self._cv.notify_all()
                if self._stream_done:
                    # Keep ticker alive — agent may still call todo_list /
                    # todo_status after stream_done. Block on stop event.
                    pass
            self._stop.wait(self._tick_sec)

    def _tick_locked(self) -> bool:
        if self._t0 == 0.0 or self._frozen:
            return False
        now_sec = self._now_sec_locked()
        changed = False
        # Promote arrivals to ready.
        #  - serial mode: arrival[i] becomes ready when arrival[i-1] is in a
        #    terminal status (or i==0). at_sec is ignored.
        #  - wall-clock mode (default): arrival becomes ready when now >= at_sec.
        if self._serial:
            for i, arr in enumerate(self._arrivals):
                t = self._tasks[arr.task_id]
                if t.status != PENDING:
                    continue
                if i == 0 or self._tasks[
                    self._arrivals[i - 1].task_id
                ].status in TERMINAL_STATUSES:
                    self._invoke_staging_hook(t.task_id)
                    t.status = READY
                    t.ready_at = now_sec
                    self._event_locked(
                        "ready", task_id=t.task_id, at_sec=now_sec
                    )
                    changed = True
                # Only promote the earliest pending arrival per tick — in
                # serial mode, at most one task is ready at any moment.
                if t.status == READY:
                    break
        else:
            for arr in self._arrivals:
                t = self._tasks[arr.task_id]
                if t.status == PENDING and now_sec >= arr.at_sec:
                    self._invoke_staging_hook(t.task_id)
                    t.status = READY
                    t.ready_at = now_sec
                    self._event_locked(
                        "ready", task_id=t.task_id, at_sec=now_sec
                    )
                    changed = True

        if not self._stream_done and now_sec >= self._max_wall_sec:
            for t in self._tasks.values():
                if t.status not in TERMINAL_STATUSES:
                    t.status = TIMEOUT
                    t.completed_at = now_sec
                    self._event_locked(
                        "timeout", task_id=t.task_id, at_sec=now_sec
                    )
                    changed = True
            self._mark_done_locked("max_wall_sec")
            changed = True
        else:
            if self._maybe_mark_done_locked():
                changed = True

        if changed:
            self._flush_tasks_locked()
        return changed

    # ── internals ──────────────────────────────────────────────────────────

    def _now_sec_locked(self) -> float:
        if self._t0 == 0.0:
            return 0.0
        return self._clock() - self._t0

    def _first_ready_locked(self) -> _TaskState | None:
        # Stable ordering: earliest scheduled at_sec wins.
        for arr in self._arrivals:
            t = self._tasks[arr.task_id]
            if t.status == READY:
                return t
        return None

    def _all_arrivals_fired_locked(self) -> bool:
        return all(t.status != PENDING for t in self._tasks.values())

    def _all_terminal_locked(self) -> bool:
        return all(t.status in TERMINAL_STATUSES for t in self._tasks.values())

    def _maybe_mark_done_locked(self) -> bool:
        if self._stream_done:
            return False
        if self._all_arrivals_fired_locked() and self._all_terminal_locked():
            self._mark_done_locked("all_completed")
            return True
        return False

    def _mark_done_locked(self, reason: str) -> None:
        if self._stream_done:
            return
        self._stream_done = True
        if not self._in_memory:
            try:
                self._done_sentinel.touch()
            except OSError as e:
                logger.warning("failed to touch done sentinel: %s", e)
        self._event_locked(
            "stream_done", reason=reason, at_sec=self._now_sec_locked()
        )

    # ── v0.2 helpers ───────────────────────────────────────────────────────

    def _invoke_staging_hook(self, task_id: str) -> None:
        """Invoke the optional staging hook just before a task becomes READY.

        Used by the sidecar pool to mv per-task workspace assets from a
        private staging dir to the agent-visible /workspace/<task>/, plus
        chown to the agent uid. Failures are logged but do not block
        promotion (the agent will see an empty workspace and likely fail
        the task — visible in events.jsonl as a non-stage_failed event).
        """
        if self._staging_hook is None:
            return
        try:
            self._staging_hook(task_id)
        except Exception as e:
            logger.warning("staging hook for %r raised: %s", task_id, e)
            self._event_locked(
                "stage_failed",
                task_id=task_id,
                error=type(e).__name__,
                detail=str(e)[:200],
            )

    def flush(self) -> None:
        """v0.2: dump in-memory state to ``state_dir``. Idempotent.

        Writes ``tasks.json`` (current snapshot) and appends pending
        events from the in-memory buffer to ``events.jsonl``. After
        flushing, the buffer is cleared so a second flush is a no-op
        unless new events accumulate. In non-in-memory mode this is also
        a no-op (already on disk per-tick).

        Designed to be called from a SIGTERM handler or atexit.
        """
        if not self._in_memory:
            return
        with self._lock:
            try:
                self._state_dir.mkdir(parents=True, exist_ok=True)
                self._tasks_path.write_text(
                    json.dumps(
                        {
                            "tasks": [t.to_dict() for t in self._tasks.values()],
                            "stream_done": self._stream_done,
                        },
                        indent=2,
                    )
                )
            except OSError as e:
                logger.warning("flush: failed to write tasks.json: %s", e)

            if self._events_buffer:
                try:
                    with self._events_path.open("a") as f:
                        for entry in self._events_buffer:
                            f.write(json.dumps(entry) + "\n")
                    self._events_buffer.clear()
                except OSError as e:
                    logger.warning(
                        "flush: failed to append events.jsonl: %s", e
                    )

    def _flush_tasks_locked(self) -> None:
        # In-memory mode: skip per-tick disk writes; flush() will dump.
        if self._in_memory:
            return
        try:
            self._tasks_path.write_text(
                json.dumps(
                    {
                        "tasks": [t.to_dict() for t in self._tasks.values()],
                        "stream_done": self._stream_done,
                    },
                    indent=2,
                )
            )
        except OSError as e:
            logger.warning("failed to flush tasks.json: %s", e)

    def _event_locked(self, event: str, **fields: Any) -> None:
        entry = {"event": event, "ts": time.time(), **fields}
        if self._in_memory:
            self._events_buffer.append(entry)
            return
        try:
            with self._events_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.warning("failed to append event: %s", e)


def load_schedule(path: Path) -> tuple[list[Arrival], float, bool]:
    """Load a schedule.json with this shape:

        {
          "max_wall_sec": 600,
          "serial": false,                   # optional, default false
          "arrivals": [
            {"at_sec": 0,  "task_id": "t1", "instruction": "..."},
            {"at_sec": 30, "task_id": "t2", "instruction": "..."}
          ]
        }

    When ``serial=true``, ``at_sec`` is ignored; arrivals are gated on
    the previous arrival reaching a terminal status (continual setting).
    """
    data = json.loads(path.read_text())
    max_wall_sec = float(data.get("max_wall_sec", 600))
    serial = bool(data.get("serial", False))
    arrivals: list[Arrival] = []
    for a in data.get("arrivals", []):
        arrivals.append(
            Arrival(
                task_id=str(a["task_id"]),
                at_sec=float(a["at_sec"]),
                instruction=str(a.get("instruction", "")),
                deadline_sec=(
                    float(a["deadline_sec"]) if "deadline_sec" in a else None
                ),
            )
        )
    return arrivals, max_wall_sec, serial
