"""Metric and performance arithmetic, including the deliberate non-answers.

Two tests here exist to pin *refusals*: ``tool_time_recoverable`` must stay
False (per-tool-call durations are absent from every artifact), and a
throughput disagreement must be surfaced rather than reconciled.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aos_bench.traj_analysis import loader, metrics, performance, report
from aos_bench.traj_analysis.tests import fixtures


def _record(tmp_path: Path, **kwargs) -> loader.TrialRecord:
    fixtures.write_trial(tmp_path, kwargs.pop("job", "main-codex-stream-light"), kwargs.pop("trial", "a__X"), **kwargs)
    return loader.discover_trials(tmp_path)[0]


def test_tool_histogram_and_fanout(tmp_path: Path) -> None:
    steps = [
        fixtures.step(1, "2026-09-19T03:00:01.000Z", tool="Bash", arguments={"command": "ls"}),
        fixtures.step(2, "2026-09-19T03:00:02.000Z", tool="Bash", arguments={"command": "pwd"}),
        fixtures.step(3, "2026-09-19T03:00:03.000Z", tool="Agent", arguments={"prompt": "go"}),
        fixtures.step(4, "2026-09-19T03:00:04.000Z", tool="mcp__aos_pool__todo_next", arguments={}),
        fixtures.step(5, "2026-09-19T03:00:05.000Z", source="user"),
    ]
    out = metrics.compute(_record(tmp_path, steps=steps))
    assert out.tool_histogram == {"Bash": 2, "Agent": 1, "mcp__aos_pool__todo_next": 1}
    assert out.n_tool_calls == 4
    assert out.n_fanout == 1
    assert out.n_pool_calls == 1
    assert out.n_steps == 5
    assert out.n_agent_steps == 4
    # step 5 is a user step and step with no tool call is not a turn
    assert out.n_turns == 4


def test_mixed_timestamp_formats_both_parse(tmp_path: Path) -> None:
    """codex/claude-code emit ``Z``; kimi-code emits ``+00:00``."""
    steps = [
        fixtures.step(1, "2026-09-19T03:00:00.000Z", tool="Bash", arguments={"command": "a"}),
        fixtures.step(2, "2026-09-19T03:00:10.000000+00:00", tool="Bash", arguments={"command": "b"}),
        fixtures.step(3, "2026-09-19T03:00:40.000Z", tool="Bash", arguments={"command": "c"}),
    ]
    out = metrics.compute(_record(tmp_path, steps=steps))
    assert out.n_step_deltas == 2
    assert out.step_delta_mean_sec == 20.0
    assert out.step_delta_median_sec == 20.0
    assert out.step_delta_max_sec == 30.0
    assert out.frac_steps_under_10s == 0.0


def test_unparseable_timestamp_is_skipped_not_fatal(tmp_path: Path) -> None:
    steps = [
        fixtures.step(1, "2026-09-19T03:00:00.000Z", tool="Bash", arguments={"command": "a"}),
        fixtures.step(2, "not-a-timestamp", tool="Bash", arguments={"command": "b"}),
        fixtures.step(3, "2026-09-19T03:00:04.000Z", tool="Bash", arguments={"command": "c"}),
    ]
    out = metrics.compute(_record(tmp_path, steps=steps))
    assert out.n_step_deltas == 1
    assert out.frac_steps_under_10s == 1.0


def test_cpu_util_proxy_uses_four_cores_and_five_seconds(tmp_path: Path) -> None:
    """1 core fully busy for 5s => 5e6 usec => util 0.25 on a 4-core quota."""
    rows = [
        fixtures.cgroup_row(0, memory_current=0, memory_peak=0, cpu_usage_usec=0, pids_current=1),
        fixtures.cgroup_row(1, memory_current=0, memory_peak=0, cpu_usage_usec=5_000_000, pids_current=1),
        fixtures.cgroup_row(2, memory_current=0, memory_peak=0, cpu_usage_usec=25_000_000, pids_current=1),
    ]
    out = metrics.compute(_record(tmp_path, steps=[], cgroup_rows=rows))
    assert out.cpu_cores == 4.0
    assert out.cpu_util_mean is not None
    assert abs(out.cpu_util_peak - 1.0) < 1e-9  # 20e6 usec over 5s on 4 cores
    assert abs(out.cpu_util_mean - 0.625) < 1e-9
    # Real throttling counters are unavailable; this must be reported as such.
    assert out.cpu_throttle_data_available is False


def test_memory_percent_of_8gib_limit(tmp_path: Path) -> None:
    gib = 1024 ** 3
    rows = [
        fixtures.cgroup_row(0, memory_current=gib, memory_peak=gib, cpu_usage_usec=0, pids_current=1),
        fixtures.cgroup_row(1, memory_current=3 * gib, memory_peak=4 * gib, cpu_usage_usec=1, pids_current=9),
    ]
    out = metrics.compute(_record(tmp_path, steps=[], cgroup_rows=rows))
    assert abs(out.mem_mean_pct_limit - 25.0) < 1e-6  # mean 2 GiB of 8 GiB
    assert abs(out.mem_peak_pct_limit - 50.0) < 1e-6
    assert out.pids_peak == 9


def test_reasoning_tokens_absent_stays_none(tmp_path: Path) -> None:
    """glm/cc/kimi omit reasoning_output_tokens; None must not become 0."""
    fm = {
        "total_prompt_tokens": 100,
        "total_completion_tokens": 10,
        "total_cached_tokens": 80,
        "total_steps": 1,
        "extra": {},
    }
    out = metrics.compute(_record(tmp_path, steps=[], final_metrics=fm))
    assert out.reasoning_tokens is None
    assert out.total_tokens == 110  # derived when extra.total_tokens is absent
    assert abs(out.cache_hit_ratio - 0.8) < 1e-9


def test_pass_rate_per_subtask_and_per_trial(tmp_path: Path) -> None:
    out = performance.compute(
        _record(tmp_path, steps=[], rewards={"a": 1.0, "b": 0.0, "c": 1.0, "d": 0.0})
    )
    assert out.n_tasks == 4
    assert out.n_passed == 2
    assert out.mean_reward == 0.5
    assert out.trial_fully_solved is False


def test_phase_breakdown_and_residual(tmp_path: Path) -> None:
    out = performance.compute(_record(tmp_path, steps=[], rewards={"a": 1.0}))
    assert out.total_wall_sec == 1200.0
    assert out.phase_sec["environment_setup"] == 120.0
    assert out.phase_sec["agent_setup"] == 60.0
    assert out.phase_sec["agent_execution"] == 900.0
    assert out.phase_sec["verifier"] == 90.0
    # 1200 - (120+60+900+90) = 30s of launcher gaps, reported not hidden.
    assert out.phase_residual_sec == 30.0


def test_throughput_agreement(tmp_path: Path) -> None:
    sched = fixtures.scheduling_metrics(n_tasks=5, n_passed=2, makespan_sec=600.0, throughput=30.0)
    out = performance.compute(_record(tmp_path, steps=[], rewards={"a": 1.0}, scheduling=sched))
    assert abs(out.throughput_computed - 30.0) < 1e-6
    assert abs(out.throughput_disagreement) < 1e-6


def test_throughput_disagreement_is_surfaced(tmp_path: Path) -> None:
    """Pool reports over n_tasks; we compute over terminal tasks. Do not reconcile."""
    sched = fixtures.scheduling_metrics(
        n_tasks=20, n_passed=2, makespan_sec=3600.0, throughput=20.0, n_terminal=6, n_unclaimed=14
    )
    fixtures.write_trial(
        tmp_path, "main-codex-stream-heavy", "h__X", steps=[], rewards={"a": 0.0}, scheduling=sched
    )
    bundles = report.analyse(tmp_path)
    assert abs(bundles[0].performance.throughput_computed - 6.0) < 1e-6
    rows = report._throughput_disagreements(bundles)
    assert len(rows) == 1
    assert rows[0]["reported"] == 20.0
    assert rows[0]["n_unclaimed"] == 14


def test_tool_time_is_a_bound_not_a_value(tmp_path: Path) -> None:
    """Per-tool-call durations do not exist in any artifact; only bounds do."""
    steps = [
        fixtures.step(1, "2026-09-19T03:00:00.000Z", tool="Bash", arguments={"command": "sleep"}),
        fixtures.step(2, "2026-09-19T03:00:30.000Z"),  # no tool call
        fixtures.step(3, "2026-09-19T03:00:40.000Z", tool="Bash", arguments={"command": "x"}),
    ]
    out = performance.compute(_record(tmp_path, steps=steps))
    assert out.tool_time_recoverable is False
    # 30s follows a tool step, 10s follows a non-tool step.
    assert abs(out.tool_attributable_frac_upper - 0.75) < 1e-9
    assert abs(out.model_only_frac_lower - 0.25) < 1e-9
    assert "upper bound" in out.tool_time_note


def test_missing_scheduling_leaves_none_not_zero(tmp_path: Path) -> None:
    out = performance.compute(_record(tmp_path, steps=[], rewards={"a": 1.0}))
    assert out.makespan_sec is None
    assert out.throughput_computed is None
    assert out.throughput_disagreement is None


# --- round 2: CPU divisor follows the real sandbox shape -------------------


def test_cpu_util_divides_by_the_trials_own_core_count(tmp_path: Path) -> None:
    """A 2-core sandbox at 1 core-second per 5 s window is 50% utilised.

    With a hardcoded 4-core divisor this reads 25%, which would make the
    halved 2c4g sandbox appear *less* loaded than the 4c8g one it is being
    compared against -- the exact inversion the comparison exists to detect.
    """
    rows = [
        fixtures.cgroup_row(
            i,
            memory_current=1 << 30,
            memory_peak=1 << 30,
            # 1 core-second consumed per 5 s sampling window.
            cpu_usage_usec=i * 1_000_000,
            pids_current=5,
            cpu_quota_usec=200000,
            memory_max=4294967296,
        )
        for i in range(4)
    ]
    trial_dir = fixtures.write_trial(
        tmp_path, "cfg-glm-stream-heavy-2c4g", "a__X", steps=[], cgroup_rows=rows
    )
    record = loader.load_trial(
        trial_dir, job="cfg-glm-stream-heavy-2c4g", arm="glm", tier="heavy", variant="2c4g"
    )
    out = metrics.compute(record)
    assert out.cpu_cores == 2.0
    # 1e6 usec / (2 cores * 5e6 usec) = 0.1
    assert out.cpu_util_mean == pytest.approx(0.1)


def test_memory_percentage_uses_the_trials_own_limit(tmp_path: Path) -> None:
    """2 GiB in a 4 GiB sandbox is 50%, not the 25% an 8 GiB divisor gives."""
    rows = [
        fixtures.cgroup_row(
            i,
            memory_current=2 * (1 << 30),
            memory_peak=2 * (1 << 30),
            cpu_usage_usec=i,
            pids_current=5,
            cpu_quota_usec=200000,
            memory_max=4294967296,
        )
        for i in range(3)
    ]
    trial_dir = fixtures.write_trial(
        tmp_path, "cfg-glm-stream-heavy-2c4g", "a__X", steps=[], cgroup_rows=rows
    )
    record = loader.load_trial(
        trial_dir, job="cfg-glm-stream-heavy-2c4g", arm="glm", tier="heavy", variant="2c4g"
    )
    out = metrics.compute(record)
    assert out.mem_limit_bytes == 4294967296
    assert out.mem_mean_pct_limit == pytest.approx(50.0)


def test_over_limit_note_quotes_the_real_limit(tmp_path: Path) -> None:
    """The artifact note must name 4 GiB for a 4 GiB cgroup, not 8."""
    rows = [
        fixtures.cgroup_row(
            i,
            memory_current=6 * (1 << 30),  # impossible inside a 4 GiB cgroup
            memory_peak=6 * (1 << 30),
            cpu_usage_usec=i,
            pids_current=5,
            cpu_quota_usec=200000,
            memory_max=4294967296,
        )
        for i in range(3)
    ]
    trial_dir = fixtures.write_trial(
        tmp_path, "cfg-glm-stream-heavy-2c4g", "a__X", steps=[], cgroup_rows=rows
    )
    record = loader.load_trial(
        trial_dir, job="cfg-glm-stream-heavy-2c4g", arm="glm", tier="heavy", variant="2c4g"
    )
    out = metrics.compute(record)
    assert out.n_samples_over_mem_limit == 3
    assert "4 GiB" in out.mem_over_limit_note


def test_cpu_saturation_fraction_counts_near_quota_windows(tmp_path: Path) -> None:
    """Share of windows at >=7/8 of quota, on the trial's own core count."""
    # 2 cores: 10 core-seconds per 5 s window = fully saturated.
    rows = [
        fixtures.cgroup_row(
            i,
            memory_current=1 << 30,
            memory_peak=1 << 30,
            cpu_usage_usec=i * 10_000_000,
            pids_current=5,
            cpu_quota_usec=200000,
            memory_max=4294967296,
        )
        for i in range(4)
    ]
    trial_dir = fixtures.write_trial(
        tmp_path, "cfg-glm-stream-heavy-2c4g", "a__X", steps=[], cgroup_rows=rows
    )
    record = loader.load_trial(
        trial_dir, job="cfg-glm-stream-heavy-2c4g", arm="glm", tier="heavy", variant="2c4g"
    )
    assert metrics.compute(record).frac_cpu_windows_near_saturation == 1.0
