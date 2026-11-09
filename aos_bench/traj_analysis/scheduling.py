"""Scheduling behaviour: concurrency *dynamics*, queueing, and task ordering.

Round 1 reported ``mean_realized_concurrency`` as a column. That is the wrong
shape for the question the benchmark exists to ask, which is not "how parallel
was the agent on average" but "**what did the agent do as work arrived**".
A mean of 2.5 is produced equally by an agent that held 2-3 tasks in flight the
whole time and by one that peaked at 8 early and collapsed to 1 for the rest of
the run. Those are opposite behaviours and only the second is a failure.

Three things are computed here, each from the pool's own task table so that
nothing depends on the agent's self-report.

**1. Concurrency as a function of time** (:func:`concurrency_profile`).
``curves.concurrency`` is a step function: a list of ``[t_sec, level]`` points
emitted on every change. It is NOT sampled at a fixed rate, so averaging the
*points* over-weights rapid transitions and is not the same as the pool's
``mean_realized_concurrency`` (which is time-weighted). We therefore integrate
properly: every statistic here is time-weighted. When ``curves`` is absent we
rebuild the identical step function from ``tasks[].claimed_at_sec`` /
``terminal_at_sec``, and :func:`concurrency_profile` records which source it
used so a reader can tell a measured curve from a reconstructed one.

**The integration window is ``[first_claim, makespan]``, not ``[0, makespan]``.**
This was established empirically, not assumed: integrating from 0 reproduces
the pool's ``mean_realized_concurrency`` for most trials but drifts by up to
4.6% on trials with a long pre-claim dead zone (``cc-stream-light`` spends up
to 99 s before the first claim). Integrating from the first claim reproduces
the pool's figure **exactly on 268/268 trials that have both**. So the pool's
metric deliberately excludes agent-setup dead time, and ``frac_time_idle``
here is therefore idleness *after work started*, not including startup. Our
integral is cross-checked against the pool aggregate on every run and a
mismatch is reported rather than averaged away.

Shape is summarised by thirds of that window (``conc_q1/q2/q3``) plus:

* ``ramp_ratio`` = late third / early third. <1 means the agent wound *down*.
* ``collapse_frac`` = share of the window spent at concurrency <= 1 **after**
  the agent first reached 2. ``None`` when it never reached 2, so "serial
  throughout" and "went parallel then gave up" stay distinguishable.
* ``starved_frac`` = share of the window spent at concurrency <= 1 **while the
  pool still had ready work in backlog**. This is the one that matters.
  ``collapse_frac`` alone over-fires badly: a light trial that finishes its
  last task serially has ``collapse_frac`` 0.98 and is not a failure at all,
  it simply ran out of work. ``starved_frac`` is high only when the agent was
  running one task at a time *with a queue behind it*, which is the actual
  pathology. Shape classification therefore keys on ``starved_frac``, and
  ``collapse_frac`` is retained only as a descriptive column.
* ``time_weighted_mean`` -- our own integral.

**2. Queueing** (:func:`queue_profile`). ``wait_sec`` is arrival -> claim,
``service_sec`` is claim -> terminal, ``turnaround_sec`` is their sum. The
interesting question is whether waits are *concentrated on late arrivals*
(starvation) or spread evenly. ``wait_arrival_slope_r`` is the Spearman
correlation between arrival order and wait; strongly positive means late tasks
wait longer, i.e. the agent is not keeping up with arrivals. For burst streams
every task arrives at t=0, so arrival order carries no information and the
correlation is reported as ``None`` rather than as a meaningless number.

Head-of-line blocking is measured as ``hol_blocked_frac``: the share of
task-wait-time accumulated while the agent already had its *maximum observed*
concurrency in flight. High values mean the queue grew because the agent was
saturated; low values with long waits mean the agent simply did not fetch.

**3. Ordering / triage** (:func:`ordering_profile`). Does the agent pick cheap
work first? We cannot know cost a priori, so we use realised ``service_sec`` as
the cost proxy and ask whether claim order correlates with it
(``triage_r`` = Spearman between claim rank and service time; negative = short
tasks claimed first = triage, ~0 = indifferent, positive = long-first).
``fifo_r`` is Spearman between arrival rank and claim rank: ~1.0 means the
agent takes whatever the pool offers in order, which for a pull-based pool is
the *default* and not evidence of a strategy. Both are reported because
"FIFO because the pool only ever offered one task" and "FIFO by choice" look
the same in the reward column and different here, once ``backlog`` is
considered: :func:`ordering_profile` also returns ``frac_time_backlog_ge2``,
the share of makespan during which the agent actually *had* a choice.

Caveat that applies to all of it: ``service_sec`` is wall-clock from claim to
terminal, so a task the agent claimed and then ignored for an hour looks
"expensive". That is the same limitation the pool's own metrics have; it is
called out rather than corrected, because there is no per-task attention signal
in any artifact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from aos_bench.traj_analysis.loader import TrialRecord

# Tolerance for "our integral agrees with the pool's mean_realized_concurrency".
# The pool and we both integrate the same step function, so agreement should be
# tight; 2% absorbs float accumulation over a few thousand transitions.
CONCURRENCY_TOLERANCE = 0.02

# A task-table status meaning the pool saw the task finish, either way.
TERMINAL_STATUSES = frozenset({"completed", "failed"})

# Pool MCP tool prefix; claiming a task requires calling ``todo_next``.
POOL_TOOL_PREFIX = "mcp__aos_pool__"


@dataclass
class ConcurrencyProfile:
    """Time-weighted shape of one trial's in-flight task count."""

    source: str = "none"  # curves | rebuilt_from_tasks | none
    makespan_sec: float | None = None
    # Start of the integration window: the first claim, matching the pool.
    window_start_sec: float | None = None
    time_weighted_mean: float | None = None
    reported_mean: float | None = None
    mean_disagreement: float | None = None
    peak: int | None = None
    reported_peak: int | None = None
    # Time-weighted mean within each third of the makespan.
    conc_q1: float | None = None
    conc_q2: float | None = None
    conc_q3: float | None = None
    ramp_ratio: float | None = None  # q3 / q1
    collapse_frac: float | None = None  # see module docstring; None if never ramped
    # Share of the window spent serial *with ready work still queued*. The
    # discriminating statistic: collapse_frac alone cannot tell "gave up" from
    # "ran out of work".
    starved_frac: float | None = None
    ever_reached_2: bool = False
    frac_time_idle: float | None = None  # concurrency == 0
    frac_time_serial: float | None = None  # concurrency <= 1
    n_transitions: int = 0
    shape: str = "unknown"  # ramp | hold | starve | drain | serial | idle-heavy | unknown


