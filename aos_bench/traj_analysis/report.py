"""Aggregate per-trial records into arm/tier tables and render the report.

Two rules shape everything here.

**Clean-vs-throttled is a column, not a footnote.** Every aggregate is computed
over clean trials *and* over throttled ones, with both n values printed. The
headline is the clean number. This is enforced structurally by
:func:`_aggregate` taking a health-filtered cohort rather than by remembering to
mention it in prose.

**Small n must be visible.** ``heavy-plus`` is one stream with three attempts;
an arm/tier cell can easily be n=1. Every row therefore carries
``n_trials`` and ``n_streams`` (distinct stream prefixes), and
:func:`_confidence_note` downgrades any cell with fewer than
:data:`MIN_N_FOR_CLAIM` clean trials or fewer than two distinct streams. A mean
over three attempts at one stream measures that stream, not the tier.

Outputs: ``summary.json`` (everything, machine-readable), ``trials.csv`` (one
row per trial, for ad-hoc pivoting), ``findings.csv`` (shortcut evidence, one
row per hit) and ``report.md``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import asdict
import json
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Sequence

from aos_bench.traj_analysis import (
    behavior,
    failures,
    health,
    loader,
    metrics,
    paired,
    performance,
    scarcity,
    scheduling,
)
from aos_bench.traj_analysis.loader import TrialRecord, stream_id

# Below this many clean trials in a cell, report the number but refuse to draw a
# conclusion from it. Three repeat attempts at a single stream is the modal
# heavy-plus cell and is not a sample of the tier.
MIN_N_FOR_CLAIM = 5
TIER_ORDER = ("light", "medium", "heavy", "heavy-plus")
ARM_ORDER = ("codex", "glm", "kimi", "cc")
SETTING_ORDER = ("stream", "burst")


class TrialBundle:
    """One trial with every analysis attached."""

    __slots__ = (
        "record",
        "health",
        "metrics",
        "performance",
        "behavior",
        "scheduling",
        "failure",
        "scarcity",
    )

    def __init__(self, record: TrialRecord) -> None:
        self.record = record
        self.health = health.classify(record)
        self.metrics = metrics.compute(record)
        self.performance = performance.compute(record)
        self.behavior = behavior.compute(record)
        self.scheduling = scheduling.compute(record)
        self.failure = failures.classify_failure(record, self.health, self.performance)
        self.scarcity = scarcity.compute(
            record, concurrency_mean=self.scheduling.concurrency.time_weighted_mean
        )

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Cell identity. Includes setting and variant.

        Round 1 keyed on ``(arm, tier)`` only, which would now silently merge
        ``main-glm-stream-light`` with ``main-glm-burst-light`` -- the exact
        comparison this round exists to make -- into one average.
        """
        r = self.record
        return r.arm, r.setting, r.tier, r.variant

    @property
    def cell_label(self) -> str:
        return self.record.cell


def analyse(jobs_dir: Path) -> list[TrialBundle]:
    return [TrialBundle(record) for record in loader.discover_trials(jobs_dir)]


def analyse_flat(flat_dir: Path) -> list[TrialBundle]:
    """Analyse a flattened preservation dir (see ``loader.discover_flat_trials``)."""
    return [TrialBundle(record) for record in loader.discover_flat_trials(flat_dir)]


