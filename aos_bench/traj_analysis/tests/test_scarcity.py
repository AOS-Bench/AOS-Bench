"""Tests for the scarcity (bindingness) analysis.

The point of this module is to make a *negative* result reportable, so the
tests pin that a non-binding limit reads as "not binding" rather than as
missing data, and that each limit is judged against the trial's **own**
configured budget. A hardcoded budget would silently turn "used 18% of the
wall clock" into whatever the last job's timeout happened to be.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aos_bench.traj_analysis import loader, scarcity
from aos_bench.traj_analysis.tests import fixtures


def _record(tmp_path: Path, **kwargs) -> loader.TrialRecord:
    job = kwargs.pop("job", "main-glm-stream-heavy")
    trial_dir = fixtures.write_trial(tmp_path, job, kwargs.pop("trial", "s1__X"), **kwargs)
    return loader.load_trial(trial_dir, job=job, arm="glm", tier="heavy")


def _cgroup(n: int, *, usage_per_window: int, cores: int = 4, mem: int = 1 << 30) -> list[str]:
    return [
        fixtures.cgroup_row(
            i,
            memory_current=mem,
            memory_peak=mem,
            cpu_usage_usec=i * usage_per_window,
            pids_current=10,
            cpu_quota_usec=cores * 100000,
            memory_max=8589934592,
        )
        for i in range(n)
    ]


def test_time_budget_is_read_from_the_trials_own_config(tmp_path: Path) -> None:
    """The fixture grants 43200 s and uses 15 min of agent_execution."""
    record = _record(tmp_path, steps=[])
    profile = scarcity.compute(record)
    assert profile.granted_wall_sec == 43200.0
    assert profile.used_wall_sec == pytest.approx(900.0)
    assert profile.time_used_share == pytest.approx(900.0 / 43200.0)
    assert profile.time_binding is False


def test_idle_cpu_is_reported_as_not_binding(tmp_path: Path) -> None:
    """A container loafing at 5% of quota must read 'not binding', not 'unknown'."""
    # 4 cores * 5 s = 20 core-seconds available; consume 1 -> 5% utilisation.
    record = _record(tmp_path, steps=[], cgroup_rows=_cgroup(6, usage_per_window=1_000_000))
    profile = scarcity.compute(record)
    assert profile.cpu_idle_share == 1.0
    assert profile.cpu_binding is False
    assert "cpu" not in profile.binding_limits


def test_saturated_cpu_is_reported_as_binding(tmp_path: Path) -> None:
    record = _record(tmp_path, steps=[], cgroup_rows=_cgroup(6, usage_per_window=20_000_000))
    profile = scarcity.compute(record)
    assert profile.cpu_idle_share == 0.0
    assert profile.cpu_binding is True
    assert "cpu" in profile.binding_limits


def test_memory_above_the_cgroup_limit_cannot_prove_binding(tmp_path: Path) -> None:
    """Samples above memory.max are a sampler artifact, not 12 GiB of usage.

    The sampler occasionally reads a parent cgroup. Letting those samples set
    the peak would let a measurement bug 'prove' that memory was the binding
    constraint.
    """
    rows = [
        fixtures.cgroup_row(
            i,
            memory_current=12 * (1 << 30),
            memory_peak=12 * (1 << 30),
            cpu_usage_usec=i,
            pids_current=10,
            cpu_quota_usec=400000,
            memory_max=8589934592,
        )
        for i in range(4)
    ]
    profile = scarcity.compute(_record(tmp_path, steps=[], cgroup_rows=rows))
    assert profile.mem_peak_share is None
    assert profile.mem_binding is None


def test_queue_binds_when_backlog_stands_or_work_is_left(tmp_path: Path) -> None:
    sched = fixtures.stream_scheduling(
        tasks={
            "a": fixtures.task(arrived=0.0, claimed=0.0, terminal=100.0),
            "b": fixtures.task(arrived=0.0, claimed=None, terminal=None, status="ready"),
        },
        makespan_sec=100.0,
        concurrency=[[0.0, 1], [100.0, 0]],
        backlog=[[0.0, 3]],
        n_unclaimed=1,
    )
    profile = scarcity.compute(_record(tmp_path, steps=[], scheduling=sched))
    assert profile.frac_time_backlog_ge2 == pytest.approx(1.0)
    assert profile.queue_binding is True


def test_queue_does_not_bind_when_the_pool_drip_feeds(tmp_path: Path) -> None:
    """If the pool never offers a second task, there is no scheduling problem."""
    sched = fixtures.stream_scheduling(
        tasks={"a": fixtures.task(arrived=0.0, claimed=0.0, terminal=100.0)},
        makespan_sec=100.0,
        concurrency=[[0.0, 1], [100.0, 0]],
        backlog=[[0.0, 0]],
        n_unclaimed=0,
    )
    profile = scarcity.compute(_record(tmp_path, steps=[], scheduling=sched))
    assert profile.frac_time_backlog_ge2 == pytest.approx(0.0)
    assert profile.queue_binding is False


def test_verdict_names_no_binding_constraint_explicitly(tmp_path: Path) -> None:
    """The headline interpretive claim must be a stated verdict, not an absence."""
    sched = fixtures.stream_scheduling(
        tasks={"a": fixtures.task(arrived=0.0, claimed=0.0, terminal=100.0)},
        makespan_sec=100.0,
        concurrency=[[0.0, 1], [100.0, 0]],
        backlog=[[0.0, 0]],
    )
    profile = scarcity.compute(
        _record(tmp_path, steps=[], scheduling=sched, cgroup_rows=_cgroup(6, usage_per_window=1_000_000))
    )
    assert profile.binding_limits == []
    assert profile.verdict == "no binding constraint detected"


def test_verdict_is_unknown_when_nothing_is_measurable(tmp_path: Path) -> None:
    """No config, no cgroup, no scheduling => 'unknown', never 'not binding'."""
    trial_dir = fixtures.write_trial(
        tmp_path, "main-glm-stream-heavy", "s1__X", steps=[], write_result=False
    )
    record = loader.load_trial(trial_dir, job="main-glm-stream-heavy", arm="glm", tier="heavy")
    assert scarcity.compute(record).verdict == "unknown"


def test_summarise_separates_heavy_load_idleness(tmp_path: Path) -> None:
    """CPU idle *while heavily loaded* is the load-bearing statistic."""
    idle = scarcity.compute(
        _record(tmp_path, trial="s1__a", steps=[], cgroup_rows=_cgroup(6, usage_per_window=1_000_000))
    )
    busy = scarcity.compute(
        _record(tmp_path, trial="s2__b", steps=[], cgroup_rows=_cgroup(6, usage_per_window=20_000_000))
    )
    out = scarcity.summarise([idle, busy], heavy_load_profiles=[idle])
    assert out["n_trials"] == 2
    assert out["mean_cpu_idle_share"] == pytest.approx(0.5)
    assert out["n_heavy_load"] == 1
    assert out["heavy_load_cpu_idle_share"] == 1.0