@dataclass
class QueueProfile:
    """Queueing view of one trial, from the pool's task table."""

    n_tasks: int = 0
    n_claimed: int = 0
    n_terminal: int = 0
    n_never_claimed: int = 0
    mean_wait_sec: float | None = None
    median_wait_sec: float | None = None
    p90_wait_sec: float | None = None
    max_wait_sec: float | None = None
    mean_service_sec: float | None = None
    median_service_sec: float | None = None
    mean_turnaround_sec: float | None = None
    # Share of total turnaround time that was spent waiting rather than served.
    wait_share_of_turnaround: float | None = None
    # Spearman(arrival rank, wait). None for burst (all arrivals coincide).
    wait_arrival_slope_r: float | None = None
    starves_late_arrivals: bool | None = None
    hol_blocked_frac: float | None = None
    # Tasks that arrived but were never claimed, and the wall time they sat.
    unclaimed_task_ids: list[str] = field(default_factory=list)
    unclaimed_idle_sec: float | None = None


@dataclass
class OrderingProfile:
    """Did the agent choose what to work on, and on what basis?"""

    n_claimed: int = 0
    fifo_r: float | None = None  # Spearman(arrival rank, claim rank)
    triage_r: float | None = None  # Spearman(claim rank, service_sec)
    reward_order_r: float | None = None  # Spearman(claim rank, reward)
    frac_time_backlog_ge2: float | None = None  # agent actually had a choice
    had_choice: bool | None = None
    verdict: str = "unknown"  # forced-fifo | fifo-by-choice | triage-short-first | long-first | mixed


