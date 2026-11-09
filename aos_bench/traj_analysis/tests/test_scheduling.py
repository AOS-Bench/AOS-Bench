"""Tests for the round-2 scheduling analysis.

The contracts pinned here are the ones that were got wrong on the real data
first and only caught by cross-checking against the pool's own aggregates:

* the concurrency integral runs from the **first claim**, not from t=0;
* ``starve`` (serial *with* backlog) is not ``collapse`` (serial because the
  work ran out) -- the first draft conflated them and labelled 154 healthy
  trials as collapsed;
* Spearman is ``None``, never 0.0, when one side is constant, which is the
  normal case for burst arrivals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aos_bench.traj_analysis import loader, scheduling
from aos_bench.traj_analysis.tests import fixtures


def _record(tmp_path: Path, sched: dict, **kwargs) -> loader.TrialRecord:
    trial_dir = fixtures.write_trial(
        tmp_path, kwargs.pop("job", "main-glm-stream-medium"), kwargs.pop("trial", "s1__X"),
        steps=kwargs.pop("steps", []), scheduling=sched, **kwargs,
    )
    return loader.load_trial(
        trial_dir, job="main-glm-stream-medium", arm="glm", tier="medium", setting="stream"
    )


# --- integration window ----------------------------------------------------


def test_concurrency_integral_starts_at_first_claim_not_zero(tmp_path: Path) -> None:
    """The pool excludes pre-claim dead time; integrating from 0 disagrees.

    Curve: nothing until t=100, then exactly 2 in flight until t=200 (the
    makespan). Integrating over [100,200] gives 2.0 -- the pool's number.
    Integrating over [0,200] would give 1.0, which is what the first draft did
    and what disagreed with `mean_realized_concurrency` on the real data.
    """
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=100.0, terminal=200.0),
            "b": fixtures.task(arrived=0.0, claimed=100.0, terminal=200.0),
        },
        makespan_sec=200.0,
        concurrency=[[100.0, 2], [200.0, 0]],
        backlog=[[0.0, 2], [100.0, 0]],
        mean_concurrency=2.0,
        peak_concurrency=2,
    )
    profile = scheduling.concurrency_profile(_record(tmp_path, sched))
    assert profile.window_start_sec == 100.0
    assert profile.time_weighted_mean == 2.0
    assert profile.reported_mean == 2.0
    assert abs(profile.mean_disagreement) < 1e-9


def test_concurrency_uses_curves_when_present(tmp_path: Path) -> None:
    sched = fixtures.stream_scheduling(
        tasks={"a": fixtures.task(arrived=0.0, claimed=0.0, terminal=10.0)},
        makespan_sec=10.0,
        concurrency=[[0.0, 1], [10.0, 0]],
        backlog=[[0.0, 0]],
    )
    assert scheduling.concurrency_profile(_record(tmp_path, sched)).source == "curves"


def test_concurrency_rebuilt_from_tasks_when_curves_absent(tmp_path: Path) -> None:
    """A missing curve must be rebuilt, not treated as zero concurrency."""
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=0.0, terminal=100.0),
            "b": fixtures.task(arrived=0.0, claimed=0.0, terminal=100.0),
        },
        makespan_sec=100.0,
        concurrency=[],
        backlog=[],
        mean_concurrency=2.0,
    )
    profile = scheduling.concurrency_profile(_record(tmp_path, sched))
    assert profile.source == "rebuilt_from_tasks"
    assert profile.peak == 2


def test_rebuild_keeps_unterminated_task_in_flight() -> None:
    """A claimed task with no terminal stamp was still held when time ran out."""
    points = scheduling.rebuild_concurrency(
        {"a": fixtures.task(arrived=0.0, claimed=5.0, terminal=None, status="in_progress")}
    )
    assert points == [(5.0, 1.0)]


# --- starve vs drain -------------------------------------------------------


def test_serial_with_no_backlog_is_drain_not_starve(tmp_path: Path) -> None:
    """Finishing the last task serially is not a failure to parallelise.

    This is the case that made ``collapse_frac`` useless as a classifier: the
    agent held 4 tasks, finished three, and ran the last one alone because
    there was nothing else to claim. collapse_frac is high; starved_frac is 0,
    and the shape is ``drain`` (wound down) rather than ``starve`` (gave up).
    """
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=0.0, terminal=10.0),
            "b": fixtures.task(arrived=0.0, claimed=0.0, terminal=15.0),
            "c": fixtures.task(arrived=0.0, claimed=0.0, terminal=20.0),
            "d": fixtures.task(arrived=0.0, claimed=0.0, terminal=100.0),
        },
        makespan_sec=100.0,
        concurrency=[[0.0, 4], [10.0, 3], [15.0, 2], [20.0, 1], [100.0, 0]],
        backlog=[[0.0, 0]],
        mean_concurrency=1.45,
        peak_concurrency=4,
    )
    profile = scheduling.concurrency_profile(_record(tmp_path, sched))
    assert profile.collapse_frac > 0.7
    assert profile.starved_frac == 0.0
    # q1 averages ~2.9 over the 4->1 wind-down, q3 is flat at 1.
    assert profile.ramp_ratio < 0.75
    assert profile.shape == "drain"


def test_serial_with_standing_backlog_is_starve(tmp_path: Path) -> None:
    """One task at a time while the pool has ready work queued is the pathology."""
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=0.0, terminal=10.0),
            "b": fixtures.task(arrived=0.0, claimed=0.0, terminal=100.0),
            "c": fixtures.task(arrived=0.0, claimed=None, terminal=None, status="ready"),
        },
        makespan_sec=100.0,
        concurrency=[[0.0, 2], [10.0, 1], [100.0, 0]],
        backlog=[[0.0, 1]],
        mean_concurrency=1.1,
        peak_concurrency=2,
        n_unclaimed=1,
    )
    profile = scheduling.concurrency_profile(_record(tmp_path, sched))
    assert profile.starved_frac > 0.8
    assert profile.shape == "starve"


def test_starved_frac_is_none_without_backlog_curve(tmp_path: Path) -> None:
    """Missing backlog must not be silently read as 'no queue'."""
    sched = fixtures.stream_scheduling(
        tasks={"a": fixtures.task(arrived=0.0, claimed=0.0, terminal=100.0)},
        makespan_sec=100.0,
        concurrency=[[0.0, 1], [100.0, 0]],
        backlog=[],
    )
    assert scheduling.concurrency_profile(_record(tmp_path, sched)).starved_frac is None


def test_never_parallel_is_serial_with_no_collapse_frac(tmp_path: Path) -> None:
    """An agent that never reached 2 did not 'give up'; collapse_frac is None."""
    sched = fixtures.stream_scheduling(
        tasks={"a": fixtures.task(arrived=0.0, claimed=0.0, terminal=100.0)},
        makespan_sec=100.0,
        concurrency=[[0.0, 1], [100.0, 0]],
        backlog=[[0.0, 0]],
        mean_concurrency=1.0,
    )
    profile = scheduling.concurrency_profile(_record(tmp_path, sched))
    assert profile.ever_reached_2 is False
    assert profile.collapse_frac is None
    assert profile.shape == "serial"


def test_ramp_shape_detected(tmp_path: Path) -> None:
    sched = fixtures.stream_scheduling(
        tasks={"a": fixtures.task(arrived=0.0, claimed=0.0, terminal=300.0)},
        makespan_sec=300.0,
        concurrency=[[0.0, 1], [100.0, 3], [200.0, 5], [300.0, 0]],
        backlog=[[0.0, 0]],
        mean_concurrency=3.0,
        peak_concurrency=5,
    )
    profile = scheduling.concurrency_profile(_record(tmp_path, sched))
    assert profile.conc_q1 == 1.0
    assert profile.conc_q3 == 5.0
    assert profile.ramp_ratio == 5.0
    assert profile.shape == "ramp"


# --- queueing --------------------------------------------------------------


def test_wait_arrival_correlation_is_none_for_burst(tmp_path: Path) -> None:
    """All-at-t=0 arrivals carry no order, so the correlation is undefined.

    Reporting 0.0 here would read as "no starvation", which is a claim the
    data cannot make.
    """
    sched = fixtures.stream_scheduling(
        tasks={
            name: fixtures.task(arrived=0.0, claimed=claimed, terminal=claimed + 10)
            for name, claimed in (("a", 1.0), ("b", 5.0), ("c", 9.0), ("d", 20.0))
        },
        makespan_sec=100.0,
        concurrency=[[1.0, 1], [100.0, 0]],
        backlog=[[0.0, 3]],
    )
    assert scheduling.queue_profile(_record(tmp_path, sched)).wait_arrival_slope_r is None


def test_late_arrivals_waiting_longer_flags_starvation(tmp_path: Path) -> None:
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=10.0, terminal=20.0),
            "b": fixtures.task(arrived=10.0, claimed=110.0, terminal=120.0),
            "c": fixtures.task(arrived=20.0, claimed=320.0, terminal=330.0),
            "d": fixtures.task(arrived=30.0, claimed=630.0, terminal=640.0),
        },
        makespan_sec=700.0,
        concurrency=[[10.0, 1], [700.0, 0]],
        backlog=[[0.0, 3]],
    )
    queue = scheduling.queue_profile(_record(tmp_path, sched))
    assert queue.wait_arrival_slope_r == pytest.approx(1.0)
    assert queue.starves_late_arrivals is True


def test_unclaimed_tasks_are_counted_and_named(tmp_path: Path) -> None:
    sched = fixtures.stream_scheduling(
        tasks={
            "done": fixtures.task(arrived=0.0, claimed=1.0, terminal=50.0),
            "left": fixtures.task(arrived=0.0, claimed=None, terminal=None, status="ready"),
        },
        makespan_sec=100.0,
        concurrency=[[1.0, 1], [50.0, 0]],
        backlog=[[0.0, 1]],
        n_unclaimed=1,
    )
    queue = scheduling.queue_profile(_record(tmp_path, sched))
    assert queue.n_never_claimed == 1
    assert queue.unclaimed_task_ids == ["left"]
    assert queue.n_claimed == 1
    assert queue.unclaimed_idle_sec == 100.0


def test_hol_blocked_frac_low_when_agent_idle_while_queue_waits(tmp_path: Path) -> None:
    """Waiting behind an idle agent is not head-of-line blocking.

    Task ``b`` waits 90 s while the agent holds only 1 task and its own peak is
    2, so almost none of that wait is attributable to capacity.
    """
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=0.0, terminal=200.0),
            "b": fixtures.task(arrived=10.0, claimed=100.0, terminal=200.0),
        },
        makespan_sec=200.0,
        concurrency=[[0.0, 1], [100.0, 2], [200.0, 0]],
        backlog=[[10.0, 1], [100.0, 0]],
        peak_concurrency=2,
    )
    assert scheduling.queue_profile(_record(tmp_path, sched)).hol_blocked_frac == 0.0


# --- ordering --------------------------------------------------------------


def test_forced_fifo_when_backlog_never_exceeds_one(tmp_path: Path) -> None:
    """With no choice on offer, 'FIFO' describes the pool, not the agent."""
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=1.0, terminal=50.0),
            "b": fixtures.task(arrived=60.0, claimed=61.0, terminal=90.0),
            "c": fixtures.task(arrived=100.0, claimed=101.0, terminal=150.0),
        },
        makespan_sec=200.0,
        concurrency=[[1.0, 1], [150.0, 0]],
        backlog=[[0.0, 0]],
    )
    ordering = scheduling.ordering_profile(_record(tmp_path, sched))
    assert ordering.had_choice is False
    assert ordering.verdict == "forced-fifo"


def test_triage_short_first_detected(tmp_path: Path) -> None:
    """Claim order negatively correlated with service time = cheap work first."""
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=10.0, terminal=1010.0),
            "b": fixtures.task(arrived=0.0, claimed=20.0, terminal=520.0),
            "c": fixtures.task(arrived=0.0, claimed=30.0, terminal=230.0),
            "d": fixtures.task(arrived=0.0, claimed=40.0, terminal=90.0),
        },
        makespan_sec=1100.0,
        concurrency=[[10.0, 4], [1100.0, 0]],
        # Backlog stays >=2 for most of the run: the agent genuinely had a
        # choice, so the ordering it picked is attributable to the agent.
        backlog=[[0.0, 4], [1000.0, 0]],
    )
    ordering = scheduling.ordering_profile(_record(tmp_path, sched))
    assert ordering.triage_r == pytest.approx(-1.0)
    assert ordering.verdict == "triage-short-first"


# --- spearman --------------------------------------------------------------


def test_spearman_returns_none_for_constant_input() -> None:
    assert scheduling.spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None


def test_spearman_returns_none_below_three_pairs() -> None:
    assert scheduling.spearman([1, 2], [1, 2]) is None


def test_spearman_handles_ties_with_average_ranks() -> None:
    """Perfectly monotone with a tie block stays high, not undefined."""
    r = scheduling.spearman([1, 2, 2, 3], [1, 2, 2, 3])
    assert r is not None and r > 0.9


def test_spearman_perfect_inverse() -> None:
    assert scheduling.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_compute_carries_setting_and_variant(tmp_path: Path) -> None:
    trial_dir = fixtures.write_trial(
        tmp_path, "cfg-glm-stream-heavy-2c4g", "v1-heavy-20-a__X", steps=[],
        scheduling=fixtures.stream_scheduling(
            tasks={"a": fixtures.task(arrived=0.0, claimed=0.0, terminal=10.0)},
            makespan_sec=10.0,
            concurrency=[[0.0, 1], [10.0, 0]],
            backlog=[[0.0, 0]],
        ),
    )
    record = loader.load_trial(
        trial_dir, job="cfg-glm-stream-heavy-2c4g", arm="glm", tier="heavy",
        setting="stream", variant="2c4g",
    )
    out = scheduling.compute(record)
    assert (out.setting, out.variant) == ("stream", "2c4g")


# --- fetch behaviour -------------------------------------------------------


def _pool_step(step_id: int, tool: str) -> dict:
    return fixtures.step(step_id, f"2026-09-19T03:00:{step_id % 60:02d}.000Z", tool=tool)


def test_fetch_profile_counts_todo_next_against_claims(tmp_path: Path) -> None:
    """One poll per claim with work left over = the agent stopped asking.

    This is the glm heavy-plus mechanism: claiming requires ``todo_next``, so
    unclaimed work with no further polls is a fetch decision, not a capacity
    limit.
    """
    steps = [_pool_step(i, "mcp__aos_pool__todo_next") for i in range(2)]
    steps += [_pool_step(i + 10, "Bash") for i in range(200)]
    sched = fixtures.stream_scheduling(
        tasks={
            f"t{i}": fixtures.task(
                arrived=float(i),
                claimed=float(i) if i < 2 else None,
                terminal=100.0 + i if i < 2 else None,
                status="completed" if i < 2 else "ready",
            )
            for i in range(30)
        },
        makespan_sec=1000.0,
        concurrency=[[0.0, 1], [1000.0, 0]],
        backlog=[[0.0, 28]],
        n_unclaimed=28,
    )
    fetch = scheduling.fetch_profile(_record(tmp_path, sched, steps=steps))
    assert fetch.n_todo_next == 2
    assert fetch.n_claimed == 2
    assert fetch.polls_per_claim == 1.0
    assert fetch.claimed_share == pytest.approx(2 / 30)
    assert fetch.stopped_asking is True


def test_fetch_profile_tight_poll_loop_draining_the_stream(tmp_path: Path) -> None:
    """Many polls per claim with everything claimed is the healthy style."""
    steps = [_pool_step(i, "mcp__aos_pool__todo_next") for i in range(40)]
    sched = fixtures.stream_scheduling(
        tasks={
            f"t{i}": fixtures.task(arrived=float(i), claimed=float(i), terminal=100.0 + i)
            for i in range(4)
        },
        makespan_sec=200.0,
        concurrency=[[0.0, 4], [200.0, 0]],
        backlog=[[0.0, 0]],
    )
    fetch = scheduling.fetch_profile(_record(tmp_path, sched, steps=steps))
    assert fetch.polls_per_claim == 10.0
    assert fetch.claimed_share == 1.0
    assert fetch.stopped_asking is False


def test_fetch_profile_counts_all_pool_calls_separately(tmp_path: Path) -> None:
    steps = [
        _pool_step(0, "mcp__aos_pool__todo_next"),
        _pool_step(1, "mcp__aos_pool__todo_update"),
        _pool_step(2, "Bash"),
    ]
    sched = fixtures.stream_scheduling(
        tasks={"t0": fixtures.task(arrived=0.0, claimed=0.0, terminal=10.0)},
        makespan_sec=10.0,
        concurrency=[[0.0, 1], [10.0, 0]],
        backlog=[[0.0, 0]],
    )
    fetch = scheduling.fetch_profile(_record(tmp_path, sched, steps=steps))
    assert (fetch.n_pool_calls, fetch.n_todo_next) == (2, 1)


def test_reward_decomposition_separates_fetching_from_solving(tmp_path: Path) -> None:
    """A pure work-acquisition failure must not look like a solving failure.

    The agent claims 2 of 4 tasks and solves BOTH perfectly. Headline reward is
    0.5 because unclaimed tasks score 0; `reward_on_claimed` is 1.0. Reporting
    only the headline would call this a capability result when the model solved
    everything it took on.
    """
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=0.0, terminal=10.0, reward=1.0),
            "b": fixtures.task(arrived=0.0, claimed=1.0, terminal=20.0, reward=1.0),
            "c": fixtures.task(arrived=0.0, claimed=None, terminal=None, status="ready"),
            "d": fixtures.task(arrived=0.0, claimed=None, terminal=None, status="ready"),
        },
        makespan_sec=100.0,
        concurrency=[[0.0, 2], [20.0, 0]],
        backlog=[[0.0, 2]],
        n_unclaimed=2,
    )
    fetch = scheduling.fetch_profile(_record(tmp_path, sched))
    assert fetch.claimed_share == 0.5
    assert fetch.reward_on_claimed == 1.0
    assert fetch.reward_all_tasks == 0.5


def test_reward_decomposition_flags_a_solving_failure(tmp_path: Path) -> None:
    """Claimed everything and scored badly: both columns low, no forfeit."""
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=0.0, terminal=10.0, reward=0.0),
            "b": fixtures.task(arrived=0.0, claimed=1.0, terminal=20.0, reward=0.0),
        },
        makespan_sec=100.0,
        concurrency=[[0.0, 2], [20.0, 0]],
        backlog=[[0.0, 0]],
    )
    fetch = scheduling.fetch_profile(_record(tmp_path, sched))
    assert fetch.claimed_share == 1.0
    assert fetch.reward_on_claimed == 0.0
    assert fetch.reward_all_tasks == 0.0
