"""Per-trial basics: step counts, tool histogram, tokens, CPU and memory.

Everything here is descriptive, but two of the numbers are easy to compute and
easy to misread, so the definitions are spelled out rather than implied.

**Step latency.** The ATIF trajectory stores one ``timestamp`` per step and
nothing else, so the only latency we can derive is the *step delta*:
``timestamp[i+1] - timestamp[i]``. That delta contains the model's
time-to-first-token, its generation time, **and the full execution time of
whatever tool the step called** — a step that ran ``make -j4`` for 8 minutes
produces an 8-minute "latency". It is therefore reported as
``step_delta_*`` and explicitly not as LLM latency. To make it marginally more
interpretable we also report a trimmed median (median is far more robust than
the mean here, because the distribution is a short-step bulk plus a long-build
tail) and the share of deltas under 10s, which is a rough proxy for "steps that
were dominated by the model rather than by a tool".

**CPU utilisation.** ``cpu_nr_periods`` / ``cpu_nr_throttled`` /
``cpu_throttled_usec`` are *always* -1 in sandboxes without a cgroup
``cpu.stat`` file, so real CPU-throttling data does not exist for this run and the
loader maps them to ``None``. Instead we use the utilisation proxy
``Δcpu_usage_usec / (cores × Δt)`` with ``cores = 4`` and ``Δt = 5s``
(``AOS_CGROUP_POLL_SEC``), and with ``cores`` read **per trial** from the
container's own ``cpu.max`` (``cpu_quota_usec / cpu_period_usec``) rather than
hardcoded. That matters because ``cfg-glm-stream-heavy-2c4g`` runs at 2 cores /
4 GiB: dividing its usage by 4 would report every utilisation figure at half
its true value and make the halved sandbox look *less* loaded than the full
one. 1.0 means the container saturated its own quota over that window; values
marginally above 1.0 occur from sampling jitter and are not clipped, so that
jitter stays visible.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from aos_bench.traj_analysis.loader import (
    CGROUP_POLL_SEC,
    SANDBOX_CORES,
    SANDBOX_MEMORY_BYTES,
    TrialRecord,
)

# Tool names that mean "spawn a subagent". ``Agent`` is the real fan-out call in
# claude-code/kimi-code. ``Task`` is only the init-time declaration of the tool
# and must NOT be counted, or every trial looks like it fanned out once.
FANOUT_TOOLS = frozenset({"Agent"})

# Pool MCP tools, split so we can talk about scheduling behaviour separately
# from ordinary work.
POOL_TOOL_PREFIX = "mcp__aos_pool__"

# Step deltas above this are almost certainly tool-dominated (long builds,
# blocking todo_next polls with timeout_sec=60, etc).
MODEL_DOMINATED_DELTA_SEC = 10.0


@dataclass
class TrialMetrics:
    trial: str
    job: str
    arm: str
    tier: str
    harness: str
    model_name: str | None

    n_steps: int = 0
    n_agent_steps: int = 0
    n_turns: int = 0
    n_tool_calls: int = 0
    tool_histogram: dict[str, int] = field(default_factory=dict)
    n_pool_calls: int = 0
    n_todo_next: int = 0
    n_fanout: int = 0

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cache_hit_ratio: float | None = None

    step_delta_mean_sec: float | None = None
    step_delta_median_sec: float | None = None
    step_delta_p90_sec: float | None = None
    step_delta_max_sec: float | None = None
    frac_steps_under_10s: float | None = None
    n_step_deltas: int = 0

    cpu_util_mean: float | None = None
    cpu_util_peak: float | None = None
    # Sandbox shape actually measured for this trial (read from cpu.max /
    # memory.max), not the grid default. 2 cores / 4 GiB for the 2c4g probe.
    cpu_cores: float = SANDBOX_CORES
    mem_limit_bytes: int = SANDBOX_MEMORY_BYTES
    # Share of sampling windows above 7/8 of the core count -- "how often was
    # this container effectively CPU-bound". Used for the 2c4g pressure claim.
    frac_cpu_windows_near_saturation: float | None = None
    cpu_throttle_data_available: bool = False
    mem_mean_bytes: float | None = None
    mem_peak_bytes: int | None = None
    mem_mean_pct_limit: float | None = None
    mem_peak_pct_limit: float | None = None
    pids_peak: int | None = None
    n_cgroup_samples: int = 0
    # Samples where memory_current exceeded memory.max. Physically impossible
    # for the cgroup itself, so a non-zero count means the sampler attributed
    # host/sibling memory to this container (see mem_over_limit_note).
    n_samples_over_mem_limit: int = 0
    mem_over_limit_note: str = ""


def _parse_ts(raw: Any) -> float | None:
    """Parse an ATIF step timestamp to epoch seconds.

    Two formats appear in the same corpus: ``...Z`` (codex, claude-code) and
    ``...+00:00`` (kimi-code). ``fromisoformat`` handles the offset form
    natively; the ``Z`` suffix is rewritten. Anything else yields ``None`` so
    one odd step cannot poison a whole trial's timing.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile. Avoids a numpy dependency."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def step_deltas(record: TrialRecord) -> list[float]:
    """Non-negative gaps between consecutive parseable step timestamps.

    Negative gaps are dropped: sub-agent steps are interleaved into the parent
    trajectory by the ATIF exporter, so ordering by ``step_id`` does not
    guarantee monotonic time once a trial fanned out. Dropping them is safer
    than taking ``abs()``, which would invent latency out of interleaving.
    """
    stamps = [t for t in (_parse_ts(s.get("timestamp")) for s in record.steps if isinstance(s, dict)) if t is not None]
    return [b - a for a, b in zip(stamps, stamps[1:]) if b >= a]