@dataclass
class FetchProfile:
    """How hard the agent worked the pool's ``todo_next`` endpoint.

    This is the *mechanism* behind abandoned backlog, and it is visible only by
    joining the trajectory's tool histogram to the pool's task table. Claiming
    a task requires calling ``todo_next``; so an agent that left 28 of 30 tasks
    unclaimed did not fail to *finish* work, it failed to **ask for** it.

    ``polls_per_claim`` separates two very different styles at equal reward:
    kimi polls the pool 100-260 times for 20 tasks (a tight fetch loop, often
    blocking with ``timeout_sec``), while codex polls ~21-27 times for the same
    20 tasks (one poll per task plus a few). Both fully drain the stream. A
    ratio near 1.0 with tasks left over is the pathological case: the agent
    asked exactly as many times as it claimed and then stopped asking.
    """

    n_todo_next: int = 0
    n_pool_calls: int = 0
    n_claimed: int = 0
    n_tasks: int = 0
    polls_per_claim: float | None = None
    claimed_share: float | None = None
    # Mean reward over the tasks the agent *actually claimed*. The headline
    # reward divides by all tasks, so an unclaimed task is scored 0 and
    # claimed_share mechanically bounds reward from above -- meaning a
    # correlation between them is partly arithmetic. This field removes that
    # floor and answers the separate question: of the work it did take, how well
    # did it do? A low claimed_share with a *healthy* reward_on_claimed is a
    # pure work-acquisition failure; both being low is a solving failure.
    reward_on_claimed: float | None = None
    reward_all_tasks: float | None = None
    # True when work remained *and* the agent stopped polling: the pool always
    # had something to give, so the missing claims are a fetch decision.
    stopped_asking: bool = False


@dataclass
class TrialScheduling:
    trial: str
    job: str
    arm: str
    tier: str
    setting: str
    variant: str
    concurrency: ConcurrencyProfile = field(default_factory=ConcurrencyProfile)
    queue: QueueProfile = field(default_factory=QueueProfile)
    ordering: OrderingProfile = field(default_factory=OrderingProfile)
    fetch: FetchProfile = field(default_factory=FetchProfile)


