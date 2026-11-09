"""Tests for paired-by-task_id comparison.

The property that matters most is the one that motivated the module: when the
two cohorts cover *different task mixes*, the unpaired delta and the paired
delta must differ, and the paired one must be the restriction to shared ids.
A regression here silently reintroduces the light->medium error (+0.34 vs
+0.089) into every A/B in the report.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aos_bench.traj_analysis import loader, paired
from aos_bench.traj_analysis.tests import fixtures


def _record(tmp_path: Path, job: str, trial: str, rewards: dict[str, float]) -> loader.TrialRecord:
    trial_dir = fixtures.write_trial(tmp_path, job, trial, steps=[], rewards=rewards)
    return loader.load_trial(trial_dir, job=job, arm="glm", tier="light", setting="stream")


def test_pairing_ignores_tasks_only_one_side_attempted(tmp_path: Path) -> None:
    """Extra easy tasks on one side must not inflate the delta.

    A has {x:0, y:0}; B has {x:0, y:0, easy:1}. Unpaired, B looks better by
    +0.33. Paired on the shared ids, B is identical to A. This is the exact
    shape of the light-vs-medium trap.
    """
    a = [_record(tmp_path, "main-glm-stream-light", "s1__a", {"x": 0.0, "y": 0.0})]
    b = [_record(tmp_path, "main-glm-burst-light", "s1__b", {"x": 0.0, "y": 0.0, "easy": 1.0})]
    out = paired.compare(a, b, label_a="A", label_b="B")
    assert out.n_shared_task_ids == 2
    assert out.unpaired_delta == pytest.approx(1 / 3)
    assert out.paired_delta == 0.0


def test_repeated_attempts_at_one_task_are_averaged_not_weighted(tmp_path: Path) -> None:
    """Three attempts on one side must not triple that task's weight.

    A attempts task ``x`` three times scoring 0/0/1 and task ``y`` once
    scoring 1. B attempts each once, scoring 1 and 1. Averaging within the id
    first gives A = mean(1/3, 1) = 0.667; flattening would give 0.5.
    """
    a = [
        _record(tmp_path, "main-glm-stream-light", "s1__a1", {"x": 0.0}),
        _record(tmp_path, "main-glm-stream-light", "s1__a2", {"x": 0.0}),
        _record(tmp_path, "main-glm-stream-light", "s1__a3", {"x": 1.0, "y": 1.0}),
    ]
    b = [_record(tmp_path, "main-glm-burst-light", "s1__b", {"x": 1.0, "y": 1.0})]
    out = paired.compare(a, b, label_a="A", label_b="B")
    assert out.paired_mean_a == pytest.approx(2 / 3)
    assert out.paired_mean_b == 1.0


def test_no_shared_ids_reports_incomparable(tmp_path: Path) -> None:
    a = [_record(tmp_path, "main-glm-stream-light", "s1__a", {"x": 1.0})]
    b = [_record(tmp_path, "main-glm-burst-light", "s2__b", {"z": 1.0})]
    out = paired.compare(a, b, label_a="A", label_b="B")
    assert out.n_shared_task_ids == 0
    assert out.paired_delta is None
    assert "not comparable" in out.confidence


def test_trials_without_reward_file_contribute_nothing(tmp_path: Path) -> None:
    """A missing verifier is not a zero score."""
    a = [
        _record(tmp_path, "main-glm-stream-light", "s1__a", {"x": 1.0}),
        _record(tmp_path, "main-glm-stream-light", "s1__b", {}),
    ]
    b = [_record(tmp_path, "main-glm-burst-light", "s1__c", {"x": 1.0})]
    out = paired.compare(a, b, label_a="A", label_b="B")
    assert out.paired_mean_a == 1.0
    assert out.paired_delta == 0.0


def test_single_stream_on_one_side_is_flagged(tmp_path: Path) -> None:
    a = [
        _record(tmp_path, "main-glm-stream-light", f"s{i}__a", {f"t{j}": 1.0 for j in range(6)})
        for i in range(3)
    ]
    b = [_record(tmp_path, "main-glm-burst-light", "s1__b", {f"t{j}": 0.0 for j in range(6)})]
    out = paired.compare(a, b, label_a="A", label_b="B")
    assert "SINGLE stream" in out.confidence


def test_sign_test_two_sided_exact() -> None:
    # 5 up, 0 down: two-sided p = 2 * (1/2)^5 = 0.0625.
    assert paired.sign_test_p(5, 0) == pytest.approx(0.0625)
    # Balanced: p = 1.0.
    assert paired.sign_test_p(3, 3) == pytest.approx(1.0)
    # No non-ties: undefined, not 1.0.
    assert paired.sign_test_p(0, 0) is None


def test_compare_metric_pairs_by_stream_and_drops_unshared(tmp_path: Path) -> None:
    """A half-finished job must not compare its streams against a full set."""
    a = [
        _record(tmp_path, "main-glm-stream-light", "s1__a", {"x": 1.0}),
        _record(tmp_path, "main-glm-stream-light", "s2__a", {"x": 1.0}),
    ]
    b = [_record(tmp_path, "main-glm-burst-light", "s1__b", {"x": 1.0})]
    values = {a[0].path: 10.0, a[1].path: 100.0, b[0].path: 20.0}
    out = paired.compare_metric(a, b, lambda r: values[r.path])
    # Unpaired A averages the unshared s2 in; by-stream does not.
    assert out["unpaired_a"] == pytest.approx(55.0)
    assert out["n_shared_streams"] == 1
    assert out["by_stream_a"] == 10.0
    assert out["by_stream_delta"] == 10.0


def test_repeat_spread_measures_the_noise_floor(tmp_path: Path) -> None:
    """Three attempts at one stream scoring 0.2/0.5/0.9: range 0.7.

    This is the number that decides whether a between-condition delta is
    interpretable at all, so it must come from repeats of the *same* stream
    and must ignore streams with only one attempt.
    """
    records = [
        _record(tmp_path, "main-glm-stream-heavy", "s1__a", {"x": 1.0}),
        _record(tmp_path, "main-glm-stream-heavy", "s1__b", {"x": 1.0}),
        _record(tmp_path, "main-glm-stream-heavy", "s1__c", {"x": 1.0}),
        _record(tmp_path, "main-glm-stream-heavy", "s2__d", {"x": 1.0}),
    ]
    values = dict(zip((r.path for r in records), (0.2, 0.5, 0.9, 0.4)))
    out = paired.repeat_spread(records, lambda r: values[r.path])
    # s2 has a single attempt and contributes nothing.
    assert out["n_streams"] == 1
    assert out["n_trials"] == 3
    assert out["mean_range"] == pytest.approx(0.7)


def test_repeat_spread_empty_when_no_stream_repeats(tmp_path: Path) -> None:
    records = [_record(tmp_path, "main-glm-stream-heavy", "s1__a", {"x": 1.0})]
    out = paired.repeat_spread(records, lambda r: 0.5)
    assert out["n_streams"] == 0
    assert out["mean_range"] is None


def test_bootstrap_resamples_streams_not_task_ids(tmp_path: Path) -> None:
    """The CI must reflect between-stream variation, not within-stream ids.

    Two streams disagree completely: one where burst wins every task, one
    where it loses every task. Resampling whole streams must therefore
    produce an interval that straddles zero. Resampling task ids would pool
    all 20 ids and report a spuriously tight interval around ~0.
    """
    # Half the streams favour burst completely, half favour stream completely.
    a, b = [], []
    for i in range(paired.MIN_STREAMS_FOR_CI):
        lo, hi = (0.0, 1.0) if i % 2 == 0 else (1.0, 0.0)
        a.append(
            _record(tmp_path, "main-glm-stream-light", f"s{i}__a", {f"t{i}_{j}": lo for j in range(10)})
        )
        b.append(
            _record(tmp_path, "main-glm-burst-light", f"s{i}__b", {f"t{i}_{j}": hi for j in range(10)})
        )
    out = paired.bootstrap_delta_ci(a, b, n_resamples=500)
    assert out["n_streams"] == paired.MIN_STREAMS_FOR_CI
    assert out["point"] == pytest.approx(0.0)
    assert out["crosses_zero"] is True
    assert out["ci_low"] < -0.3 and out["ci_high"] > 0.3


def test_bootstrap_refuses_a_single_stream(tmp_path: Path) -> None:
    """One stream cannot support a bootstrap; say so instead of emitting one."""
    a = [_record(tmp_path, "main-glm-stream-light", "s1__a", {"x": 0.0})]
    b = [_record(tmp_path, "main-glm-burst-light", "s1__b", {"x": 1.0})]
    out = paired.bootstrap_delta_ci(a, b)
    assert out["ci_low"] is None
    assert "cannot produce an honest interval" in out["note"]


def test_bootstrap_is_deterministic_for_a_fixed_seed(tmp_path: Path) -> None:
    a = [
        _record(tmp_path, "main-glm-stream-light", f"s{i}__a", {f"t{i}": 0.0})
        for i in range(paired.MIN_STREAMS_FOR_CI)
    ]
    b = [
        _record(tmp_path, "main-glm-burst-light", f"s{i}__b", {f"t{i}": 1.0})
        for i in range(paired.MIN_STREAMS_FOR_CI)
    ]
    first = paired.bootstrap_delta_ci(a, b, n_resamples=200)
    second = paired.bootstrap_delta_ci(a, b, n_resamples=200)
    assert (first["ci_low"], first["ci_high"]) == (second["ci_low"], second["ci_high"])


def test_bootstrap_refuses_below_the_cluster_minimum(tmp_path: Path) -> None:
    """Three shared streams cannot support a CI, even though it would compute.

    Observed on real data: glm-burst-heavy produced [-0.350, -0.148] -- an
    interval excluding zero -- from 3 shared streams where two contributed a
    single burst trial each and the worst stream was missing from one side. A
    percentile interval over 3 clusters is arithmetic, not inference.
    """
    a = [
        _record(tmp_path, "main-glm-stream-heavy", f"s{i}__a", {f"t{i}": 1.0})
        for i in range(3)
    ]
    b = [
        _record(tmp_path, "main-glm-burst-heavy", f"s{i}__b", {f"t{i}": 0.0})
        for i in range(3)
    ]
    out = paired.bootstrap_delta_ci(a, b)
    assert out["ci_low"] is None
    assert str(paired.MIN_STREAMS_FOR_CI) in out["note"]


def test_bootstrap_runs_at_the_cluster_minimum(tmp_path: Path) -> None:
    a = [
        _record(tmp_path, "main-glm-stream-light", f"s{i}__a", {f"t{i}": 0.0})
        for i in range(paired.MIN_STREAMS_FOR_CI)
    ]
    b = [
        _record(tmp_path, "main-glm-burst-light", f"s{i}__b", {f"t{i}": 1.0})
        for i in range(paired.MIN_STREAMS_FOR_CI)
    ]
    out = paired.bootstrap_delta_ci(a, b, n_resamples=300)
    assert out["ci_low"] is not None
    assert out["point"] == pytest.approx(1.0)
