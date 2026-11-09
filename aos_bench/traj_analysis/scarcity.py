"""Does the benchmark setting actually impose scarcity?

This module exists to test an interpretive claim that decides what the whole
benchmark can say. AOSBench presents itself as measuring how an agent behaves
under *resource pressure* — a fixed sandbox, a wall-clock budget, and more work
arriving than can be done at once. If none of those limits ever binds, then the
benchmark measures task-solving skill with extra scheduling bookkeeping, and any
framing about resource management is unsupported.

Four independent bindingness tests, each phrased so that a *negative* result is
informative rather than absent:

``time``
    ``used / granted`` where granted is the agent timeout from
    ``result.json:config.agent.override_timeout_sec`` (43200 s here) and used is
    the measured ``agent_execution`` span. A budget that is never more than a
    third consumed cannot be the binding constraint. Read per trial, not
    assumed, because the multiplier differs by job.

``cpu``
    Share of 5 s cgroup windows below :data:`CPU_IDLE_THRESHOLD` of the
    container's own quota. High idleness *at high concurrency* is the strong
    form of the argument: it means the sandbox was not the bottleneck even when
    the agent was maximally loaded, so the agent was waiting on the model or on
    its own serialisation, not on cores.

``memory``
    Peak as a share of ``memory.max``. Memory is the one limit the 2c4g probe
    did move (18.5% -> 43.5%), so this separates "no limit binds" from "this
    particular limit does not bind".

``queue``
    Whether the pool ever actually made the agent choose: share of makespan with
    backlog >= 2, and whether tasks went unclaimed. A stream that hands out work
    slower than the agent consumes it imposes no scheduling problem at all,
    which would make the scheduling metrics descriptive rather than a test.

The verdict function deliberately returns ``"no binding constraint detected"``
rather than a score. A composite "scarcity index" would invite exactly the
over-reading this module is meant to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aos_bench.traj_analysis.loader import TrialRecord
from aos_bench.traj_analysis.metrics import cpu_util_series
from aos_bench.traj_analysis.performance import _iso

# Below this share of its own CPU quota, a sampling window counts as idle.
CPU_IDLE_THRESHOLD = 0.25

# Above this share of the granted wall clock, time is plausibly binding.
TIME_BINDING_SHARE = 0.80

# Above this share of memory.max, memory is plausibly binding.
MEM_BINDING_SHARE = 0.80

# Concurrency at or above this counts as "the agent was heavily loaded", used
# for the strong form of the CPU-idleness argument.
HEAVY_LOAD_CONCURRENCY = 4.0


@dataclass
class ScarcityProfile:
    """Which declared limits, if any, actually bound this trial."""

    granted_wall_sec: float | None = None
    used_wall_sec: float | None = None
    time_used_share: float | None = None
    time_binding: bool | None = None

    n_cpu_windows: int = 0
    cpu_idle_share: float | None = None
    cpu_p95_util: float | None = None
    cpu_binding: bool | None = None

    mem_peak_share: float | None = None
    mem_binding: bool | None = None

    frac_time_backlog_ge2: float | None = None
    n_unclaimed: int | None = None
    queue_binding: bool | None = None

    binding_limits: list[str] = field(default_factory=list)
    verdict: str = "unknown"


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def granted_wall_sec(record: TrialRecord) -> float | None:
    """Agent wall-clock budget in seconds, from the trial's own config.

    Read rather than hardcoded: ``timeout_multiplier`` and
    ``override_timeout_sec`` vary per job, and a wrong denominator would turn
    "used 12% of budget" into a headline either way.
    """
    config = (record.result or {}).get("config")
    if not isinstance(config, dict):
        return None
    agent = config.get("agent")
    if not isinstance(agent, dict):
        return None
    for key in ("override_timeout_sec", "max_timeout_sec"):
        value = agent.get(key)
        try:
            granted = float(value)
        except (TypeError, ValueError):
            continue
        if granted > 0:
            multiplier = config.get("agent_timeout_multiplier") or config.get("timeout_multiplier")
            try:
                factor = float(multiplier)
            except (TypeError, ValueError):
                factor = 1.0
            return granted * (factor if factor > 0 else 1.0)
    return None


def compute(record: TrialRecord, *, concurrency_mean: float | None = None) -> ScarcityProfile:
    """Bindingness profile for one trial.

    ``concurrency_mean`` is passed in rather than recomputed so this module does
    not depend on ``scheduling``; the caller already has it.
    """
    out = ScarcityProfile()

    out.granted_wall_sec = granted_wall_sec(record)
    result = record.result or {}
    block = result.get("agent_execution")
    if isinstance(block, dict):
        start, end = _iso(block.get("started_at")), _iso(block.get("finished_at"))
        if start is not None and end is not None and end >= start:
            out.used_wall_sec = end - start
    if out.granted_wall_sec and out.used_wall_sec is not None:
        out.time_used_share = out.used_wall_sec / out.granted_wall_sec
        out.time_binding = out.time_used_share >= TIME_BINDING_SHARE

    series = cpu_util_series(record)
    out.n_cpu_windows = len(series)
    if series:
        utils = [u for _, u in series]
        out.cpu_idle_share = sum(1 for u in utils if u < CPU_IDLE_THRESHOLD) / len(utils)
        out.cpu_p95_util = _percentile(utils, 0.95)
        # Binding means the container spent a real fraction of its life pinned
        # at its own quota, not merely touched it once.
        out.cpu_binding = bool(out.cpu_p95_util and out.cpu_p95_util >= 0.95)

    limit = record.sandbox_memory_bytes
    peaks = [s.memory_peak for s in record.cgroup if s.memory_peak is not None]
    # Samples above the cgroup's own limit are a sampler artifact (it read a
    # parent cgroup), so they cannot be used to claim memory was binding.
    usable = [p for p in peaks if p <= limit]
    if usable and limit:
        out.mem_peak_share = max(usable) / limit
        out.mem_binding = out.mem_peak_share >= MEM_BINDING_SHARE

    sched = record.scheduling or {}
    agg = sched.get("aggregates") if isinstance(sched.get("aggregates"), dict) else {}
    curves = sched.get("curves") if isinstance(sched.get("curves"), dict) else {}
    try:
        out.n_unclaimed = int(agg.get("n_unclaimed"))
    except (TypeError, ValueError):
        out.n_unclaimed = None
    try:
        makespan = float(agg.get("makespan_sec"))
    except (TypeError, ValueError):
        makespan = 0.0
    backlog = curves.get("backlog")
    if isinstance(backlog, list) and makespan > 0:
        below = 0.0
        level = 0.0
        cursor = 0.0
        for item in backlog:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                t, value = float(item[0]), float(item[1])
            except (TypeError, ValueError):
                continue
            if t >= makespan:
                break
            if t > cursor:
                if level <= 1.0:
                    below += t - cursor
                cursor = t
            level = value
        if cursor < makespan and level <= 1.0:
            below += makespan - cursor
        out.frac_time_backlog_ge2 = 1.0 - below / makespan
        # The pool posed a real scheduling problem if the agent either had a
        # standing choice or could not keep up with arrivals.
        out.queue_binding = out.frac_time_backlog_ge2 > 0.25 or bool(out.n_unclaimed)

    for name, binding in (
        ("time", out.time_binding),
        ("cpu", out.cpu_binding),
        ("memory", out.mem_binding),
        ("queue", out.queue_binding),
    ):
        if binding:
            out.binding_limits.append(name)

    if not any(
        v is not None for v in (out.time_binding, out.cpu_binding, out.mem_binding, out.queue_binding)
    ):
        out.verdict = "unknown"
    elif out.binding_limits:
        out.verdict = "binding: " + ",".join(out.binding_limits)
    else:
        out.verdict = "no binding constraint detected"
    return out


def summarise(
    profiles: list[ScarcityProfile], *, heavy_load_profiles: list[ScarcityProfile] | None = None
) -> dict[str, Any]:
    """Aggregate bindingness across a cohort."""

    def _mean(values: list[float]) -> float | None:
        vals = [v for v in values if v is not None]
        return sum(vals) / len(vals) if vals else None

    counts: dict[str, int] = {}
    for profile in profiles:
        for limit in profile.binding_limits:
            counts[limit] = counts.get(limit, 0) + 1
    n_none = sum(1 for p in profiles if p.verdict == "no binding constraint detected")
    return {
        "n_trials": len(profiles),
        "mean_time_used_share": _mean([p.time_used_share for p in profiles]),
        "max_time_used_share": max(
            [p.time_used_share for p in profiles if p.time_used_share is not None], default=None
        ),
        "mean_cpu_idle_share": _mean([p.cpu_idle_share for p in profiles]),
        "mean_cpu_p95_util": _mean([p.cpu_p95_util for p in profiles]),
        "mean_mem_peak_share": _mean([p.mem_peak_share for p in profiles]),
        "max_mem_peak_share": max(
            [p.mem_peak_share for p in profiles if p.mem_peak_share is not None], default=None
        ),
        "mean_frac_time_backlog_ge2": _mean([p.frac_time_backlog_ge2 for p in profiles]),
        "n_binding_by_limit": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "n_no_binding_constraint": n_none,
        "share_no_binding_constraint": n_none / len(profiles) if profiles else None,
        "heavy_load_cpu_idle_share": (
            _mean([p.cpu_idle_share for p in heavy_load_profiles])
            if heavy_load_profiles
            else None
        ),
        "n_heavy_load": len(heavy_load_profiles) if heavy_load_profiles else 0,
    }