# ---------------------------------------------------------------------------
# aggregation helpers
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def _med(values: Sequence[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return float(median(clean)) if clean else None


def _pick(bundles: Iterable[TrialBundle], getter: Callable[[TrialBundle], Any]) -> list[Any]:
    return [v for v in (getter(b) for b in bundles) if v is not None]


def _confidence_note(n_trials: int, n_streams: int) -> str:
    if n_trials == 0:
        return "no data"
    if n_streams < 2:
        return f"n={n_trials} over a SINGLE stream - describes that stream, not the tier"
    if n_trials < MIN_N_FOR_CLAIM:
        return f"n={n_trials} - too small to conclude"
    return f"n={n_trials} over {n_streams} streams"


def _aggregate(bundles: list[TrialBundle], label: str) -> dict[str, Any]:
    """Aggregate a cohort. ``label`` is the cohort name (clean / throttled / ...)."""
    streams = {stream_id(b.record) for b in bundles}
    rewards = _pick(bundles, lambda b: b.performance.mean_reward)
    out: dict[str, Any] = {
        "cohort": label,
        "n_trials": len(bundles),
        "n_streams": len(streams),
        "confidence": _confidence_note(len(bundles), len(streams)),
        "mean_reward": _mean(rewards),
        "median_reward": _med(rewards),
        "n_tasks_total": sum(_pick(bundles, lambda b: b.performance.n_tasks)),
        "n_passed_total": sum(_pick(bundles, lambda b: b.performance.n_passed)),
        "trial_full_solve_rate": (
            _mean([1.0 if b.performance.trial_fully_solved else 0.0 for b in bundles if b.performance.trial_fully_solved is not None])
        ),
        "mean_steps": _mean(_pick(bundles, lambda b: b.metrics.n_steps)),
        "median_steps": _med(_pick(bundles, lambda b: b.metrics.n_steps)),
        "mean_turns": _mean(_pick(bundles, lambda b: b.metrics.n_turns)),
        "mean_tool_calls": _mean(_pick(bundles, lambda b: b.metrics.n_tool_calls)),
        "mean_fanout": _mean(_pick(bundles, lambda b: b.metrics.n_fanout)),
        "mean_prompt_tokens": _mean(_pick(bundles, lambda b: b.metrics.prompt_tokens)),
        "mean_completion_tokens": _mean(_pick(bundles, lambda b: b.metrics.completion_tokens)),
        "mean_cached_tokens": _mean(_pick(bundles, lambda b: b.metrics.cached_tokens)),
        "mean_reasoning_tokens": _mean(_pick(bundles, lambda b: b.metrics.reasoning_tokens)),
        "n_with_reasoning_tokens": len(_pick(bundles, lambda b: b.metrics.reasoning_tokens)),
        "mean_total_tokens": _mean(_pick(bundles, lambda b: b.metrics.total_tokens)),
        "mean_cache_hit_ratio": _mean(_pick(bundles, lambda b: b.metrics.cache_hit_ratio)),
        "mean_step_delta_sec": _mean(_pick(bundles, lambda b: b.metrics.step_delta_mean_sec)),
        "median_step_delta_sec": _med(_pick(bundles, lambda b: b.metrics.step_delta_median_sec)),
        "mean_frac_steps_under_10s": _mean(_pick(bundles, lambda b: b.metrics.frac_steps_under_10s)),
        "mean_cpu_util": _mean(_pick(bundles, lambda b: b.metrics.cpu_util_mean)),
        "peak_cpu_util": max(_pick(bundles, lambda b: b.metrics.cpu_util_peak), default=None),
        "mean_mem_pct": _mean(_pick(bundles, lambda b: b.metrics.mem_mean_pct_limit)),
        "peak_mem_pct": max(_pick(bundles, lambda b: b.metrics.mem_peak_pct_limit), default=None),
        "mean_wall_sec": _mean(_pick(bundles, lambda b: b.performance.total_wall_sec)),
        "mean_makespan_sec": _mean(_pick(bundles, lambda b: b.performance.makespan_sec)),
        "mean_throughput_computed": _mean(_pick(bundles, lambda b: b.performance.throughput_computed)),
        "mean_throughput_reported": _mean(_pick(bundles, lambda b: b.performance.throughput_reported)),
        "mean_tool_frac_upper": _mean(_pick(bundles, lambda b: b.performance.tool_attributable_frac_upper)),
        "mean_unclaimed": _mean(_pick(bundles, lambda b: b.performance.n_unclaimed)),
        # --- round 2: concurrency as a shape, not a scalar -----------------
        "conc_time_weighted_mean": _mean(_pick(bundles, lambda b: b.scheduling.concurrency.time_weighted_mean)),
        "conc_reported_mean": _mean(_pick(bundles, lambda b: b.scheduling.concurrency.reported_mean)),
        "conc_peak_max": max(_pick(bundles, lambda b: b.scheduling.concurrency.peak), default=None),
        "conc_q1": _mean(_pick(bundles, lambda b: b.scheduling.concurrency.conc_q1)),
        "conc_q2": _mean(_pick(bundles, lambda b: b.scheduling.concurrency.conc_q2)),
        "conc_q3": _mean(_pick(bundles, lambda b: b.scheduling.concurrency.conc_q3)),
        "conc_ramp_ratio": _med(_pick(bundles, lambda b: b.scheduling.concurrency.ramp_ratio)),
        "conc_collapse_frac": _mean(_pick(bundles, lambda b: b.scheduling.concurrency.collapse_frac)),
        "conc_starved_frac": _mean(_pick(bundles, lambda b: b.scheduling.concurrency.starved_frac)),
        "conc_frac_time_serial": _mean(_pick(bundles, lambda b: b.scheduling.concurrency.frac_time_serial)),
        "conc_frac_time_idle": _mean(_pick(bundles, lambda b: b.scheduling.concurrency.frac_time_idle)),
        "conc_shapes": dict(Counter(b.scheduling.concurrency.shape for b in bundles)),
        "conc_sources": dict(Counter(b.scheduling.concurrency.source for b in bundles)),
        # --- round 2: queueing ---------------------------------------------
        "queue_mean_wait_sec": _mean(_pick(bundles, lambda b: b.scheduling.queue.mean_wait_sec)),
        "queue_p90_wait_sec": _mean(_pick(bundles, lambda b: b.scheduling.queue.p90_wait_sec)),
        "queue_max_wait_sec": max(_pick(bundles, lambda b: b.scheduling.queue.max_wait_sec), default=None),
        "queue_mean_service_sec": _mean(_pick(bundles, lambda b: b.scheduling.queue.mean_service_sec)),
        "queue_wait_share": _mean(_pick(bundles, lambda b: b.scheduling.queue.wait_share_of_turnaround)),
        "queue_wait_arrival_r": _mean(_pick(bundles, lambda b: b.scheduling.queue.wait_arrival_slope_r)),
        "n_with_wait_arrival_r": len(_pick(bundles, lambda b: b.scheduling.queue.wait_arrival_slope_r)),
        "queue_starving_trials": sum(
            1 for b in bundles if b.scheduling.queue.starves_late_arrivals
        ),
        "queue_hol_blocked_frac": _mean(_pick(bundles, lambda b: b.scheduling.queue.hol_blocked_frac)),
        "queue_mean_never_claimed": _mean(_pick(bundles, lambda b: b.scheduling.queue.n_never_claimed)),
        # --- round 2: ordering / triage -------------------------------------
        "order_fifo_r": _mean(_pick(bundles, lambda b: b.scheduling.ordering.fifo_r)),
        "order_triage_r": _mean(_pick(bundles, lambda b: b.scheduling.ordering.triage_r)),
        "order_reward_r": _mean(_pick(bundles, lambda b: b.scheduling.ordering.reward_order_r)),
        "order_frac_time_backlog_ge2": _mean(
            _pick(bundles, lambda b: b.scheduling.ordering.frac_time_backlog_ge2)
        ),
        "order_verdicts": dict(Counter(b.scheduling.ordering.verdict for b in bundles)),
        # --- round 2: fetch behaviour (the mechanism behind abandoned work) --
        "fetch_todo_next": _mean(_pick(bundles, lambda b: b.scheduling.fetch.n_todo_next)),
        "fetch_polls_per_claim": _mean(_pick(bundles, lambda b: b.scheduling.fetch.polls_per_claim)),
        "fetch_claimed_share": _mean(_pick(bundles, lambda b: b.scheduling.fetch.claimed_share)),
        "fetch_n_stopped_asking": sum(1 for b in bundles if b.scheduling.fetch.stopped_asking),
        "fetch_reward_on_claimed": _mean(_pick(bundles, lambda b: b.scheduling.fetch.reward_on_claimed)),
        "fetch_reward_all_tasks": _mean(_pick(bundles, lambda b: b.scheduling.fetch.reward_all_tasks)),
        # --- round 2: sandbox shape actually measured ------------------------
        "sandbox_cores": sorted({b.metrics.cpu_cores for b in bundles}),
        "sandbox_mem_gib": sorted({round(b.metrics.mem_limit_bytes / 1024**3, 2) for b in bundles}),
        "frac_cpu_windows_near_saturation": _mean(
            _pick(bundles, lambda b: b.metrics.frac_cpu_windows_near_saturation)
        ),
        # --- round 3: does any declared limit actually bind? ----------------
        "scarcity_time_used_share": _mean(_pick(bundles, lambda b: b.scarcity.time_used_share)),
        "scarcity_cpu_idle_share": _mean(_pick(bundles, lambda b: b.scarcity.cpu_idle_share)),
        "scarcity_cpu_p95_util": _mean(_pick(bundles, lambda b: b.scarcity.cpu_p95_util)),
        "scarcity_mem_peak_share": _mean(_pick(bundles, lambda b: b.scarcity.mem_peak_share)),
        "scarcity_n_no_binding": sum(
            1 for b in bundles if b.scarcity.verdict == "no binding constraint detected"
        ),
        "scarcity_binding_counts": dict(
            Counter(limit for b in bundles for limit in b.scarcity.binding_limits)
        ),
        "resource_classes": dict(Counter(b.behavior.resource_class for b in bundles)),
        "strategies": dict(Counter(s for b in bundles for s in b.behavior.strategies)),
        "stream_done_reasons": dict(
            Counter(str(b.performance.stream_done_reason) for b in bundles)
        ),
    }
    phases: dict[str, float | None] = {}
    for phase in performance.PHASES:
        phases[phase] = _mean(_pick(bundles, lambda b, p=phase: b.performance.phase_sec.get(p)))
    out["mean_phase_sec"] = phases
    out["mean_phase_residual_sec"] = _mean(_pick(bundles, lambda b: b.performance.phase_residual_sec))
    return out


def _throughput_disagreements(bundles: list[TrialBundle]) -> list[dict[str, Any]]:
    """Trials where our recomputed throughput differs from the pool's own.

    Reported rather than reconciled: the pool computes its aggregate at stream
    teardown and may use a different denominator than the number of tasks that
    reached a terminal state, which is what we count.
    """
    rows: list[dict[str, Any]] = []
    for b in bundles:
        p = b.performance
        computed, reported = p.throughput_computed, p.throughput_reported
        if computed is None or reported in (None, 0):
            continue
        rel = abs(computed - reported) / reported
        if rel > performance.THROUGHPUT_TOLERANCE:
            rows.append(
                {
                    "trial": b.record.trial_name,
                    "job": b.record.job,
                    "computed": computed,
                    "reported": reported,
                    "rel_diff": rel,
                    "n_tasks": p.n_tasks,
                    "n_unclaimed": p.n_unclaimed,
                    "n_terminal_tasks": p.n_terminal_tasks,
                    "implied_numerator": p.throughput_numerator_reported,
                    "n_reported_completed": p.n_reported_completed,
                }
            )
    return sorted(rows, key=lambda r: -r["rel_diff"])


def _throughput_numerator_explained(bundles: list[TrialBundle]) -> dict[str, Any]:
    """Check whether the pool's throughput numerator is ``n_reported_completed``.

    Inverting ``reported x makespan / 3600`` recovers the numerator the pool
    used. If it matches ``n_reported_completed`` everywhere, the whole
    "disagreement" is one definition difference (agent-reported completions vs
    pool-observed terminal tasks) rather than a bug — which is a far more useful
    thing to report than a list of 93 mismatched trials.
    """
    matches = mismatches = 0
    examples: list[dict[str, Any]] = []
    for b in bundles:
        p = b.performance
        if p.throughput_numerator_reported is None or p.n_reported_completed is None:
            continue
        if abs(p.throughput_numerator_reported - p.n_reported_completed) < 1e-6:
            matches += 1
        else:
            mismatches += 1
            if len(examples) < 10:
                examples.append(
                    {
                        "trial": f"{b.record.job}/{b.record.trial_name}",
                        "implied_numerator": p.throughput_numerator_reported,
                        "n_reported_completed": p.n_reported_completed,
                    }
                )
    return {
        "hypothesis": "reported throughput numerator == aggregates.n_reported_completed",
        "n_matching": matches,
        "n_mismatching": mismatches,
        "conclusion": (
            "confirmed: the pool counts agent-reported completions, we count "
            "pool-observed terminal tasks; the gap is definitional, not a bug"
            if mismatches == 0 and matches
            else "NOT confirmed - the pool's numerator is something else; investigate"
        ),
        "mismatch_examples": examples,
    }


def build_summary(bundles: list[TrialBundle]) -> dict[str, Any]:
    by_cell: dict[tuple[str, str, str, str], list[TrialBundle]] = defaultdict(list)
    for b in bundles:
        by_cell[b.key].append(b)

    cells: list[dict[str, Any]] = []
    for (arm, setting, tier, variant), group in sorted(
        by_cell.items(),
        key=lambda kv: (
            ARM_ORDER.index(kv[0][0]) if kv[0][0] in ARM_ORDER else 99,
            SETTING_ORDER.index(kv[0][1]) if kv[0][1] in SETTING_ORDER else 99,
            TIER_ORDER.index(kv[0][2]) if kv[0][2] in TIER_ORDER else 99,
            kv[0][3],
        ),
    ):
        status = Counter(b.health.status for b in group)
        clean = [b for b in group if b.health.status == "clean"]
        throttled = [b for b in group if b.health.status == "throttled"]
        account = [b for b in group if b.health.status == "account_error"]
        other = [b for b in group if b.health.status in ("stunted", "unparseable")]
        cells.append(
            {
                "arm": arm,
                "setting": setting,
                "tier": tier,
                "variant": variant,
                "cell": group[0].cell_label,
                "n_total": len(group),
                "status_counts": dict(status),
                "clean": _aggregate(clean, "clean"),
                "throttled": _aggregate(throttled, "throttled"),
                "account_error": _aggregate(account, "account_error"),
                "stunted_or_unparseable": _aggregate(other, "stunted_or_unparseable"),
            }
        )

    finished = [b for b in bundles if b.health.status != "in_flight"]
    analysable = [b for b in bundles if b.health.is_analysable]
    return {
        "totals": {
            "n_trial_dirs": len(bundles),
            "n_finished": len(finished),
            "n_in_flight": sum(1 for b in bundles if b.health.status == "in_flight"),
            "n_clean": sum(1 for b in bundles if b.health.status == "clean"),
            "n_throttled": sum(1 for b in bundles if b.health.status == "throttled"),
            "n_account_error": sum(1 for b in bundles if b.health.status == "account_error"),
            "n_stunted": sum(1 for b in bundles if b.health.status == "stunted"),
            "n_infra_error": sum(1 for b in bundles if b.health.status == "infra_error"),
            "n_unparseable": sum(1 for b in bundles if b.health.status == "unparseable"),
            "n_with_trajectory": len(analysable),
            "stunted_ratio_of_finished": (
                sum(1 for b in finished if b.health.stunted) / len(finished) if finished else None
            ),
            "throttled_ratio_of_finished": (
                sum(1 for b in finished if b.health.throttled) / len(finished) if finished else None
            ),
        },
        "throttle_labels": dict(
            Counter(label for b in bundles for label in b.health.throttle_labels)
        ),
        "throttle_buckets": dict(
            Counter(
                {k: v for b in bundles for k, v in b.health.throttle_buckets.items()}
            )
        ),
        "auth_labels": dict(Counter(label for b in bundles for label in b.health.auth_labels)),
        "account_labels": dict(
            Counter(label for b in bundles for label in b.health.account_labels)
        ),
        "infra_error_classes": dict(
            Counter(cls for b in bundles for cls in b.health.infra_error_classes)
        ),
        "missing_artifacts": dict(
            Counter(name for b in bundles for name in b.health.missing_artifacts)
        ),
        "load_errors": {
            b.record.trial_name: b.health.load_errors for b in bundles if b.health.load_errors
        },
        "cells": cells,
        "export_gap": _export_gap(bundles),
        "tier_pairing": _tier_pairing(bundles),
        "scarcity": _scarcity_summary(bundles),
        "comparisons": _comparisons(bundles),
        "failure_taxonomy": failures.summarise([b.failure for b in bundles]),
        "concurrency_outliers": _concurrency_outliers(bundles),
        "tool_histogram_by_arm": _tool_histogram_by_arm(bundles),
        "shortcuts": _shortcut_summary(bundles),
        "throughput_disagreements": _throughput_disagreements(bundles),
        "throughput_numerator_check": _throughput_numerator_explained(bundles),
        "memory_measurement_artifacts": [
            {
                "trial": f"{b.record.job}/{b.record.trial_name}",
                "n_samples_over_limit": b.metrics.n_samples_over_mem_limit,
                "reported_peak_pct": b.metrics.mem_peak_pct_limit,
                "note": b.metrics.mem_over_limit_note,
            }
            for b in bundles
            if b.metrics.n_samples_over_mem_limit
        ],
        "cpu_throttle_data_available": any(b.metrics.cpu_throttle_data_available for b in bundles),
        "notes": {
            "step_delta": (
                "step_delta_* is the gap between consecutive ATIF step timestamps and "
                "includes tool execution time; it is NOT LLM latency."
            ),
            "tool_time": performance.TrialPerformance.tool_time_note,
            "cpu": (
                "cpu_nr_periods/cpu_nr_throttled are -1 when cpu.stat is unavailable; "
                "cpu_util is the proxy d(cpu_usage_usec)/(4 cores * 5s)."
            ),
        },
    }


# Trial-level scalars compared stream-by-stream alongside the paired reward.
# Keyed by the name used in the report; the getter takes a TrialBundle.
COMPARISON_METRICS: dict[str, Callable[[TrialBundle], float | None]] = {
    "mean_reward": lambda b: b.performance.mean_reward,
    "makespan_sec": lambda b: b.performance.makespan_sec,
    "mean_wait_sec": lambda b: b.scheduling.queue.mean_wait_sec,
    "mean_service_sec": lambda b: b.scheduling.queue.mean_service_sec,
    "conc_time_weighted_mean": lambda b: b.scheduling.concurrency.time_weighted_mean,
    "conc_peak": lambda b: (
        float(b.scheduling.concurrency.peak)
        if b.scheduling.concurrency.peak is not None
        else None
    ),
    "n_steps": lambda b: float(b.metrics.n_steps) if b.metrics.n_steps else None,
    "n_tool_calls": lambda b: float(b.metrics.n_tool_calls) if b.metrics.n_tool_calls else None,
    "cpu_util_mean": lambda b: b.metrics.cpu_util_mean,
    "frac_cpu_windows_near_saturation": lambda b: b.metrics.frac_cpu_windows_near_saturation,
    "mem_mean_pct": lambda b: b.metrics.mem_mean_pct_limit,
    "n_unclaimed": lambda b: (
        float(b.performance.n_unclaimed) if b.performance.n_unclaimed is not None else None
    ),
}

# The two A/B questions round 2 exists to answer, plus the cross-tier control
# that proved pairing is necessary in the first place. Each entry is
# ``(label, predicate_a, predicate_b)`` over clean bundles.
COMPARISON_SPECS: tuple[tuple[str, str, str, Callable[[TrialBundle], bool], Callable[[TrialBundle], bool]], ...] = (
    (
        "glm light: stream -> burst",
        "glm-stream-light",
        "glm-burst-light",
        lambda b: b.key == ("glm", "stream", "light", ""),
        lambda b: b.key == ("glm", "burst", "light", ""),
    ),
    (
        "glm medium: stream -> burst",
        "glm-stream-medium",
        "glm-burst-medium",
        lambda b: b.key == ("glm", "stream", "medium", ""),
        lambda b: b.key == ("glm", "burst", "medium", ""),
    ),
    (
        "glm heavy: stream -> burst",
        "glm-stream-heavy",
        "glm-burst-heavy",
        lambda b: b.key == ("glm", "stream", "heavy", ""),
        lambda b: b.key == ("glm", "burst", "heavy", ""),
    ),
    (
        "glm heavy: 4c8g -> 2c4g",
        "glm-stream-heavy (4c8g)",
        "glm-stream-heavy-2c4g",
        lambda b: b.key == ("glm", "stream", "heavy", ""),
        lambda b: b.key == ("glm", "stream", "heavy", "2c4g"),
    ),
)


def _comparisons(bundles: list[TrialBundle]) -> list[dict[str, Any]]:
    """Run every A/B in :data:`COMPARISON_SPECS` over the clean cohort.

    Clean-only on both sides: a burst arm that happened to be throttled less
    than its stream counterpart would otherwise show up as "burst helps", which
    is the single most likely way to get this wrong.
    """
    clean = [b for b in bundles if b.health.status == "clean"]
    out: list[dict[str, Any]] = []
    for label, label_a, label_b, pred_a, pred_b in COMPARISON_SPECS:
        side_a = [b for b in clean if pred_a(b)]
        side_b = [b for b in clean if pred_b(b)]
        if not side_a and not side_b:
            continue
        comparison = paired.compare(
            [b.record for b in side_a],
            [b.record for b in side_b],
            label_a=label_a,
            label_b=label_b,
        )
        row: dict[str, Any] = {"label": label, "reward": asdict(comparison)}
        if not side_a or not side_b:
            row["status"] = (
                f"UNAVAILABLE: {0 if not side_a else len(side_a)} clean trials on the "
                f"{label_a} side and {0 if not side_b else len(side_b)} on {label_b}"
            )
        else:
            row["status"] = "ok"
        row["metrics"] = {
            name: paired.compare_metric(
                [b.record for b in side_a], [b.record for b in side_b], _bind(getter, side_a + side_b)
            )
            for name, getter in COMPARISON_METRICS.items()
        }
        # Noise floor: how much the same stream varies between repeat attempts
        # on each side. A delta below this is not detectable at this n.
        reward_getter = _bind(COMPARISON_METRICS["mean_reward"], side_a + side_b)
        row["reward_bootstrap_ci"] = paired.bootstrap_delta_ci(
            [b.record for b in side_a], [b.record for b in side_b]
        )
        row["reward_noise_floor"] = {
            label_a: paired.repeat_spread([b.record for b in side_a], reward_getter),
            label_b: paired.repeat_spread([b.record for b in side_b], reward_getter),
        }
        out.append(row)
    return out


def _bind(
    getter: Callable[[TrialBundle], float | None], bundles: list[TrialBundle]
) -> Callable[[TrialRecord], float | None]:
    """Adapt a bundle-level getter to the record-level API of ``paired``.

    ``paired`` deliberately takes ``TrialRecord`` so it has no dependency on
    the report layer; this closure maps records back to their bundle by
    identity of the trial path, which is unique across the whole jobs tree.
    """
    index = {b.record.path: b for b in bundles}

    def inner(record: TrialRecord) -> float | None:
        bundle = index.get(record.path)
        return getter(bundle) if bundle is not None else None

    return inner


def _export_gap(bundles: list[TrialBundle]) -> dict[str, Any]:
    """Trials that finished and were *scored* but exported no agent artifacts.

    This is a blind spot, not a result, and it is load-bearing enough to get
    its own section. Such a trial has a ``reward.json`` and complete scheduling
    metrics but **no ``trajectory.json`` and no transcript at all** — which
    means the throttle detector, whose only input is the transcript, cannot
    run on it. It is therefore classified ``unparseable`` (never ``clean``),
    and its reward is excluded from every headline.

    The reason that matters: if these trials were disproportionately throttled,
    excluding them is correct; if they were not, excluding them biases the
    headline. We cannot tell which, and the comparison of their mean reward
    against the clean mean in the same job is the closest available evidence —
    reported here so the direction of the possible bias is at least visible.
    """
    affected = [
        b
        for b in bundles
        if b.health.status == "unparseable" and b.performance.mean_reward is not None
    ]
    by_job: dict[str, dict[str, Any]] = {}
    for b in affected:
        row = by_job.setdefault(
            b.record.job,
            {"job": b.record.job, "n_affected": 0, "rewards": [], "no_transcript": 0},
        )
        row["n_affected"] += 1
        row["rewards"].append(b.performance.mean_reward)
        if not b.record.transcripts and not b.record.stderr_transcripts:
            row["no_transcript"] += 1
    clean_by_job: dict[str, list[float]] = defaultdict(list)
    for b in bundles:
        if b.health.status == "clean" and b.performance.mean_reward is not None:
            clean_by_job[b.record.job].append(b.performance.mean_reward)

    rows = []
    for job, row in sorted(by_job.items()):
        clean = clean_by_job.get(job, [])
        rows.append(
            {
                "job": job,
                "n_affected": row["n_affected"],
                "n_without_any_transcript": row["no_transcript"],
                "mean_reward_affected": _mean(row["rewards"]),
                "n_clean": len(clean),
                "mean_reward_clean": _mean(clean),
                "delta_vs_clean": (
                    _mean(row["rewards"]) - _mean(clean) if clean else None
                ),
            }
        )
    return {
        "n_affected": len(affected),
        "n_without_any_transcript": sum(
            1 for b in affected if not b.record.transcripts and not b.record.stderr_transcripts
        ),
        "arms_affected": sorted({b.record.arm for b in affected}),
        "by_job": rows,
        "note": (
            "scored but no agent/trajectory.json and no transcript => the throttle "
            "detector cannot run on these trials; they are excluded from every clean "
            "figure and their true health is UNKNOWN, not clean and not throttled"
        ),
    }


def _tier_pairing(bundles: list[TrialBundle]) -> dict[str, Any]:
    """Cross-tier comparisons, unpaired vs paired by task_id, per arm.

    **Cross-tier reward is not comparable unpaired and this is the table that
    proves it.** Light and medium share a task pool, but medium's extra tasks
    are markedly easier, so an unpaired light->medium "gain" is mostly a change
    of task mix. For codex the unpaired gap was +0.34 and the paired gap
    +0.089 -- a four-fold overstatement that would have gone straight into a
    paper.

    So every adjacent tier step is reported both ways, per arm, with the number
    of shared task ids. Where the paired and unpaired numbers diverge, the
    paired one is the only defensible figure; where few ids are shared, neither
    is, and the ``n_shared`` column says so.
    """
    steps = (("light", "medium"), ("medium", "heavy"), ("heavy", "heavy-plus"))
    rows: list[dict[str, Any]] = []
    clean = [b for b in bundles if b.health.status == "clean" and b.record.setting == "stream"]
    for arm in ARM_ORDER:
        for lo, hi in steps:
            side_lo = [b.record for b in clean if b.record.arm == arm and b.record.tier == lo]
            side_hi = [b.record for b in clean if b.record.arm == arm and b.record.tier == hi]
            if not side_lo or not side_hi:
                continue
            comparison = paired.compare(
                side_lo, side_hi, label_a=f"{arm}-{lo}", label_b=f"{arm}-{hi}"
            )
            rows.append(
                {
                    "arm": arm,
                    "from_tier": lo,
                    "to_tier": hi,
                    "n_trials_lo": comparison.n_trials_a,
                    "n_trials_hi": comparison.n_trials_b,
                    "unpaired_lo": comparison.unpaired_mean_a,
                    "unpaired_hi": comparison.unpaired_mean_b,
                    "unpaired_delta": comparison.unpaired_delta,
                    "n_shared": comparison.n_shared_task_ids,
                    "paired_lo": comparison.paired_mean_a,
                    "paired_hi": comparison.paired_mean_b,
                    "paired_delta": comparison.paired_delta,
                    "overstatement": (
                        comparison.unpaired_delta - comparison.paired_delta
                        if comparison.unpaired_delta is not None
                        and comparison.paired_delta is not None
                        else None
                    ),
                }
            )
    return {
        "rows": rows,
        "note": (
            "unpaired cross-tier deltas conflate task difficulty with tier size; "
            "only the paired column is quotable, and only where n_shared is large"
        ),
    }


def _render_tier_pairing(summary: dict[str, Any]) -> list[str]:
    rows = summary["tier_pairing"]["rows"]
    if not rows:
        return []
    lines = [
        "",
        "## R3-1b. Cross-tier steps: unpaired vs paired by task_id",
        "",
        "Cross-tier reward **cannot** be compared unpaired: adjacent tiers share a task",
        "pool but the larger tier's extra tasks differ in difficulty, so an unpaired",
        "delta mostly measures the task mix. `overstatement` is how much the unpaired",
        "delta exceeds the paired one -- i.e. how wrong the naive number would be.",
        "Only the paired column is quotable, and only where `n_shared` is large.",
        "",
        "| arm | step | n lo/hi | unpaired lo -> hi | unpaired delta | n shared |"
        " paired lo -> hi | **paired delta** | overstatement |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['arm']} | {r['from_tier']}->{r['to_tier']} |"
            f" {r['n_trials_lo']}/{r['n_trials_hi']} |"
            f" {_fmt(r['unpaired_lo'])} -> {_fmt(r['unpaired_hi'])} |"
            f" {_fmt(r['unpaired_delta'])} | {r['n_shared']} |"
            f" {_fmt(r['paired_lo'])} -> {_fmt(r['paired_hi'])} |"
            f" **{_fmt(r['paired_delta'])}** | {_fmt(r['overstatement'])} |"
        )
    return lines


def _scarcity_summary(bundles: list[TrialBundle]) -> dict[str, Any]:
    """Does any declared limit bind, overall and under heavy load?

    The heavy-load slice is the load-bearing one. "CPU is idle" is unsurprising
    for a trial running one task; "CPU is idle while the agent holds 4+ tasks"
    is the claim that the sandbox was never the bottleneck.
    """
    clean = [b for b in bundles if b.health.status == "clean"]
    heavy = [
        b
        for b in clean
        if (b.scheduling.concurrency.time_weighted_mean or 0.0) >= scarcity.HEAVY_LOAD_CONCURRENCY
    ]
    overall = scarcity.summarise(
        [b.scarcity for b in clean], heavy_load_profiles=[b.scarcity for b in heavy]
    )
    by_cell: dict[str, dict[str, Any]] = {}
    for b in clean:
        row = by_cell.setdefault(b.cell_label, {"profiles": []})
        row["profiles"].append(b.scarcity)
    per_cell = {
        label: scarcity.summarise(row["profiles"]) for label, row in sorted(by_cell.items())
    }
    return {"overall": overall, "by_cell": per_cell}


def _render_scarcity(summary: dict[str, Any]) -> list[str]:
    block = summary.get("scarcity")
    if not block:
        return []
    o = block["overall"]
    lines = [
        "",
        "## R3-4. Does the setting impose real scarcity?",
        "",
        "This decides what the benchmark can claim. If none of the declared limits",
        "(wall clock, CPU, memory, queue depth) ever binds, then the setting measures",
        "task-solving skill with scheduling bookkeeping attached, and framing it as a",
        "resource-management benchmark is unsupported. Each limit is tested separately",
        "against the trial's *own* configured budget, never a hardcoded one.",
        "",
        f"- clean trials tested: **{o['n_trials']}**",
        f"- wall clock used: mean **{_fmt(o['mean_time_used_share'],3)}** of the granted"
        f" agent budget, worst case **{_fmt(o['max_time_used_share'],3)}**"
        f" (binding threshold {scarcity.TIME_BINDING_SHARE})",
        f"- CPU windows below {scarcity.CPU_IDLE_THRESHOLD:.0%} of quota: mean"
        f" **{_fmt(o['mean_cpu_idle_share'],3)}**; mean p95 utilisation"
        f" **{_fmt(o['mean_cpu_p95_util'],3)}** of quota",
        f"- **CPU idle share among heavily-loaded trials** (time-weighted concurrency >="
        f" {scarcity.HEAVY_LOAD_CONCURRENCY:.0f}, n={o['n_heavy_load']}):"
        f" **{_fmt(o['heavy_load_cpu_idle_share'],3)}**",
        f"- memory peak: mean **{_fmt(o['mean_mem_peak_share'],3)}** of the sandbox limit,"
        f" worst **{_fmt(o['max_mem_peak_share'],3)}**",
        f"- share of makespan with backlog >= 2: mean"
        f" **{_fmt(o['mean_frac_time_backlog_ge2'],3)}**",
        f"- trials with **no** binding constraint detected: **{o['n_no_binding_constraint']}"
        f"/{o['n_trials']}** ({_fmt(o['share_no_binding_constraint'],2)})",
        f"- binding-limit counts: `{o['n_binding_by_limit']}`",
        "",
        "| cell | n | time used | cpu idle | cpu p95 | mem peak | backlog>=2 |"
        " no binding constraint |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, cell in block["by_cell"].items():
        lines.append(
            f"| {label} | {cell['n_trials']} | {_fmt(cell['mean_time_used_share'],3)} |"
            f" {_fmt(cell['mean_cpu_idle_share'],2)} | {_fmt(cell['mean_cpu_p95_util'],2)} |"
            f" {_fmt(cell['mean_mem_peak_share'],2)} |"
            f" {_fmt(cell['mean_frac_time_backlog_ge2'],2)} |"
            f" {cell['n_no_binding_constraint']}/{cell['n_trials']} |"
        )
    return lines


def _concurrency_outliers(bundles: list[TrialBundle]) -> dict[str, Any]:
    """Per-trial concurrency shapes worth eyeballing, plus the pool cross-check.

    Two lists: trials whose curve *collapsed* (went parallel, then ran serial
    for most of the remaining makespan) and trials where our time-weighted
    integral disagrees with the pool's own ``mean_realized_concurrency`` by
    more than :data:`scheduling.CONCURRENCY_TOLERANCE`. The second list should
    be empty; if it is not, every concurrency number in this report is suspect
    and that must be visible rather than smoothed.
    """
    collapsed: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    for b in bundles:
        c = b.scheduling.concurrency
        if c.time_weighted_mean is None:
            continue
        if c.shape == "starve":
            collapsed.append(
                {
                    "trial": f"{b.record.job}/{b.record.trial_name}",
                    "status": b.health.status,
                    "peak": c.peak,
                    "q1": c.conc_q1,
                    "q2": c.conc_q2,
                    "q3": c.conc_q3,
                    "ramp_ratio": c.ramp_ratio,
                    "collapse_frac": c.collapse_frac,
                    "starved_frac": c.starved_frac,
                    "n_unclaimed": b.performance.n_unclaimed,
                    "mean_reward": b.performance.mean_reward,
                }
            )
        if c.reported_mean:
            rel = abs(c.time_weighted_mean - c.reported_mean) / c.reported_mean
            if rel > scheduling.CONCURRENCY_TOLERANCE:
                mismatched.append(
                    {
                        "trial": f"{b.record.job}/{b.record.trial_name}",
                        "ours": c.time_weighted_mean,
                        "pool": c.reported_mean,
                        "rel_diff": rel,
                        "source": c.source,
                    }
                )
    return {
        "n_starved": len(collapsed),
        "starved": sorted(collapsed, key=lambda r: r["starved_frac"] or 0, reverse=True)[:25],
        "n_mean_mismatch": len(mismatched),
        "mean_mismatch": sorted(mismatched, key=lambda r: -r["rel_diff"])[:15],
        "cross_check": (
            "our time-weighted integral of curves.concurrency reproduces the pool's "
            "mean_realized_concurrency"
            if not mismatched
            else "DISAGREEMENT: our integral differs from the pool aggregate - "
            "treat every concurrency figure here as unverified"
        ),
    }


def _tool_histogram_by_arm(bundles: list[TrialBundle]) -> dict[str, dict[str, int]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for b in bundles:
        for tool, count in b.metrics.tool_histogram.items():
            out[b.record.arm][tool] += count
    return {arm: dict(counter.most_common()) for arm, counter in out.items()}


def _shortcut_summary(bundles: list[TrialBundle]) -> dict[str, Any]:
    by_category: Counter[str] = Counter()
    trials_by_category: dict[str, set[str]] = defaultdict(set)
    by_access: Counter[str] = Counter()
    hosts: Counter[str] = Counter()
    for b in bundles:
        for f in b.behavior.findings:
            if f.category.startswith("network:"):
                hosts[f.category.split(":", 1)[1]] += 1
                continue
            by_category[f.category] += 1
            by_access[f"{f.category}/{f.access}"] += 1
            trials_by_category[f.category].add(f"{b.record.job}/{b.record.trial_name}")
    return {
        "schedule_leak_trials": sorted(
            f"{b.record.job}/{b.record.trial_name}" for b in bundles if b.behavior.schedule_leak
        ),
        "by_category_calls": dict(by_category.most_common()),
        "by_category_trials": {k: len(v) for k, v in sorted(trials_by_category.items())},
        "by_category_access": dict(by_access.most_common()),
        "grader_write_trials": sorted(
            f"{b.record.job}/{b.record.trial_name}"
            for b in bundles
            if b.behavior.grader_write_calls
        ),
        "grader_use_trials": sorted(
            f"{b.record.job}/{b.record.trial_name}"
            for b in bundles
            if b.behavior.grader_use_calls
        ),
        "unrecognised_hosts": dict(hosts.most_common(50)),
    }


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------

TRIAL_CSV_FIELDS = (
    "job source_cohort arm setting tier variant trial harness model status throttled throttle_labels stunted "
    "infra_error_classes n_steps n_turns n_tool_calls n_pool_calls n_todo_next n_fanout "
    "prompt_tokens completion_tokens cached_tokens reasoning_tokens total_tokens cache_hit_ratio "
    "step_delta_mean_sec step_delta_median_sec frac_steps_under_10s "
    "cpu_util_mean cpu_util_peak mem_mean_pct mem_peak_pct pids_peak "
    "n_tasks n_passed mean_reward fully_solved n_unclaimed stream_done_reason "
    "total_wall_sec env_setup_sec agent_setup_sec agent_exec_sec verifier_sec phase_residual_sec "
    "makespan_sec throughput_computed throughput_reported tool_frac_upper "
    "resource_class probed_resources used_parallelism strategies "
    "grader_look grader_use grader_write schedule_leak "
    "sandbox_cores sandbox_mem_gib frac_cpu_near_sat "
    "conc_source conc_twmean conc_peak conc_q1 conc_q2 conc_q3 conc_ramp_ratio "
    "conc_collapse_frac conc_starved_frac conc_frac_serial conc_shape "
    "queue_mean_wait queue_p90_wait queue_max_wait queue_mean_service "
    "queue_wait_share queue_wait_arrival_r queue_hol_blocked_frac queue_never_claimed "
    "order_fifo_r order_triage_r order_reward_r order_backlog_ge2 order_verdict "
    "fetch_todo_next fetch_polls_per_claim fetch_claimed_share fetch_stopped_asking "
    "fetch_reward_on_claimed fetch_reward_all_tasks "
    "failure_bucket failure_measurement failure_cutoff_vs_choice "
    "scarcity_time_share scarcity_cpu_idle scarcity_cpu_p95 scarcity_mem_share "
    "scarcity_binding scarcity_verdict"
).split()


def _trial_row(b: TrialBundle) -> dict[str, Any]:
    m, p, h, bh = b.metrics, b.performance, b.health, b.behavior
    c, q, o, fv = b.scheduling.concurrency, b.scheduling.queue, b.scheduling.ordering, b.failure
    return {
        "job": b.record.job,
        "source_cohort": b.record.source_cohort,
        "arm": b.record.arm,
        "setting": b.record.setting,
        "tier": b.record.tier,
        "variant": b.record.variant,
        "trial": b.record.trial_name,
        "harness": m.harness,
        "model": m.model_name or "",
        "status": h.status,
        "throttled": int(h.throttled),
        "throttle_labels": "|".join(h.throttle_labels),
        "stunted": int(h.stunted),
        "infra_error_classes": "|".join(h.infra_error_classes),
        "n_steps": m.n_steps,
        "n_turns": m.n_turns,
        "n_tool_calls": m.n_tool_calls,
        "n_pool_calls": m.n_pool_calls,
        "n_todo_next": m.n_todo_next,
        "n_fanout": m.n_fanout,
        "prompt_tokens": m.prompt_tokens,
        "completion_tokens": m.completion_tokens,
        "cached_tokens": m.cached_tokens,
        "reasoning_tokens": m.reasoning_tokens,
        "total_tokens": m.total_tokens,
        "cache_hit_ratio": m.cache_hit_ratio,
        "step_delta_mean_sec": m.step_delta_mean_sec,
        "step_delta_median_sec": m.step_delta_median_sec,
        "frac_steps_under_10s": m.frac_steps_under_10s,
        "cpu_util_mean": m.cpu_util_mean,
        "cpu_util_peak": m.cpu_util_peak,
        "mem_mean_pct": m.mem_mean_pct_limit,
        "mem_peak_pct": m.mem_peak_pct_limit,
        "pids_peak": m.pids_peak,
        "n_tasks": p.n_tasks,
        "n_passed": p.n_passed,
        "mean_reward": p.mean_reward,
        "fully_solved": "" if p.trial_fully_solved is None else int(p.trial_fully_solved),
        "n_unclaimed": p.n_unclaimed,
        "stream_done_reason": p.stream_done_reason or "",
        "total_wall_sec": p.total_wall_sec,
        "env_setup_sec": p.phase_sec.get("environment_setup"),
        "agent_setup_sec": p.phase_sec.get("agent_setup"),
        "agent_exec_sec": p.phase_sec.get("agent_execution"),
        "verifier_sec": p.phase_sec.get("verifier"),
        "phase_residual_sec": p.phase_residual_sec,
        "makespan_sec": p.makespan_sec,
        "throughput_computed": p.throughput_computed,
        "throughput_reported": p.throughput_reported,
        "tool_frac_upper": p.tool_attributable_frac_upper,
        "resource_class": bh.resource_class,
        "probed_resources": int(bh.probed_resources),
        "used_parallelism": int(bh.used_parallelism),
        "strategies": "|".join(bh.strategies),
        "grader_look": bh.grader_look_calls,
        "grader_use": bh.grader_use_calls,
        "grader_write": bh.grader_write_calls,
        "schedule_leak": int(bh.schedule_leak),
        "sandbox_cores": m.cpu_cores,
        "sandbox_mem_gib": round(m.mem_limit_bytes / 1024**3, 2),
        "frac_cpu_near_sat": m.frac_cpu_windows_near_saturation,
        "conc_source": c.source,
        "conc_twmean": c.time_weighted_mean,
        "conc_peak": c.peak,
        "conc_q1": c.conc_q1,
        "conc_q2": c.conc_q2,
        "conc_q3": c.conc_q3,
        "conc_ramp_ratio": c.ramp_ratio,
        "conc_collapse_frac": c.collapse_frac,
        "conc_starved_frac": c.starved_frac,
        "conc_frac_serial": c.frac_time_serial,
        "conc_shape": c.shape,
        "queue_mean_wait": q.mean_wait_sec,
        "queue_p90_wait": q.p90_wait_sec,
        "queue_max_wait": q.max_wait_sec,
        "queue_mean_service": q.mean_service_sec,
        "queue_wait_share": q.wait_share_of_turnaround,
        "queue_wait_arrival_r": q.wait_arrival_slope_r,
        "queue_hol_blocked_frac": q.hol_blocked_frac,
        "queue_never_claimed": q.n_never_claimed,
        "order_fifo_r": o.fifo_r,
        "order_triage_r": o.triage_r,
        "order_reward_r": o.reward_order_r,
        "order_backlog_ge2": o.frac_time_backlog_ge2,
        "order_verdict": o.verdict,
        "fetch_todo_next": b.scheduling.fetch.n_todo_next,
        "fetch_polls_per_claim": b.scheduling.fetch.polls_per_claim,
        "fetch_claimed_share": b.scheduling.fetch.claimed_share,
        "fetch_stopped_asking": int(b.scheduling.fetch.stopped_asking),
        "fetch_reward_on_claimed": b.scheduling.fetch.reward_on_claimed,
        "fetch_reward_all_tasks": b.scheduling.fetch.reward_all_tasks,
        "failure_bucket": fv.bucket,
        "failure_measurement": int(fv.is_measurement_artifact),
        "failure_cutoff_vs_choice": fv.cutoff_vs_choice or "",
        "scarcity_time_share": b.scarcity.time_used_share,
        "scarcity_cpu_idle": b.scarcity.cpu_idle_share,
        "scarcity_cpu_p95": b.scarcity.cpu_p95_util,
        "scarcity_mem_share": b.scarcity.mem_peak_share,
        "scarcity_binding": "|".join(b.scarcity.binding_limits),
        "scarcity_verdict": b.scarcity.verdict,
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(summary: dict[str, Any], bundles: list[TrialBundle]) -> str:
    t = summary["totals"]
    lines: list[str] = [
        "# AOSBench trajectory analysis",
        "",
        "Generated by `python -m aos_bench.traj_analysis`. **All headline numbers are",
        "clean-trial only**; throttled trials are reported separately because a",
        "rate-limited trial still writes a well-formed `result.json` and simply scores",
        "lower (it is not a model-quality signal).",
        "",
        "## 0. Cohort",
        "",
        f"- trial dirs discovered: {t['n_trial_dirs']}",
        f"- finished: {t['n_finished']} | in flight (no result.json, no exception): {t['n_in_flight']}",
        f"- clean: {t['n_clean']} | throttled: {t['n_throttled']}"
        f" | account_error: {t['n_account_error']} | stunted: {t['n_stunted']}"
        f" | infra_error: {t['n_infra_error']} | unparseable: {t['n_unparseable']}",
        f"- stunted (<= {health.STUNTED_MAX_STEPS} steps) share of finished:"
        f" {_fmt(t['stunted_ratio_of_finished'])}",
        f"- throttled share of finished: {_fmt(t['throttled_ratio_of_finished'])}",
        "",
        "### Throttle detectors that fired",
        "",
    ]
    for label, count in sorted(summary["throttle_labels"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- `{label}`: {count} trials")
    if summary["throttle_buckets"]:
        lines.append("")
        lines.append("Bucket markers seen: " + ", ".join(
            f"`{k}` x{v}" for k, v in sorted(summary["throttle_buckets"].items())
        ))
    if summary["auth_labels"]:
        lines.append("")
        lines.append("Auth failures (separate from throttling): " + ", ".join(
            f"`{k}` x{v}" for k, v in sorted(summary["auth_labels"].items())
        ))
    lines += ["", "### Infra errors by class", ""]
    for cls, count in sorted(summary["infra_error_classes"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- `{cls}`: {count}")
    if summary["missing_artifacts"]:
        lines += ["", "### Missing artifacts (finished trials only)", ""]
        for name, count in sorted(summary["missing_artifacts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{name}`: missing in {count} trials")

    # -- headline table ----------------------------------------------------
    lines += [
        "",
        "## 1. Reward by arm x tier (clean vs throttled)",
        "",
        "| cell (arm-setting-tier) | total | clean n | clean streams | clean mean reward | throttled n |"
        " throttled mean reward | delta | confidence (clean) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in summary["cells"]:
        c, th = cell["clean"], cell["throttled"]
        delta = (
            c["mean_reward"] - th["mean_reward"]
            if c["mean_reward"] is not None and th["mean_reward"] is not None
            else None
        )
        lines.append(
            f"| {cell['cell']} | {cell['n_total']} | {c['n_trials']} |"
            f" {c['n_streams']} | {_fmt(c['mean_reward'])} | {th['n_trials']} |"
            f" {_fmt(th['mean_reward'])} | {_fmt(delta)} | {c['confidence']} |"
        )

    # -- basics ------------------------------------------------------------
    lines += [
        "",
        "## 2. Per-trial basics (clean cohort)",
        "",
        "| cell (arm-setting-tier) | n | steps (mean/med) | turns | tool calls | Agent fan-out |"
        " prompt tok | compl tok | cached tok | reasoning tok | cache hit |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in summary["cells"]:
        c = cell["clean"]
        if not c["n_trials"]:
            continue
        lines.append(
            f"| {cell['cell']} | {c['n_trials']} |"
            f" {_fmt(c['mean_steps'],1)}/{_fmt(c['median_steps'],1)} |"
            f" {_fmt(c['mean_turns'],1)} | {_fmt(c['mean_tool_calls'],1)} |"
            f" {_fmt(c['mean_fanout'],2)} | {_fmt(c['mean_prompt_tokens'],0)} |"
            f" {_fmt(c['mean_completion_tokens'],0)} | {_fmt(c['mean_cached_tokens'],0)} |"
            f" {_fmt(c['mean_reasoning_tokens'],0)} ({c['n_with_reasoning_tokens']} trials) |"
            f" {_fmt(c['mean_cache_hit_ratio'])} |"
        )

    lines += [
        "",
        "### Resource envelope (clean cohort; 4 cores / 8 GiB sandbox)",
        "",
        f"- real CPU-throttling counters available anywhere in this run:"
        f" **{summary['cpu_throttle_data_available']}** (`-1` where the sandbox exposes no `cpu.stat`;"
        " `-1` is treated as unavailable, never zero)",
        "",
        "| cell (arm-setting-tier) | n | cpu util mean | cpu util peak | mem mean % | mem peak % |"
        " step delta mean s | step delta med s | frac steps <10s |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in summary["cells"]:
        c = cell["clean"]
        if not c["n_trials"]:
            continue
        lines.append(
            f"| {cell['cell']} | {c['n_trials']} | {_fmt(c['mean_cpu_util'])} |"
            f" {_fmt(c['peak_cpu_util'])} | {_fmt(c['mean_mem_pct'],1)} |"
            f" {_fmt(c['peak_mem_pct'],1)} | {_fmt(c['mean_step_delta_sec'],1)} |"
            f" {_fmt(c['median_step_delta_sec'],1)} | {_fmt(c['mean_frac_steps_under_10s'])} |"
        )

    # -- performance -------------------------------------------------------
    lines += [
        "",
        "## 3. Performance (clean cohort)",
        "",
        "| cell (arm-setting-tier) | n | full-solve rate | wall s | env setup s | agent setup s |"
        " agent exec s | verifier s | residual s | makespan s | thr/h computed |"
        " thr/h reported | tool frac (upper bound) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in summary["cells"]:
        c = cell["clean"]
        if not c["n_trials"]:
            continue
        ph = c["mean_phase_sec"]
        lines.append(
            f"| {cell['cell']} | {c['n_trials']} |"
            f" {_fmt(c['trial_full_solve_rate'])} | {_fmt(c['mean_wall_sec'],0)} |"
            f" {_fmt(ph['environment_setup'],0)} | {_fmt(ph['agent_setup'],0)} |"
            f" {_fmt(ph['agent_execution'],0)} | {_fmt(ph['verifier'],0)} |"
            f" {_fmt(c['mean_phase_residual_sec'],0)} | {_fmt(c['mean_makespan_sec'],0)} |"
            f" {_fmt(c['mean_throughput_computed'],2)} |"
            f" {_fmt(c['mean_throughput_reported'],2)} | {_fmt(c['mean_tool_frac_upper'])} |"
        )
    lines += [
        "",
        f"`tool frac` caveat: {performance.TrialPerformance.tool_time_note}.",
        "",
    ]
    dis = summary["throughput_disagreements"]
    chk = summary["throughput_numerator_check"]
    lines += [
        f"### Throughput cross-check: {len(dis)} trials disagree with "
        f"`scheduling_metrics.aggregates.throughput_tasks_per_hour` by >"
        f"{performance.THROUGHPUT_TOLERANCE:.0%}",
        "",
        f"Root cause test - *{chk['hypothesis']}*: {chk['n_matching']} match,"
        f" {chk['n_mismatching']} mismatch. **{chk['conclusion']}**",
        "",
        "So the two numbers answer different questions: `reported` = tasks the"
        " **agent claimed** it completed per hour; `computed` = tasks the **pool"
        " observed** reach a terminal state per hour. On heavy tiers the agent"
        " reports far fewer than terminate (it ran out of turns mid-stream), which"
        " is why the gap is largest there.",
    ]
    if dis:
        lines += [
            "",
            "| trial | computed (pool-terminal) | reported (agent-claimed) | rel diff |"
            " n_tasks | n_terminal | n_reported_completed | n_unclaimed |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for row in dis[:20]:
            lines.append(
                f"| {row['job']}/{row['trial']} | {_fmt(row['computed'],2)} |"
                f" {_fmt(row['reported'],2)} | {_fmt(row['rel_diff'])} |"
                f" {row['n_tasks']} | {row['n_terminal_tasks']} |"
                f" {row['n_reported_completed']} | {row['n_unclaimed']} |"
            )

    mem_art = summary["memory_measurement_artifacts"]
    lines += [
        "",
        f"### Memory measurement artifacts: {len(mem_art)} trials",
        "",
        "A cgroup cannot exceed its own `memory.max` without an OOM kill, so any"
        " `memory_current` above 8 GiB is the sampler reading a parent/host cgroup,"
        " not the workload. Peaks for these trials are **unmeasured**, not huge.",
    ]
    if mem_art:
        lines += ["", "| trial | samples over limit | reported peak % of 8 GiB |", "|---|---|---|"]
        for row in sorted(mem_art, key=lambda r: -(r["reported_peak_pct"] or 0)):
            lines.append(
                f"| {row['trial']} | {row['n_samples_over_limit']} |"
                f" {_fmt(row['reported_peak_pct'],1)} |"
            )

    # -- behaviour ---------------------------------------------------------
    sc = summary["shortcuts"]
    lines += [
        "",
        "## 4. Behaviour",
        "",
        "### 4a. Shortcut / grader-access evidence",
        "",
        f"- **`/sandbox/aos/schedule.json` (v0.1 schedule leak): "
        f"{len(sc['schedule_leak_trials'])} trials** "
        f"{'-- CONTAINMENT REGRESSION, investigate' if sc['schedule_leak_trials'] else '-- absent, as required under pool v0.3'}",
        "",
        "| category | calls | trials |",
        "|---|---|---|",
    ]
    for cat, calls in sc["by_category_calls"].items():
        lines.append(f"| `{cat}` | {calls} | {sc['by_category_trials'].get(cat, 0)} |")
    lines += [
        "",
        "Access split (`looked at` vs `used` vs `wrote`):",
        "",
    ]
    for key, count in sc["by_category_access"].items():
        lines.append(f"- `{key}`: {count}")
    lines += [
        "",
        f"- trials that *executed* something at a grader path: {len(sc['grader_use_trials'])}",
        f"- trials that *wrote* to a grader path: {len(sc['grader_write_trials'])}",
        "",
        "Unrecognised network hosts (low severity; benign mirrors are allowlisted):",
        "",
    ]
    for host, count in list(sc["unrecognised_hosts"].items())[:25]:
        lines.append(f"- `{host}`: {count} fetches")

    lines += [
        "",
        "### 4b. Resource awareness (all analysable trials)",
        "",
        "| cell (arm-setting-tier) | cohort | inspect_then_adapt | adapt_only | inspect_only | neither |",
        "|---|---|---|---|---|---|",
    ]
    for cell in summary["cells"]:
        for cohort_key in ("clean", "throttled"):
            c = cell[cohort_key]
            if not c["n_trials"]:
                continue
            rc = c["resource_classes"]
            lines.append(
                f"| {cell['cell']} | {cohort_key} |"
                f" {rc.get('inspect_then_adapt',0)} | {rc.get('adapt_only',0)} |"
                f" {rc.get('inspect_only',0)} | {rc.get('neither',0)} |"
            )

    lines += [
        "",
        "### 4c. Strategy frequency",
        "",
        "| cell (arm-setting-tier) | cohort | serial_poll | claim_batch | fanout | list_first | background_jobs |",
        "|---|---|---|---|---|---|---|",
    ]
    for cell in summary["cells"]:
        for cohort_key in ("clean", "throttled"):
            c = cell[cohort_key]
            if not c["n_trials"]:
                continue
            st = c["strategies"]
            lines.append(
                f"| {cell['cell']} | {cohort_key} | {st.get('serial_poll',0)} |"
                f" {st.get('claim_batch',0)} | {st.get('fanout',0)} | {st.get('list_first',0)} |"
                f" {st.get('background_jobs',0)} |"
            )

    lines += ["", "### Strategy vs reward (clean trials, pooled across arms)", ""]
    lines += _strategy_reward_table(bundles)

    lines += ["", "## Tool histogram by arm", ""]
    for arm, hist in summary["tool_histogram_by_arm"].items():
        top = ", ".join(f"`{k}`={v}" for k, v in list(hist.items())[:12])
        lines.append(f"- **{arm}**: {top}")

    lines += _render_tier_pairing(summary)
    lines += _render_scarcity(summary)
    lines += _render_export_gap(summary)
    lines += _render_concurrency(summary)
    lines += _render_queueing(summary)
    lines += _render_comparisons(summary)
    lines += _render_failures(summary)

    lines += ["", "## Caveats baked into these numbers", ""]
    for key, note in summary["notes"].items():
        lines.append(f"- **{key}**: {note}")
    lines.append("")
    return "\n".join(lines)


def _render_export_gap(summary: dict[str, Any]) -> list[str]:
    gap = summary["export_gap"]
    if not gap["n_affected"]:
        return []
    return (
        [
            "",
            "## R2-0. Blind spot: scored trials with no agent artifacts",
            "",
            f"**{gap['n_affected']} trials** finished, were scored by the verifier and have",
            f"complete scheduling metrics, but exported no `agent/trajectory.json` -- and",
            f"**{gap['n_without_any_transcript']}** of them exported no transcript of any kind.",
            f"Arms affected: {', '.join(gap['arms_affected'])}.",
            "",
            "This is a measurement blind spot, not a result. The throttle detector's only",
            "input is the transcript, so for these trials we cannot tell clean from",
            "throttled. They are classified `unparseable` and excluded from every headline",
            "figure -- which is the safe choice, but it is only *unbiased* if they were",
            "throttled at the same rate as the rest. Their reward versus the clean mean in",
            "the same job is the closest available evidence on that, below.",
            "",
            "| job | affected | no transcript | mean reward (affected) | n clean |"
            " mean reward (clean) | delta |",
            "|---|---|---|---|---|---|---|",
        ]
        + [
            f"| `{row['job']}` | {row['n_affected']} | {row['n_without_any_transcript']} |"
            f" {_fmt(row['mean_reward_affected'])} | {row['n_clean']} |"
            f" {_fmt(row['mean_reward_clean'])} | {_fmt(row['delta_vs_clean'])} |"
            for row in gap["by_job"]
        ]
    )


def _render_concurrency(summary: dict[str, Any]) -> list[str]:
    """Concurrency as a shape: thirds of the makespan, ramp, collapse."""
    lines = [
        "",
        "## R2-A. Realized concurrency as a trajectory, not a scalar",
        "",
        "All figures are **time-weighted** integrals of the pool's own",
        "`curves.concurrency` step function (rebuilt from",
        "`tasks[].claimed_at_sec`/`terminal_at_sec` where the curve is absent -- the",
        "`src` column says which). q1/q2/q3 are the mean in-flight task count during",
        "each third of the window; `ramp` is q3/q1 (<1 = the agent wound down);",
        "`collapse` is the share of time spent serial after first reaching 2 -- kept",
        "only as description, because it over-fires on trials that simply ran out of",
        "work. **`starve` is the one that matters**: share of the window spent serial",
        "*while the pool still had ready tasks queued*. A shape of `starve` means the",
        "agent held one task with a backlog behind it; `drain` means concurrency fell",
        "because there was nothing left to claim.",
        "",
        "The window is `[first_claim, makespan]`, matching the pool: integrating from",
        "t=0 instead disagrees with `mean_realized_concurrency` by up to 4.6% on slow",
        "starters, and from the first claim it matches exactly on every trial.",
        "",
        f"Cross-check: {summary['concurrency_outliers']['cross_check']}"
        f" ({summary['concurrency_outliers']['n_mean_mismatch']} trials disagree).",
        "",
        "| cell (arm-setting-tier) | n | src | tw-mean | pool-mean | peak | q1 | q2 | q3 |"
        " ramp | collapse | **starve** | % time serial | % time idle | shapes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in summary["cells"]:
        c = cell["clean"]
        if not c["n_trials"]:
            continue
        srcs = "/".join(k for k in c["conc_sources"] if k != "none") or "none"
        shapes = ", ".join(f"{k}x{v}" for k, v in sorted(c["conc_shapes"].items(), key=lambda kv: -kv[1]))
        lines.append(
            f"| {cell['cell']} | {c['n_trials']} | {srcs} |"
            f" {_fmt(c['conc_time_weighted_mean'],2)} | {_fmt(c['conc_reported_mean'],2)} |"
            f" {_fmt(c['conc_peak_max'],0)} | {_fmt(c['conc_q1'],2)} | {_fmt(c['conc_q2'],2)} |"
            f" {_fmt(c['conc_q3'],2)} | {_fmt(c['conc_ramp_ratio'],2)} |"
            f" {_fmt(c['conc_collapse_frac'],2)} | **{_fmt(c['conc_starved_frac'],2)}** |"
            f" {_fmt(c['conc_frac_time_serial'],2)} |"
            f" {_fmt(c['conc_frac_time_idle'],2)} | {shapes} |"
        )
    collapsed = summary["concurrency_outliers"]["starved"]
    if collapsed:
        lines += [
            "",
            f"### Trials that STARVED: serial while the pool still had ready work"
            f" ({summary['concurrency_outliers']['n_starved']} total, top 25)",
            "",
            "| trial | status | peak | q1 | q2 | q3 | starved frac | collapse frac |"
            " unclaimed | reward |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for row in collapsed:
            lines.append(
                f"| `{row['trial']}` | {row['status']} | {row['peak']} | {_fmt(row['q1'],2)} |"
                f" {_fmt(row['q2'],2)} | {_fmt(row['q3'],2)} | **{_fmt(row['starved_frac'],2)}** |"
                f" {_fmt(row['collapse_frac'],2)} | {row['n_unclaimed']} |"
                f" {_fmt(row['mean_reward'])} |"
            )
    if summary["concurrency_outliers"]["mean_mismatch"]:
        lines += ["", "### Concurrency cross-check failures (investigate before quoting)", ""]
        for row in summary["concurrency_outliers"]["mean_mismatch"]:
            lines.append(
                f"- `{row['trial']}`: ours {_fmt(row['ours'],2)} vs pool"
                f" {_fmt(row['pool'],2)} (rel {_fmt(row['rel_diff'],3)}, src={row['source']})"
            )
    return lines


def _render_queueing(summary: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## R2-B. Queueing and task ordering",
        "",
        "`wait` = arrival -> claim, `service` = claim -> terminal (wall clock, so a",
        "claimed-then-ignored task looks expensive -- there is no per-task attention",
        "signal in any artifact). `wait~arrival r` is Spearman between arrival order",
        "and wait: strongly positive means late arrivals wait longer, i.e. starvation.",
        "It is **blank for burst**, where all tasks arrive at t=0 and the correlation",
        "is undefined rather than zero. `HOL` is the share of accumulated wait time",
        "that elapsed while the agent was already at its own peak concurrency: high =",
        "genuinely saturated, low = the agent simply did not pull.",
        "",
        "`backlog>=2` is the share of makespan during which the agent had more than one",
        "ready task to choose from; without that, 'FIFO' describes the pool's arrival",
        "schedule and not the agent. `triage r` is Spearman(claim order, service time):",
        "negative = short tasks first.",
        "",
        "| cell (arm-setting-tier) | n | mean wait s | p90 wait s | max wait s |"
        " mean service s | wait share | wait~arrival r | starving | HOL | backlog>=2 |"
        " fifo r | triage r | verdicts |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in summary["cells"]:
        c = cell["clean"]
        if not c["n_trials"]:
            continue
        r = c["queue_wait_arrival_r"]
        # Undefined for two different reasons and they must not be conflated:
        # burst arrivals are all simultaneous (no arrival order exists), while
        # a stream trial that claimed <3 tasks simply has too few pairs.
        if r is not None:
            r_text = f"{_fmt(r,2)} (n={c['n_with_wait_arrival_r']})"
        elif cell["setting"] == "burst":
            r_text = "undefined (burst: all arrive at t=0)"
        else:
            r_text = "undefined (<3 claimed tasks)"
        verdicts = ", ".join(
            f"{k}x{v}" for k, v in sorted(c["order_verdicts"].items(), key=lambda kv: -kv[1])
        )
        lines.append(
            f"| {cell['cell']} | {c['n_trials']} | {_fmt(c['queue_mean_wait_sec'],0)} |"
            f" {_fmt(c['queue_p90_wait_sec'],0)} | {_fmt(c['queue_max_wait_sec'],0)} |"
            f" {_fmt(c['queue_mean_service_sec'],0)} | {_fmt(c['queue_wait_share'],2)} |"
            f" {r_text} | {c['queue_starving_trials']}/{c['n_trials']} |"
            f" {_fmt(c['queue_hol_blocked_frac'],2)} |"
            f" {_fmt(c['order_frac_time_backlog_ge2'],2)} | {_fmt(c['order_fifo_r'],2)} |"
            f" {_fmt(c['order_triage_r'],2)} | {verdicts} |"
        )

    lines += [
        "",
        "### Fetch behaviour: the mechanism behind abandoned work",
        "",
        "Claiming a task requires calling `mcp__aos_pool__todo_next`. So an agent that",
        "left tasks unclaimed did not fail to *finish* work -- it failed to **ask** for",
        "it. `polls/claim` separates two styles that look identical in the reward",
        "column: a tight fetch loop (many polls per task, often blocking) versus",
        "one-poll-per-task. A ratio near 1.0 *with tasks left over* is the pathology:",
        "the agent asked exactly as often as it claimed and then stopped asking.",
        "",
        "**Reward decomposition.** The headline reward divides by *all* tasks, so an",
        "unclaimed task scores 0 and `claimed share` mechanically caps reward -- a",
        "correlation between them is partly arithmetic. `reward|claimed` removes that",
        "floor: it averages only over tasks the agent actually took. A low claimed",
        "share with a healthy `reward|claimed` is a pure **work-acquisition** failure;",
        "both low is a **solving** failure. The gap between the two columns is the",
        "reward the agent forfeited by not asking for work.",
        "",
        "| cell (arm-setting-tier) | n | mean todo_next | polls/claim | claimed share |"
        " trials leaving work | reward\\|claimed | reward (all) | forfeited |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in summary["cells"]:
        c = cell["clean"]
        if not c["n_trials"]:
            continue
        lines.append(
            f"| {cell['cell']} | {c['n_trials']} | {_fmt(c['fetch_todo_next'],1)} |"
            f" {_fmt(c['fetch_polls_per_claim'],2)} | {_fmt(c['fetch_claimed_share'],2)} |"
            f" {c['fetch_n_stopped_asking']}/{c['n_trials']} |"
            f" {_fmt(c['fetch_reward_on_claimed'])} | {_fmt(c['fetch_reward_all_tasks'])} |"
            f" {_fmt((c['fetch_reward_on_claimed'] - c['fetch_reward_all_tasks']) if c['fetch_reward_on_claimed'] is not None and c['fetch_reward_all_tasks'] is not None else None)} |"
        )
    return lines


def _render_comparisons(summary: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## R2-C/D. Paired A/B comparisons",
        "",
        "Reward is paired **by task_id** and averaged within a task id before",
        "differencing, because an unpaired mean compares task mixes rather than",
        "settings (the light->medium control shrank from +0.34 to +0.089 under exactly",
        "this correction). Trial-level quantities cannot be paired by task id, so they",
        "are paired **by stream id** instead and only shared streams contribute.",
        "`p` is a two-sided exact sign test over non-tied ids -- it speaks to direction",
        "only, and `n_eff` is printed because 3 movers cannot support a claim.",
        "",
    ]
    for comp in summary["comparisons"]:
        rw = comp["reward"]
        lines += [
            f"### {comp['label']}",
            "",
            f"- status: {comp['status']}",
            f"- clean trials: {rw['n_trials_a']} ({rw['n_streams_a']} streams) vs"
            f" {rw['n_trials_b']} ({rw['n_streams_b']} streams)",
            f"- shared task ids: {rw['n_shared_task_ids']}"
            f" (A has {rw['n_task_ids_a']}, B has {rw['n_task_ids_b']})",
            f"- **unpaired** reward: {_fmt(rw['unpaired_mean_a'])} -> {_fmt(rw['unpaired_mean_b'])}"
            f" (delta {_fmt(rw['unpaired_delta'])})",
            f"- **paired** reward: {_fmt(rw['paired_mean_a'])} -> {_fmt(rw['paired_mean_b'])}"
            f" (**delta {_fmt(rw['paired_delta'])}**)",
            f"- task ids better/worse/tied under B: {rw['n_better_b']}/{rw['n_worse_b']}/{rw['n_tied']}"
            f"; n_eff={rw['n_effective']}, sign-test p={_fmt(rw['sign_test_p'],3)}",
            f"- confidence: {rw['confidence']}",
            "",
        ]
        floor = comp.get("reward_noise_floor") or {}
        floor_bits = []
        for side, stats in floor.items():
            if stats.get("mean_range") is None:
                continue
            floor_bits.append(
                f"{side}: mean {_fmt(stats['mean_range'],3)}, worst"
                f" {_fmt(stats['max_range'],3)} ({stats['n_streams']} streams,"
                f" {stats['n_trials']} trials)"
            )
        thin_side = min(rw["n_trials_a"], rw["n_trials_b"])
        thin_streams = min(rw["n_streams_a"], rw["n_streams_b"])
        if thin_side < paired.MIN_TRIALS_PER_SIDE or thin_streams < paired.MIN_STREAMS_FOR_CI:
            lines += [
                f"- **PROVISIONAL**: thinnest side has {thin_side} clean trials over"
                f" {thin_streams} streams (want >= {paired.MIN_TRIALS_PER_SIDE} trials,"
                f" >= {paired.MIN_STREAMS_FOR_CI} streams). Do not quote this delta."
                " The glm-burst-medium cell looked significant at n=5 (p=0.013,"
                " +0.144) and decayed to +0.030 (p=0.359) at n=23; treat any"
                " small-n significance here as the same pattern until refilled.",
                "",
            ]
        ci = comp.get("reward_bootstrap_ci") or {}
        if ci.get("ci_low") is not None:
            verdict = (
                "**inside the interval - not distinguishable from zero**"
                if ci["crosses_zero"]
                else "**excludes zero**"
            )
            lines += [
                f"- **95% CI on the paired delta** (cluster bootstrap over"
                f" {ci['n_streams']} whole streams, the unit of replication -- task ids"
                f" within a stream share an agent and a sandbox and are not"
                f" independent): [{_fmt(ci['ci_low'],3)}, {_fmt(ci['ci_high'],3)}];"
                f" {verdict}. {_fmt(ci['frac_positive'],2)} of resamples positive.",
                "",
            ]
        elif ci.get("note"):
            lines += [f"- CI not computed: {ci['note']}", ""]
        if floor_bits:
            lines += [
                "- **noise floor** (trial-mean reward range between repeat attempts at the"
                " *same* stream -- same tasks, same schedule, only seed and gateway"
                " weather differ): " + "; ".join(floor_bits),
                "  A paired delta smaller than this is not detectable at this n,"
                " whatever the sign test says about its direction.",
                "",
            ]
        if rw["top_movers"]:
            lines += ["Largest per-task movers:", ""]
            for mover in rw["top_movers"]:
                lines.append(
                    f"  - `{mover['task_id']}`: {_fmt(mover['mean_a'],2)} ->"
                    f" {_fmt(mover['mean_b'],2)} (delta {_fmt(mover['delta'],2)})"
                )
            lines.append("")
        lines += [
            "Trial-level, paired by stream id:",
            "",
            "| metric | A (unpaired) | B (unpaired) | shared streams | A | B | delta |"
            " streams up/down | sign p |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for name, m in comp["metrics"].items():
            lines.append(
                f"| {name} | {_fmt(m['unpaired_a'],3)} | {_fmt(m['unpaired_b'],3)} |"
                f" {m['n_shared_streams']} | {_fmt(m['by_stream_a'],3)} |"
                f" {_fmt(m['by_stream_b'],3)} | {_fmt(m['by_stream_delta'],3)} |"
                f" {m['n_streams_up']}/{m['n_streams_down']} | {_fmt(m['sign_test_p'],3)} |"
            )
        lines.append("")
    return lines


def _render_failures(summary: dict[str, Any]) -> list[str]:
    tax = summary["failure_taxonomy"]
    lines = [
        "",
        f"## R2-E. Why the low tail is low (reward <= {tax['threshold']})",
        "",
        "Measurement buckets (`throttled`, `infra_error`, `never_claimed_cutoff`,",
        "`unparseable`, `no_scheduling_data`) must not be read as model quality.",
        "`abandoned_backlog` and `never_claimed_by_choice` are **not** artifacts: the",
        "agent ran for hundreds of steps without throttling and stopped fetching work,",
        "which is the behaviour the benchmark is built to detect.",
        "",
        f"- trials with a reward file: {tax['n_trials_scored']}",
        f"- of those, scoring <= {tax['threshold']}: **{tax['n_low']}**",
        f"- measurement artifact: **{tax['n_measurement_artifact']}**"
        f" | capability/behaviour: **{tax['n_capability']}**"
        f" (measurement share {_fmt(tax['measurement_share_of_low_tail'],2)})",
        f"- trials with NO reward file at all: {tax['n_no_reward_file']}"
        f" {tax['no_reward_buckets']} - counted separately from scoring zero, because"
        " 'the verifier never ran' and 'the agent solved nothing' are different facts",
        "",
        "| bucket | trials in low tail |",
        "|---|---|",
    ]
    for bucket, count in tax["buckets"].items():
        lines.append(f"| `{bucket}` | {count} |")
    if tax["by_cell"]:
        lines += ["", "Low-tail buckets by cell:", ""]
        for cell, buckets in sorted(tax["by_cell"].items()):
            detail = ", ".join(f"`{k}`x{v}" for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]))
            lines.append(f"- **{cell}**: {detail}")
    ambiguous = tax["ambiguous_unclaimed_resolved"]
    if ambiguous:
        lines += [
            "",
            "### `n_unclaimed>0` with empty `stream_done_reason`, resolved by trajectory length",
            "",
            "Short trajectory => the agent process was killed (discard). Long trajectory",
            "=> the agent chose not to fetch more work, and that *is* the measurement.",
            "",
            "| trial | steps | claimed/tasks | verdict |",
            "|---|---|---|---|",
        ]
        for row in sorted(ambiguous, key=lambda r: (r["verdict"] or "", r["n_steps"] or 0)):
            lines.append(
                f"| `{row['trial']}` | {row['n_steps']} |"
                f" {row['n_claimed']}/{row['n_tasks']} | **{row['verdict']}** |"
            )
    return lines


def _strategy_reward_table(bundles: list[TrialBundle]) -> list[str]:
    """Reward conditioned on strategy label, clean trials only.

    Pooled across arms deliberately: per-arm-per-strategy cells are n<=3 here,
    and the correlation is confounded by arm anyway (only claude-code-family
    harnesses expose `Agent`), which the caveat line states.
    """
    buckets: dict[str, list[float]] = defaultdict(list)
    for b in bundles:
        if b.health.status != "clean" or b.performance.mean_reward is None:
            continue
        for label in b.behavior.strategies:
            buckets[label].append(b.performance.mean_reward)
        buckets[f"resource:{b.behavior.resource_class}"].append(b.performance.mean_reward)
    rows = ["| label | n clean trials | mean reward |", "|---|---|---|"]
    for label, values in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        rows.append(f"| `{label}` | {len(values)} | {_fmt(sum(values)/len(values))} |")
    rows.append("")
    rows.append(
        "Confounded by arm (`fanout` is only reachable on claude-code/kimi-code, which"
        " also differ in model), so read this as description, not causation."
    )
    return rows


def write_outputs(out_dir: Path, bundles: list[TrialBundle], summary: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    _atomic_write(out_dir / "summary.json", json.dumps(summary, indent=2, default=str) + "\n")

    trials_path = out_dir / "trials.csv"
    tmp = trials_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRIAL_CSV_FIELDS)
        writer.writeheader()
        for b in sorted(bundles, key=lambda x: (x.record.job, x.record.trial_name)):
            writer.writerow(_trial_row(b))
    tmp.replace(trials_path)

    findings_path = out_dir / "findings.csv"
    tmp = findings_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["job", "arm", "tier", "trial", "step_id", "tool", "category", "severity", "access", "snippet"],
        )
        writer.writeheader()
        for b in bundles:
            for f in b.behavior.findings:
                row = asdict(f)
                row["snippet"] = " ".join(str(row["snippet"]).split())[:300]
                writer.writerow(row)
    tmp.replace(findings_path)

    health_path = out_dir / "health.json"
    _atomic_write(
        health_path,
        json.dumps([asdict(b.health) for b in bundles], indent=2, default=str) + "\n",
    )

    _atomic_write(out_dir / "report.md", render_markdown(summary, bundles))


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
