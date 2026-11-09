"""Paired-by-task_id comparison between two cohorts.

Unpaired cohort means are not comparable on this benchmark and the reason is
already documented in the round-1 notes: the light and medium task pools
overlap heavily, but medium's 13 extra tasks are much easier (0.790 vs 0.608),
so an unpaired light->medium "improvement" of +0.34 shrinks to +0.089 once the
comparison is restricted to tasks both cohorts actually attempted. The same
trap applies to every comparison we care about in round 2:

* **stream vs burst** -- same 10 light streams, same 50 task ids (verified
  identical per stream), so pairing is exact and the difference is causal with
  respect to the arrival schedule.
* **2c4g vs 4c8g** -- same 5 heavy streams, but the 2c4g job is still running,
  so the *set of streams with data* differs between cohorts and an unpaired
  mean would compare "the 3 streams that finished small" with "all 5 large".

So: one code path, used everywhere, that groups both sides by ``task_id``,
keeps only ids present on both sides, and averages **per task id first** (so a
task attempted 3 times on one side and once on the other is not triple-counted)
before differencing.

Significance: with n in the tens of task ids and rewards that are mostly 0/1, a
t-test's assumptions do not hold. We report instead

* the paired mean difference,
* the number of task ids that improved / worsened / tied,
* a two-sided **exact sign test** p-value over the non-tied ids (stdlib
  binomial, no scipy), which needs no distributional assumption.

A sign-test p-value is about *direction*, not size. It is reported alongside
the effect, never instead of it, and ``n_effective`` (non-tied ids) is always
printed because 3 non-tied ids cannot support any claim however small p is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import comb
from typing import Any, Callable, Iterable, Sequence

from aos_bench.traj_analysis.loader import TrialRecord, stream_id

# A cluster bootstrap needs enough clusters to resample. Below this, a
# percentile interval is arithmetic dressed as inference: with 3 shared streams
# there are only 10 distinct resamples, and glm-burst-heavy produced a CI that
# *excluded zero* off 3 streams where two contributed a single burst trial each
# and the worst-performing stream was absent from one side entirely. The n=5
# glm-burst-medium result looked equally significant and evaporated at n=23.
MIN_STREAMS_FOR_CI = 6

# Below this many trials on the thinner side, flag the comparison as provisional
# regardless of what the interval says.
MIN_TRIALS_PER_SIDE = 8


@dataclass
class PairedComparison:
    label_a: str
    label_b: str
    n_trials_a: int = 0
    n_trials_b: int = 0
    n_streams_a: int = 0
    n_streams_b: int = 0
    n_task_ids_a: int = 0
    n_task_ids_b: int = 0
    n_shared_task_ids: int = 0
    unpaired_mean_a: float | None = None
    unpaired_mean_b: float | None = None
    unpaired_delta: float | None = None
    paired_mean_a: float | None = None
    paired_mean_b: float | None = None
    paired_delta: float | None = None
    n_better_b: int = 0
    n_worse_b: int = 0
    n_tied: int = 0
    n_effective: int = 0
    sign_test_p: float | None = None
    # Task ids with the largest |delta|, so a reader can go look at them.
    top_movers: list[dict[str, Any]] = field(default_factory=list)
    confidence: str = ""


def sign_test_p(n_pos: int, n_neg: int) -> float | None:
    """Two-sided exact binomial sign test at p=0.5. ``None`` if no non-ties."""
    n = n_pos + n_neg
    if n == 0:
        return None
    k = min(n_pos, n_neg)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2.0**n)
    return min(1.0, 2.0 * tail)


def task_rewards(
    records: Iterable[TrialRecord],
) -> tuple[dict[str, list[float]], set[str], int]:
    """Collect ``{task_id: [reward, ...]}`` plus the stream set and trial count.

    Reward comes from ``verifier/reward.json``, which is the authoritative
    per-sub-task score; a trial with no reward file contributes nothing rather
    than contributing zeros (a missing verifier is not a failed task).
    """
    by_task: dict[str, list[float]] = {}
    streams: set[str] = set()
    n_trials = 0
    for record in records:
        if not record.reward:
            continue
        n_trials += 1
        streams.add(stream_id(record))
        for task_id, value in record.reward.items():
            by_task.setdefault(task_id, []).append(value)
    return by_task, streams, n_trials


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compare(
    records_a: Sequence[TrialRecord],
    records_b: Sequence[TrialRecord],
    *,
    label_a: str,
    label_b: str,
    min_shared: int = 5,
) -> PairedComparison:
    """Paired reward comparison of cohort B against cohort A.

    Positive ``paired_delta`` means B scored higher. Both the unpaired and the
    paired numbers are returned: their divergence is itself the finding when
    the two cohorts cover different task mixes.
    """
    a_tasks, a_streams, a_n = task_rewards(records_a)
    b_tasks, b_streams, b_n = task_rewards(records_b)

    out = PairedComparison(
        label_a=label_a,
        label_b=label_b,
        n_trials_a=a_n,
        n_trials_b=b_n,
        n_streams_a=len(a_streams),
        n_streams_b=len(b_streams),
        n_task_ids_a=len(a_tasks),
        n_task_ids_b=len(b_tasks),
    )

    flat_a = [v for values in a_tasks.values() for v in values]
    flat_b = [v for values in b_tasks.values() for v in values]
    out.unpaired_mean_a = _mean(flat_a)
    out.unpaired_mean_b = _mean(flat_b)
    if out.unpaired_mean_a is not None and out.unpaired_mean_b is not None:
        out.unpaired_delta = out.unpaired_mean_b - out.unpaired_mean_a

    shared = sorted(set(a_tasks) & set(b_tasks))
    out.n_shared_task_ids = len(shared)
    if not shared:
        out.confidence = "no shared task ids - cohorts are not comparable"
        return out

    # Average within a task id first: otherwise a task with 3 attempts on one
    # side and 1 on the other silently reweights the mean toward that task.
    per_task: list[tuple[str, float, float]] = [
        (task_id, sum(a_tasks[task_id]) / len(a_tasks[task_id]), sum(b_tasks[task_id]) / len(b_tasks[task_id]))
        for task_id in shared
    ]
    out.paired_mean_a = _mean([a for _, a, _ in per_task])
    out.paired_mean_b = _mean([b for _, _, b in per_task])
    if out.paired_mean_a is not None and out.paired_mean_b is not None:
        out.paired_delta = out.paired_mean_b - out.paired_mean_a

    for _task_id, a, b in per_task:
        if b > a:
            out.n_better_b += 1
        elif b < a:
            out.n_worse_b += 1
        else:
            out.n_tied += 1
    out.n_effective = out.n_better_b + out.n_worse_b
    out.sign_test_p = sign_test_p(out.n_better_b, out.n_worse_b)

    movers = sorted(per_task, key=lambda r: -abs(r[2] - r[1]))[:8]
    out.top_movers = [
        {"task_id": tid, "mean_a": a, "mean_b": b, "delta": b - a}
        for tid, a, b in movers
        if b != a
    ]

    if out.n_shared_task_ids < min_shared:
        out.confidence = (
            f"{out.n_shared_task_ids} shared task ids - too few to conclude"
        )
    elif out.n_effective < min_shared:
        out.confidence = (
            f"{out.n_shared_task_ids} shared ids but only {out.n_effective} moved -"
            " direction is not established"
        )
    elif min(out.n_streams_a, out.n_streams_b) < 2:
        out.confidence = (
            f"{out.n_shared_task_ids} shared ids over a SINGLE stream on one side -"
            " describes that stream, not the setting"
        )
    else:
        out.confidence = (
            f"{out.n_shared_task_ids} shared task ids over"
            f" {min(out.n_streams_a, out.n_streams_b)}+ streams"
        )
    return out


def bootstrap_delta_ci(
    records_a: Sequence[TrialRecord],
    records_b: Sequence[TrialRecord],
    *,
    n_resamples: int = 4000,
    seed: int = 20260921,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Cluster bootstrap CI for the paired reward delta, resampling *streams*.

    The sign test answers "is the direction reliable"; it says nothing about
    magnitude, and with 19 non-tied task ids it is easy to read a p of 0.36 as
    "no effect" when the data are in fact just uninformative. A confidence
    interval says which effect sizes are still on the table, which is the
    question actually being asked of the benchmark.

    **The resampling unit is the stream, not the task id.** Task ids within a
    stream share an agent, a sandbox and an arrival schedule, so they are not
    independent; bootstrapping task ids would treat 34 correlated observations
    as 34 independent ones and produce an interval several times too narrow.
    Resampling whole streams (with both sides' trials for that stream kept
    together, preserving the pairing) respects the clustering and is the
    honest unit of replication — there are only ~10 of them, and the width of
    the resulting interval is supposed to show that.

    Returns the point estimate, the percentile interval, and the share of
    resamples landing on each side of zero.
    """
    import random

    by_stream_a: dict[str, list[TrialRecord]] = {}
    by_stream_b: dict[str, list[TrialRecord]] = {}
    for record in records_a:
        by_stream_a.setdefault(stream_id(record), []).append(record)
    for record in records_b:
        by_stream_b.setdefault(stream_id(record), []).append(record)
    streams = sorted(set(by_stream_a) & set(by_stream_b))
    if len(streams) < MIN_STREAMS_FOR_CI:
        return {
            "n_streams": len(streams),
            "point": None,
            "ci_low": None,
            "ci_high": None,
            "note": (
                f"only {len(streams)} shared streams (need {MIN_STREAMS_FOR_CI}) - "
                "a bootstrap over this few clusters cannot produce an honest interval"
            ),
        }

    rng = random.Random(seed)
    point = compare(records_a, records_b, label_a="a", label_b="b").paired_delta
    deltas: list[float] = []
    for _ in range(n_resamples):
        picked = [streams[rng.randrange(len(streams))] for _ in streams]
        side_a = [r for s in picked for r in by_stream_a[s]]
        side_b = [r for s in picked for r in by_stream_b[s]]
        delta = compare(side_a, side_b, label_a="a", label_b="b").paired_delta
        if delta is not None:
            deltas.append(delta)
    if not deltas:
        return {
            "n_streams": len(streams),
            "point": point,
            "ci_low": None,
            "ci_high": None,
            "note": "no resample produced a comparable pair",
        }
    deltas.sort()
    lo = deltas[max(0, int(alpha / 2 * len(deltas)) - 1)]
    hi = deltas[min(len(deltas) - 1, int((1 - alpha / 2) * len(deltas)))]
    return {
        "n_streams": len(streams),
        "n_resamples": len(deltas),
        "point": point,
        "ci_low": lo,
        "ci_high": hi,
        "frac_positive": sum(1 for d in deltas if d > 0) / len(deltas),
        "crosses_zero": lo <= 0.0 <= hi,
        "note": "percentile CI, resampling whole streams (the unit of replication)",
    }


