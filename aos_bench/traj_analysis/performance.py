"""Outcome metrics: pass rate, wall-clock phase split, throughput, tool share.

Four things worth knowing before reading a number out of here.

**Pass rate has two denominators and they answer different questions.**
*Per-sub-task* pass rate (``n_passed / n_tasks``, the mean reward) is the
benchmark score. *Per-trial* pass rate (did the trial solve **all** its tasks)
collapses to ~0 at heavy tiers and is only useful for light. Both are emitted;
mixing them up is the easiest way to produce an unreproducible headline.

**Phase timings come only from ``result.json``.** The four phase dicts
(``environment_setup``, ``agent_setup``, ``agent_execution``, ``verifier``) each
carry ``started_at``/``finished_at``. ``total_wall_sec`` is
``finished_at - started_at`` at the top level, which is *not* the sum of phases —
harbor has gaps between phases (sandbox handshakes). We report both and the
residual, because a large residual is a real signal about launcher overhead.

**Throughput is cross-checked, never trusted blind.** We recompute
``n_terminal / makespan × 3600`` and compare against
``scheduling_metrics.aggregates.throughput_tasks_per_hour``. Disagreement is
surfaced per trial rather than smoothed over — and on this corpus the cause was
identified by inverting the pool's own number: for 227/227 trials,
``reported × makespan / 3600`` equals ``aggregates.n_reported_completed``
exactly. So the pool counts tasks the **agent said it finished**, while we count
tasks that reached a terminal state in the task table. The two differ whenever
the agent marked a task done that the pool never saw terminate, or vice versa.
Both are emitted (``throughput_computed`` / ``throughput_reported``) plus
``throughput_numerator_reported``, so a reader can pick the definition they mean
instead of inheriting one silently. Neither is "wrong"; conflating them is.

**Tool-call time is NOT cleanly recoverable, and this module says so.** The ATIF
trajectory stores one timestamp per step and no per-tool-call duration; the
harness transcripts do carry ``item.started``/``item.completed`` events for
codex but no wall-clock stamps on them, and claude-code's ``tool_result`` blocks
likewise carry no duration. So the honest answer is a *bound*, not a value:

* ``tool_attributable_frac`` = (Σ deltas following a tool-calling step) /
  (Σ all deltas). This is an **upper bound** on the tool-time share, because
  each such delta also contains the model's own generation time for the next
  step.
* ``model_only_frac_lower_bound`` = share of total delta time in deltas after
  steps with **no** tool call — a lower bound on pure model time.

Any single scalar "tool_call_time / total_time" would be invented. Callers get
the bound and the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aos_bench.traj_analysis.loader import TrialRecord
from aos_bench.traj_analysis.metrics import _parse_ts

PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")

# A recomputed vs reported throughput gap larger than this (relative) is
# reported as a disagreement rather than rounding noise.
THROUGHPUT_TOLERANCE = 0.02

# Reward >= this counts as a pass. The verifier emits 0.0/1.0 for most task
# families but wildclaw-style judges emit fractions, so the threshold is
# explicit rather than `== 1.0`.
PASS_THRESHOLD = 1.0


@dataclass
class TrialPerformance:
    trial: str
    job: str
    arm: str
    tier: str

    n_tasks: int | None = None
    n_passed: int | None = None
    mean_reward: float | None = None
    trial_fully_solved: bool | None = None
    n_unclaimed: int | None = None
    n_timeout: int | None = None
    stream_done_reason: str | None = None

    total_wall_sec: float | None = None
    phase_sec: dict[str, float | None] = field(default_factory=dict)
    phase_residual_sec: float | None = None

    makespan_sec: float | None = None
    mean_wait_sec: float | None = None
    mean_service_sec: float | None = None
    mean_flow_sec: float | None = None
    mean_concurrency: float | None = None
    peak_concurrency: int | None = None

    throughput_computed: float | None = None
    throughput_reported: float | None = None
    throughput_disagreement: float | None = None
    n_terminal_tasks: int | None = None
    # ``reported x makespan / 3600``: the task count the pool's own aggregate
    # implies. Verified to equal ``aggregates.n_reported_completed`` on this
    # corpus, i.e. agent-reported completions rather than pool-observed ones.
    throughput_numerator_reported: float | None = None
    n_reported_completed: int | None = None

    tool_attributable_frac_upper: float | None = None
    model_only_frac_lower: float | None = None
    tool_time_recoverable: bool = False
    tool_time_note: str = (
        "upper bound only: per-tool-call durations are absent from ATIF and from "
        "all three harness transcripts"
    )


def _phase_duration(result: dict[str, Any], phase: str) -> float | None:
    block = result.get(phase)
    if not isinstance(block, dict):
        return None
    start = _iso(block.get("started_at"))
    end = _iso(block.get("finished_at"))
    if start is None or end is None:
        return None
    return end - start


def _iso(raw: Any) -> float | None:
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def tool_time_bounds(record: TrialRecord) -> tuple[float | None, float | None]:
    """Upper bound on tool-time share and lower bound on model-only share.

    A delta ``t[i+1] - t[i]`` is attributed to "tool" iff step ``i`` issued at
    least one tool call. That delta still contains the model's generation time
    for step ``i+1``, hence upper bound. See the module docstring.
    """
    pairs: list[tuple[bool, float]] = []
    prev_ts: float | None = None
    prev_had_tool = False
    for step in record.steps:
        if not isinstance(step, dict):
            continue
        stamp = _parse_ts(step.get("timestamp"))
        if stamp is None:
            continue
        if prev_ts is not None and stamp >= prev_ts:
            pairs.append((prev_had_tool, stamp - prev_ts))
        calls = step.get("tool_calls")
        prev_had_tool = isinstance(calls, list) and bool(calls)
        prev_ts = stamp

    total = sum(d for _, d in pairs)
    if total <= 0:
        return None, None
    tool_time = sum(d for had_tool, d in pairs if had_tool)
    return tool_time / total, (total - tool_time) / total


def compute(record: TrialRecord) -> TrialPerformance:
    out = TrialPerformance(
        trial=record.trial_name, job=record.job, arm=record.arm, tier=record.tier
    )

    # --- rewards ---------------------------------------------------------
    # reward.json is the authoritative per-sub-task score; scheduling_metrics
    # carries its own n_passed which we keep separately for cross-checking.
    if record.reward:
        rewards = list(record.reward.values())
        out.n_tasks = len(rewards)
        out.n_passed = sum(1 for r in rewards if r >= PASS_THRESHOLD)
        out.mean_reward = sum(rewards) / len(rewards)
        out.trial_fully_solved = out.n_passed == out.n_tasks

    # --- phase wall clock ------------------------------------------------
    if record.result:
        start = _iso(record.result.get("started_at"))
        end = _iso(record.result.get("finished_at"))
        if start is not None and end is not None:
            out.total_wall_sec = end - start
        out.phase_sec = {phase: _phase_duration(record.result, phase) for phase in PHASES}
        known = [v for v in out.phase_sec.values() if v is not None]
        if out.total_wall_sec is not None and len(known) == len(PHASES):
            out.phase_residual_sec = out.total_wall_sec - sum(known)

    # --- scheduling ------------------------------------------------------
    sched = record.scheduling or {}
    agg = sched.get("aggregates") if isinstance(sched.get("aggregates"), dict) else {}
    out.stream_done_reason = sched.get("stream_done_reason")

    def _f(key: str) -> float | None:
        value = agg.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _i(key: str) -> int | None:
        value = agg.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    out.makespan_sec = _f("makespan_sec")
    out.mean_wait_sec = _f("mean_wait_sec")
    out.mean_service_sec = _f("mean_service_sec")
    out.mean_flow_sec = _f("mean_flow_time_sec")
    out.mean_concurrency = _f("mean_realized_concurrency")
    out.peak_concurrency = _i("peak_realized_concurrency")
    out.n_unclaimed = _i("n_unclaimed")
    out.n_timeout = _i("n_timeout")
    out.throughput_reported = _f("throughput_tasks_per_hour")

    # Recompute from the task table so the denominator is explicit: only tasks
    # that actually reached a terminal state can have been "thrown through" the
    # system. Using n_tasks instead would credit the agent for work it never
    # claimed, which is exactly how heavy-tier throughput gets overstated.
    tasks = sched.get("tasks") if isinstance(sched.get("tasks"), dict) else {}
    n_terminal = sum(
        1
        for t in tasks.values()
        if isinstance(t, dict) and t.get("terminal_at_sec") is not None
    )
    out.n_terminal_tasks = n_terminal
    out.n_reported_completed = _i("n_reported_completed")
    if out.makespan_sec and out.makespan_sec > 0:
        out.throughput_computed = n_terminal / out.makespan_sec * 3600.0
        if out.throughput_reported:
            out.throughput_disagreement = out.throughput_computed - out.throughput_reported
            out.throughput_numerator_reported = out.throughput_reported * out.makespan_sec / 3600.0

    # --- tool time bounds ------------------------------------------------
    upper, lower = tool_time_bounds(record)
    out.tool_attributable_frac_upper = upper
    out.model_only_frac_lower = lower
    out.tool_time_recoverable = False

    return out