def cpu_utilisation(record: TrialRecord) -> tuple[float | None, float | None, bool]:
    """Return ``(mean_util, peak_util, throttle_data_available)``.

    ``util = Δcpu_usage_usec / (cores × Δt_usec)``. ``cpu_usage_usec`` is a
    monotonic counter, so a negative delta means the sampler restarted and the
    window is discarded. ``throttle_data_available`` reports whether *any*
    sample carried real ``cpu.stat`` data — without ``cpu.stat`` this is always False,
    and saying so is the point: it stops a reader concluding "no throttling".
    """
    usages = [(s.sample, s.cpu_usage_usec) for s in record.cgroup if s.cpu_usage_usec is not None]
    throttle_available = any(
        s.cpu_nr_periods is not None or s.cpu_nr_throttled is not None for s in record.cgroup
    )
    if len(usages) < 2:
        return None, None, throttle_available

    cores = record.sandbox_cores
    utils: list[float] = []
    for (idx_a, usage_a), (idx_b, usage_b) in zip(usages, usages[1:]):
        d_usage = usage_b - usage_a
        n_intervals = max(1, idx_b - idx_a)
        window_usec = n_intervals * CGROUP_POLL_SEC * 1e6
        if d_usage < 0 or window_usec <= 0:
            continue
        utils.append(d_usage / (cores * window_usec))
    if not utils:
        return None, None, throttle_available
    return sum(utils) / len(utils), max(utils), throttle_available


def cpu_util_series(record: TrialRecord) -> list[tuple[float, float]]:
    """Per-window ``(t_sec, util)`` series, for within-trial pressure analysis.

    ``t_sec`` is the *end* of the window, derived from the sample index and
    :data:`CGROUP_POLL_SEC` (the TSV has no wall clock). Same divisor rules as
    :func:`cpu_utilisation`.
    """
    usages = [(s.sample, s.cpu_usage_usec) for s in record.cgroup if s.cpu_usage_usec is not None]
    if len(usages) < 2:
        return []
    cores = record.sandbox_cores
    out: list[tuple[float, float]] = []
    for (idx_a, usage_a), (idx_b, usage_b) in zip(usages, usages[1:]):
        d_usage = usage_b - usage_a
        n_intervals = max(1, idx_b - idx_a)
        window_usec = n_intervals * CGROUP_POLL_SEC * 1e6
        if d_usage < 0 or window_usec <= 0:
            continue
        out.append((idx_b * CGROUP_POLL_SEC, d_usage / (cores * window_usec)))
    return out