def repeat_spread(
    records: Sequence[TrialRecord],
    getter: Callable[[TrialRecord], float | None],
    *,
    min_attempts: int = 2,
) -> dict[str, Any]:
    """Run-to-run spread across repeated attempts at the *same* stream.

    This is the noise floor, and without it no A/B delta can be interpreted.
    Repeat attempts differ only in random seed and gateway weather — the task
    set, the arrival schedule and the sandbox shape are identical — so the
    spread between them is pure measurement noise for that cell.

    Reported as the mean within-stream range (max - min) and the mean absolute
    deviation from the stream's own mean. A between-condition delta smaller
    than the within-stream range is not detectable at this n, whatever the
    sign test says about its direction.
    """
    by_stream: dict[str, list[float]] = {}
    for record in records:
        value = getter(record)
        if value is None:
            continue
        by_stream.setdefault(stream_id(record), []).append(value)
    groups = [v for v in by_stream.values() if len(v) >= min_attempts]
    if not groups:
        return {"n_streams": 0, "mean_range": None, "mean_abs_dev": None, "max_range": None}
    ranges = [max(v) - min(v) for v in groups]
    devs = [sum(abs(x - sum(v) / len(v)) for x in v) / len(v) for v in groups]
    return {
        "n_streams": len(groups),
        "n_trials": sum(len(v) for v in groups),
        "mean_range": sum(ranges) / len(ranges),
        "max_range": max(ranges),
        "mean_abs_dev": sum(devs) / len(devs),
    }