# ---------------------------------------------------------------------------
# small stats helpers (stdlib only, by package policy)
# ---------------------------------------------------------------------------


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties (very common in burst arrival times) are fair."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman rho, or ``None`` when it is undefined.

    Undefined here means fewer than 3 pairs, or one side constant — which is
    the normal case for burst arrivals (all ``arrived_at_sec`` identical) and
    must not be reported as 0.0 "no correlation".
    """
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    if len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


# ---------------------------------------------------------------------------
# step-function machinery
# ---------------------------------------------------------------------------


def _clean_curve(raw: Any) -> list[tuple[float, float]]:
    """Coerce a ``curves.*`` payload into sorted ``(t, level)`` pairs."""
    if not isinstance(raw, list):
        return []
    points: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            points.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError):
            continue
    points.sort(key=lambda p: p[0])
    return points


def rebuild_concurrency(tasks: dict[str, Any]) -> list[tuple[float, float]]:
    """Rebuild the concurrency step function from claim/terminal stamps.

    Used when ``curves`` is missing. A task contributes +1 at
    ``claimed_at_sec`` and -1 at ``terminal_at_sec``; a claimed task with no
    terminal stamp (the trial was cut off mid-flight) stays in flight to the
    end, which is the truthful reading — it was still held.
    """
    events: list[tuple[float, int]] = []
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        claimed = task.get("claimed_at_sec")
        if claimed is None:
            continue
        try:
            events.append((float(claimed), +1))
        except (TypeError, ValueError):
            continue
        terminal = task.get("terminal_at_sec")
        if terminal is not None:
            try:
                events.append((float(terminal), -1))
            except (TypeError, ValueError):
                pass
    if not events:
        return []
    events.sort(key=lambda e: (e[0], -e[1]))
    points: list[tuple[float, float]] = []
    level = 0
    for t, delta in events:
        level += delta
        if points and points[-1][0] == t:
            points[-1] = (t, float(level))
        else:
            points.append((t, float(level)))
    return points


def _integrate(points: list[tuple[float, float]], start: float, end: float) -> float | None:
    """Time-weighted mean of a step function over ``[start, end]``.

    The level in force at time ``t`` is the level of the last point at or
    before ``t``; before the first point the level is 0 (nothing claimed yet).
    """
    if end <= start:
        return None
    total = 0.0
    level = 0.0
    cursor = start
    for t, value in points:
        if t >= end:
            break
        if t > cursor:
            total += level * (min(t, end) - cursor)
            cursor = t
        level = value
    if cursor < end:
        total += level * (end - cursor)
    return total / (end - start)


def _time_at_or_below(points: list[tuple[float, float]], end: float, threshold: float, start: float = 0.0) -> float:
    """Seconds in ``[start, end]`` where the step function is <= ``threshold``."""
    total = 0.0
    level = 0.0
    cursor = start
    for t, value in points:
        if t >= end:
            break
        if t > cursor:
            if level <= threshold:
                total += min(t, end) - cursor
            cursor = t
        level = value
    if cursor < end and level <= threshold:
        total += end - cursor
    return total


def _level_at(points: list[tuple[float, float]], t: float) -> float:
    """Step-function level in force at ``t`` (0 before the first point)."""
    level = 0.0
    for pt, value in points:
        if pt > t:
            break
        level = value
    return level


def _serial_with_backlog_frac(
    conc: list[tuple[float, float]],
    backlog: list[tuple[float, float]],
    start: float,
    end: float,
) -> float | None:
    """Share of ``[start, end]`` at concurrency <= 1 while backlog >= 1.

    Both inputs are step functions on the same clock, so we walk their merged
    transition points and accumulate the intervals where both conditions hold.
    Returns ``None`` when the backlog curve is unavailable — guessing that a
    missing backlog means "no queue" would turn every starving trial healthy.
    """
    if not conc or not backlog or end <= start:
        return None
    times = sorted(
        {start, end}
        | {t for t, _ in conc if start < t < end}
        | {t for t, _ in backlog if start < t < end}
    )
    total = 0.0
    for a, b in zip(times, times[1:]):
        if _level_at(conc, a) <= 1.0 and _level_at(backlog, a) >= 1.0:
            total += b - a
    return total / (end - start)


def _classify_shape(p: ConcurrencyProfile) -> str:
    """Name the curve's shape. Thresholds are deliberately coarse.

    They exist to make 300 trials skimmable, not to be a statistical test; the
    underlying q1/q2/q3, ramp_ratio and starved_frac are always emitted so a
    reader can disagree with the label.

    ``starve`` outranks everything else because it is the only shape that is a
    *problem*: the agent held one task while the pool had more ready. ``drain``
    is the benign look-alike — concurrency fell because the work ran out.
    """
    if p.time_weighted_mean is None:
        return "unknown"
    if p.frac_time_idle is not None and p.frac_time_idle > 0.5:
        return "idle-heavy"
    if p.starved_frac is not None and p.starved_frac >= 0.3:
        return "starve"
    if not p.ever_reached_2:
        return "serial"
    if p.ramp_ratio is None:
        return "unknown"
    if p.ramp_ratio >= 1.25:
        return "ramp"
    if p.ramp_ratio <= 0.75:
        return "drain"
    return "hold"


def concurrency_profile(record: TrialRecord) -> ConcurrencyProfile:
    out = ConcurrencyProfile()
    sched = record.scheduling or {}
    agg = sched.get("aggregates") if isinstance(sched.get("aggregates"), dict) else {}
    tasks = sched.get("tasks") if isinstance(sched.get("tasks"), dict) else {}
    curves = sched.get("curves") if isinstance(sched.get("curves"), dict) else {}

    points = _clean_curve(curves.get("concurrency"))
    if points:
        out.source = "curves"
    else:
        points = rebuild_concurrency(tasks)
        out.source = "rebuilt_from_tasks" if points else "none"
    if not points:
        return out

    out.n_transitions = len(points)
    out.peak = int(max(v for _, v in points))

    try:
        out.reported_mean = float(agg.get("mean_realized_concurrency"))
    except (TypeError, ValueError):
        out.reported_mean = None
    try:
        out.reported_peak = int(agg.get("peak_realized_concurrency"))
    except (TypeError, ValueError):
        out.reported_peak = None

    makespan: float | None
    try:
        makespan = float(agg.get("makespan_sec"))
    except (TypeError, ValueError):
        makespan = None
    if not makespan or makespan <= 0:
        # Fall back to the last transition. Slightly understates the tail but
        # is the only bound available when the aggregate was not written.
        makespan = points[-1][0]
    out.makespan_sec = makespan

    # The pool integrates from the first claim, not from t=0 -- verified to
    # reproduce its mean_realized_concurrency exactly on 268/268 trials. Using
    # 0 instead inflates the pre-claim dead zone into the average and drifts by
    # up to 4.6% on trials with a slow start.
    start = points[0][0]
    out.window_start_sec = start
    span = makespan - start
    if span <= 0:
        return out

    out.time_weighted_mean = _integrate(points, start, makespan)
    if out.time_weighted_mean is not None and out.reported_mean:
        out.mean_disagreement = out.time_weighted_mean - out.reported_mean

    third = span / 3.0
    out.conc_q1 = _integrate(points, start, start + third)
    out.conc_q2 = _integrate(points, start + third, start + 2 * third)
    out.conc_q3 = _integrate(points, start + 2 * third, makespan)
    if out.conc_q1 and out.conc_q3 is not None and out.conc_q1 > 0:
        out.ramp_ratio = out.conc_q3 / out.conc_q1

    out.frac_time_idle = _time_at_or_below(points, makespan, 0.0, start=start) / span
    out.frac_time_serial = _time_at_or_below(points, makespan, 1.0, start=start) / span

    # Collapse is only meaningful after the agent demonstrated it *could* hold
    # two tasks. Before that, low concurrency is "never started", not "gave up".
    first_two = next((t for t, v in points if v >= 2), None)
    out.ever_reached_2 = first_two is not None
    if first_two is not None and makespan > first_two:
        after = _time_at_or_below(points, makespan, 1.0, start=first_two)
        out.collapse_frac = after / (makespan - first_two)

    # The statistic that actually separates "gave up" from "ran out of work".
    out.starved_frac = _serial_with_backlog_frac(
        points, _clean_curve(curves.get("backlog")), start, makespan
    )

    out.shape = _classify_shape(out)
    return out


def queue_profile(record: TrialRecord) -> QueueProfile:
    out = QueueProfile()
    sched = record.scheduling or {}
    tasks = sched.get("tasks") if isinstance(sched.get("tasks"), dict) else {}
    if not tasks:
        return out

    agg = sched.get("aggregates") if isinstance(sched.get("aggregates"), dict) else {}
    try:
        makespan = float(agg.get("makespan_sec"))
    except (TypeError, ValueError):
        makespan = 0.0

    def _num(task: dict[str, Any], key: str) -> float | None:
        value = task.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    arrivals: list[float] = []
    waits: list[float] = []
    services: list[float] = []
    turnarounds: list[float] = []
    unclaimed: list[str] = []
    unclaimed_idle = 0.0

    out.n_tasks = len(tasks)
    for task_id, task in tasks.items():
        if not isinstance(task, dict):
            continue
        claimed = _num(task, "claimed_at_sec")
        arrived = _num(task, "arrived_at_sec")
        if claimed is None:
            unclaimed.append(str(task_id))
            if arrived is not None and makespan > arrived:
                unclaimed_idle += makespan - arrived
            continue
        out.n_claimed += 1
        if task.get("terminal_at_sec") is not None:
            out.n_terminal += 1
        wait = _num(task, "wait_sec")
        service = _num(task, "service_sec")
        turnaround = _num(task, "turnaround_sec")
        if wait is not None and arrived is not None:
            arrivals.append(arrived)
            waits.append(wait)
        if service is not None:
            services.append(service)
        if turnaround is not None:
            turnarounds.append(turnaround)

    out.n_never_claimed = len(unclaimed)
    out.unclaimed_task_ids = sorted(unclaimed)
    out.unclaimed_idle_sec = unclaimed_idle if unclaimed else None

    if waits:
        out.mean_wait_sec = sum(waits) / len(waits)
        out.median_wait_sec = _median(waits)
        out.p90_wait_sec = _percentile(waits, 0.90)
        out.max_wait_sec = max(waits)
    if services:
        out.mean_service_sec = sum(services) / len(services)
        out.median_service_sec = _median(services)
    if turnarounds:
        out.mean_turnaround_sec = sum(turnarounds) / len(turnarounds)
        total_turn = sum(turnarounds)
        if total_turn > 0 and waits:
            out.wait_share_of_turnaround = sum(waits) / total_turn

    # Burst streams have identical arrival times, so spearman() returns None
    # (constant input) rather than an invented zero -- see its docstring.
    out.wait_arrival_slope_r = spearman(arrivals, waits)
    if out.wait_arrival_slope_r is not None:
        out.starves_late_arrivals = out.wait_arrival_slope_r >= 0.5

    out.hol_blocked_frac = _hol_blocked_frac(record, tasks, makespan)
    return out


def _hol_blocked_frac(
    record: TrialRecord, tasks: dict[str, Any], makespan: float
) -> float | None:
    """Share of accumulated wait time spent while the agent was at peak load.

    "Peak load" is this trial's own maximum observed concurrency, not a global
    constant: the agents have no declared parallelism limit, so the only
    evidence of their ceiling is the highest level they ever reached. Waits
    accrued at that ceiling are plausibly *capacity* blocking; waits accrued
    below it are the agent declining to pull more work.

    Returns ``None`` when the peak is below 2. An agent that never held two
    tasks has no capacity to be blocked by: every second of queue time would
    count as "blocked at peak" and the metric would read 1.00 for exactly the
    trials that are the *opposite* of saturated. That is the glm heavy-plus
    signature (peak 1, 28/30 unclaimed) and reporting it as fully
    head-of-line-blocked would invert the finding.
    """
    conc = concurrency_points(record)
    if not conc or makespan <= 0:
        return None
    peak = max(v for _, v in conc)
    if peak < 2:
        return None

    blocked = 0.0
    total = 0.0
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        try:
            arrived = float(task["arrived_at_sec"])
            claimed = float(task["claimed_at_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if claimed <= arrived:
            continue
        total += claimed - arrived
        blocked += _time_at_least(conc, arrived, claimed, peak)
    if total <= 0:
        return None
    return blocked / total


def _time_at_least(points: list[tuple[float, float]], start: float, end: float, threshold: float) -> float:
    """Seconds in ``[start, end]`` where the step function is >= ``threshold``."""
    if end <= start:
        return 0.0
    total = 0.0
    level = 0.0
    cursor = start
    for t, value in points:
        if t <= start:
            level = value
            continue
        if t >= end:
            break
        if level >= threshold:
            total += t - cursor
        cursor = t
        level = value
    if cursor < end and level >= threshold:
        total += end - cursor
    return total


def concurrency_points(record: TrialRecord) -> list[tuple[float, float]]:
    """The concurrency step function, from ``curves`` or rebuilt."""
    sched = record.scheduling or {}
    curves = sched.get("curves") if isinstance(sched.get("curves"), dict) else {}
    points = _clean_curve(curves.get("concurrency"))
    if points:
        return points
    tasks = sched.get("tasks") if isinstance(sched.get("tasks"), dict) else {}
    return rebuild_concurrency(tasks)


def ordering_profile(record: TrialRecord) -> OrderingProfile:
    out = OrderingProfile()
    sched = record.scheduling or {}
    tasks = sched.get("tasks") if isinstance(sched.get("tasks"), dict) else {}
    agg = sched.get("aggregates") if isinstance(sched.get("aggregates"), dict) else {}
    if not tasks:
        return out

    arrivals: list[float] = []
    claims: list[float] = []
    services: list[float] = []
    rewards: list[float] = []
    claim_for_service: list[float] = []
    claim_for_reward: list[float] = []

    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        try:
            claimed = float(task["claimed_at_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        out.n_claimed += 1
        try:
            arrivals.append(float(task["arrived_at_sec"]))
            claims.append(claimed)
        except (KeyError, TypeError, ValueError):
            pass
        service = task.get("service_sec")
        if service is not None:
            try:
                services.append(float(service))
                claim_for_service.append(claimed)
            except (TypeError, ValueError):
                pass
        reward = task.get("reward")
        if reward is not None:
            try:
                rewards.append(float(reward))
                claim_for_reward.append(claimed)
            except (TypeError, ValueError):
                pass

    out.fifo_r = spearman(arrivals, claims)
    out.triage_r = spearman(claim_for_service, services)
    out.reward_order_r = spearman(claim_for_reward, rewards)

    # Did the agent ever have more than one ready task to choose between? If
    # backlog never reached 2, "the agent worked FIFO" is a statement about the
    # pool's arrival schedule, not about the agent.
    curves = sched.get("curves") if isinstance(sched.get("curves"), dict) else {}
    backlog = _clean_curve(curves.get("backlog"))
    try:
        makespan = float(agg.get("makespan_sec"))
    except (TypeError, ValueError):
        makespan = 0.0
    if backlog and makespan > 0:
        below = _time_at_or_below(backlog, makespan, 1.0)
        out.frac_time_backlog_ge2 = 1.0 - below / makespan
        out.had_choice = out.frac_time_backlog_ge2 > 0.05

    if out.had_choice is False:
        out.verdict = "forced-fifo"
    elif out.triage_r is None:
        out.verdict = "unknown"
    elif out.triage_r <= -0.4:
        out.verdict = "triage-short-first"
    elif out.triage_r >= 0.4:
        out.verdict = "long-first"
    elif out.fifo_r is not None and out.fifo_r >= 0.9:
        out.verdict = "fifo-by-choice"
    else:
        out.verdict = "mixed"
    return out


def fetch_profile(record: TrialRecord) -> FetchProfile:
    """Join the trajectory's pool-tool usage to the pool's claim record."""
    out = FetchProfile()
    for _step, call in record.tool_calls():
        name = str(call.get("function_name") or "")
        if not name.startswith(POOL_TOOL_PREFIX):
            continue
        out.n_pool_calls += 1
        if name == f"{POOL_TOOL_PREFIX}todo_next":
            out.n_todo_next += 1

    sched = record.scheduling or {}
    tasks = sched.get("tasks") if isinstance(sched.get("tasks"), dict) else {}
    out.n_tasks = len(tasks)
    out.n_claimed = sum(
        1 for t in tasks.values() if isinstance(t, dict) and t.get("claimed_at_sec") is not None
    )
    if out.n_claimed:
        out.polls_per_claim = out.n_todo_next / out.n_claimed
    rewards_claimed: list[float] = []
    rewards_all: list[float] = []
    for t in tasks.values():
        if not isinstance(t, dict):
            continue
        try:
            value = float(t.get("reward"))
        except (TypeError, ValueError):
            continue
        rewards_all.append(value)
        if t.get("claimed_at_sec") is not None:
            rewards_claimed.append(value)
    if rewards_claimed:
        out.reward_on_claimed = sum(rewards_claimed) / len(rewards_claimed)
    if rewards_all:
        out.reward_all_tasks = sum(rewards_all) / len(rewards_all)

    if out.n_tasks:
        out.claimed_share = out.n_claimed / out.n_tasks
        # Every arriving task is claimable, so leaving any unclaimed while
        # having stopped polling is a fetch decision, not a capacity limit.
        out.stopped_asking = out.n_claimed < out.n_tasks
    return out


def compute(record: TrialRecord) -> TrialScheduling:
    return TrialScheduling(
        trial=record.trial_name,
        job=record.job,
        arm=record.arm,
        tier=record.tier,
        setting=record.setting,
        variant=record.variant,
        concurrency=concurrency_profile(record),
        queue=queue_profile(record),
        ordering=ordering_profile(record),
        fetch=fetch_profile(record),
    )