def compute(record: TrialRecord) -> TrialMetrics:
    """Descriptive metrics for one trial. Missing inputs stay ``None``."""
    out = TrialMetrics(
        trial=record.trial_name,
        job=record.job,
        arm=record.arm,
        tier=record.tier,
        harness=record.harness,
        model_name=record.model_name,
    )

    steps = record.steps
    out.n_steps = len(steps)
    out.n_agent_steps = sum(1 for s in steps if isinstance(s, dict) and s.get("source") == "agent")

    histogram: Counter[str] = Counter()
    for _step, call in record.tool_calls():
        name = str(call.get("function_name") or "<unnamed>")
        histogram[name] += 1
    out.tool_histogram = dict(histogram.most_common())
    out.n_tool_calls = sum(histogram.values())
    out.n_pool_calls = sum(v for k, v in histogram.items() if k.startswith(POOL_TOOL_PREFIX))
    out.n_todo_next = histogram.get(f"{POOL_TOOL_PREFIX}todo_next", 0)
    out.n_fanout = sum(histogram.get(name, 0) for name in FANOUT_TOOLS)

    # "Turns" = agent steps that issued at least one tool call. A step with no
    # tool call is narration/reasoning, not an action, so counting raw steps
    # would let a chatty harness look busier than a terse one.
    out.n_turns = sum(
        1
        for s in steps
        if isinstance(s, dict) and s.get("source") == "agent" and isinstance(s.get("tool_calls"), list) and s["tool_calls"]
    )

    fm = record.final_metrics
    extra = fm.get("extra") if isinstance(fm.get("extra"), dict) else {}

    def _int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    out.prompt_tokens = _int(fm.get("total_prompt_tokens"))
    out.completion_tokens = _int(fm.get("total_completion_tokens"))
    out.cached_tokens = _int(fm.get("total_cached_tokens"))
    # Only codex reports reasoning tokens; glm/cc/kimi omit the field entirely
    # (verified: 147 trials with no reasoning_output_tokens). None != 0.
    out.reasoning_tokens = _int(extra.get("reasoning_output_tokens"))
    total = _int(extra.get("total_tokens"))
    if total is None and out.prompt_tokens is not None and out.completion_tokens is not None:
        total = out.prompt_tokens + out.completion_tokens
    out.total_tokens = total
    if out.prompt_tokens and out.cached_tokens is not None:
        out.cache_hit_ratio = out.cached_tokens / out.prompt_tokens

    deltas = step_deltas(record)
    out.n_step_deltas = len(deltas)
    if deltas:
        out.step_delta_mean_sec = sum(deltas) / len(deltas)
        out.step_delta_median_sec = float(median(deltas))
        out.step_delta_p90_sec = _percentile(deltas, 0.90)
        out.step_delta_max_sec = max(deltas)
        out.frac_steps_under_10s = sum(1 for d in deltas if d < MODEL_DOMINATED_DELTA_SEC) / len(deltas)

    out.cpu_util_mean, out.cpu_util_peak, out.cpu_throttle_data_available = cpu_utilisation(record)
    out.cpu_cores = record.sandbox_cores
    out.mem_limit_bytes = record.sandbox_memory_bytes
    series = cpu_util_series(record)
    if series:
        # 7/8 of quota: high enough that the container is competing with itself
        # for CPU, low enough not to require a perfectly-aligned sample.
        out.frac_cpu_windows_near_saturation = sum(1 for _, u in series if u >= 0.875) / len(series)

    mems = [s.memory_current for s in record.cgroup if s.memory_current is not None]
    peaks = [s.memory_peak for s in record.cgroup if s.memory_peak is not None]
    pids = [s.pids_current for s in record.cgroup if s.pids_current is not None]
    out.n_cgroup_samples = len(record.cgroup)
    limit = out.mem_limit_bytes
    if mems:
        out.mem_mean_bytes = sum(mems) / len(mems)
        out.mem_mean_pct_limit = 100.0 * out.mem_mean_bytes / limit
    if peaks:
        out.mem_peak_bytes = max(peaks)
        out.mem_peak_pct_limit = 100.0 * out.mem_peak_bytes / limit
    if pids:
        out.pids_peak = max(pids)

    # A cgroup cannot exceed its own memory.max without being OOM-killed, so
    # any sample above the limit is a *measurement* artifact, not the workload
    # using 46 GiB. Flag it loudly instead of publishing a >100% peak as fact.
    out.n_samples_over_mem_limit = sum(1 for m in mems if m > limit)
    if out.n_samples_over_mem_limit:
        out.mem_over_limit_note = (
            f"{out.n_samples_over_mem_limit}/{len(mems)} samples report "
            f"memory_current above memory.max ({limit / 1024**3:.0f} GiB); physically "
            "impossible for this cgroup, so the sampler read a parent/host cgroup for "
            "those samples. Treat this trial's memory peak as unmeasured."
        )

    return out