def compare_metric(
    records_a: Sequence[TrialRecord],
    records_b: Sequence[TrialRecord],
    getter: Callable[[TrialRecord], float | None],
) -> dict[str, Any]:
    """Unpaired mean of a per-*trial* scalar on each side.

    Trial-level quantities (makespan, concurrency, wait) cannot be paired by
    task id because they describe the whole stream. They *can* be paired by
    stream id, which is what ``by_stream`` does: only streams present on both
    sides contribute, so a half-finished job does not get to compare its easy
    streams against the other side's full set.
    """
    def collect(records: Sequence[TrialRecord]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for record in records:
            value = getter(record)
            if value is None:
                continue
            out.setdefault(stream_id(record), []).append(value)
        return out

    a, b = collect(records_a), collect(records_b)
    shared = sorted(set(a) & set(b))
    flat_a = [v for vs in a.values() for v in vs]
    flat_b = [v for vs in b.values() for v in vs]
    paired_a = [sum(a[s]) / len(a[s]) for s in shared]
    paired_b = [sum(b[s]) / len(b[s]) for s in shared]
    n_pos = sum(1 for x, y in zip(paired_a, paired_b) if y > x)
    n_neg = sum(1 for x, y in zip(paired_a, paired_b) if y < x)
    return {
        "n_a": len(flat_a),
        "n_b": len(flat_b),
        "unpaired_a": _mean(flat_a),
        "unpaired_b": _mean(flat_b),
        "n_shared_streams": len(shared),
        "by_stream_a": _mean(paired_a),
        "by_stream_b": _mean(paired_b),
        "by_stream_delta": (
            _mean(paired_b) - _mean(paired_a)
            if paired_a and paired_b
            else None
        ),
        "n_streams_up": n_pos,
        "n_streams_down": n_neg,
        "sign_test_p": sign_test_p(n_pos, n_neg),
    }
