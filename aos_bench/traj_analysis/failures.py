"""Why did a low-scoring trial score low? Measurement, or capability?

A reward near zero has at least five mutually-exclusive causes and they carry
completely different meaning for the benchmark:

``throttled``
    The gateway rate-limited the agent. Detected only from the transcript --
    ``result.json`` is well formed and ``n_errors`` is 0. Pure measurement
    artifact; the score says nothing about the model.

``infra_error``
    The sandbox never came up, the image never pulled, the harness exited
    non-zero. ``exception.txt`` exists. Measurement artifact.

``never_claimed``
    The pool offered work and the agent claimed **nothing**. With a stunted
    trajectory this is an infra cut-off; with a long one it is a behavioural
    result (see ``cutoff_vs_choice`` below).

``abandoned_backlog``
    The agent claimed some tasks and then stopped pulling, leaving tasks
    unclaimed at teardown. This is the glm heavy-plus signature: 28-29 of 30
    unclaimed across 242/269/77-step trajectories with zero throttling.

``attempted_and_failed``
    The agent claimed (nearly) everything and the verifier scored it low. This
    is the only bucket that is a capability result.

The ambiguous case is ``n_unclaimed > 0`` with an empty ``stream_done_reason``,
where the pool did not record why the stream ended. The disambiguator is the
trajectory length, and it is a real one rather than a heuristic of convenience:
an infra cut-off kills the agent process, so the trajectory stops abruptly and
short; an agent that chose to stop produces hundreds of steps and then declines
to call ``todo_next`` again. :data:`CUTOFF_MAX_STEPS` draws that line, and the
field ``cutoff_vs_choice`` records which side each trial landed on **together
with its step count**, so the judgement is auditable rather than asserted.

The output is deliberately a classification per trial plus the evidence, not a
single percentage. "How much of the low tail is measurement" is then a sum over
buckets the reader can recompute with a different line if they disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aos_bench.traj_analysis.health import TrialHealth
from aos_bench.traj_analysis.loader import TrialRecord
from aos_bench.traj_analysis.performance import TrialPerformance

# Reward at or below this counts as "low tail" and gets classified.
LOW_REWARD_THRESHOLD = 0.1

# A trajectory shorter than this, with unclaimed work left, reads as the agent
# being killed rather than stopping. Healthy observed runs are 34-269 steps;
# the observed infra cut-offs are all well under 30.
CUTOFF_MAX_STEPS = 30

# Below this share of tasks claimed, the trial is "abandoned backlog" rather
# than "attempted and failed": the score mostly measures work never started.
CLAIMED_SHARE_ABANDONED = 0.8

MEASUREMENT_BUCKETS = frozenset({"throttled", "infra_error", "never_claimed_cutoff", "unparseable"})
CAPABILITY_BUCKETS = frozenset({"attempted_and_failed", "abandoned_backlog", "never_claimed_by_choice"})


@dataclass
class FailureVerdict:
    trial: str
    job: str
    arm: str
    tier: str
    setting: str
    variant: str
    mean_reward: float | None
    bucket: str
    is_measurement_artifact: bool
    n_tasks: int | None = None
    n_claimed: int | None = None
    n_unclaimed: int | None = None
    claimed_share: float | None = None
    n_steps: int | None = None
    stream_done_reason: str | None = None
    cutoff_vs_choice: str | None = None
    throttle_labels: list[str] = field(default_factory=list)
    infra_error_classes: list[str] = field(default_factory=list)
    evidence: str = ""


def classify_failure(
    record: TrialRecord,
    health: TrialHealth,
    perf: TrialPerformance,
) -> FailureVerdict:
    """Bucket one trial. Called for every trial; only low scorers are reported.

    Precedence matters and mirrors ``health.classify``: a throttled trial that
    also abandoned its backlog is reported as throttled, because the throttle
    is the upstream cause and quoting the downstream symptom would hide it.
    """
    sched = record.scheduling or {}
    tasks = sched.get("tasks") if isinstance(sched.get("tasks"), dict) else {}
    n_claimed = sum(
        1 for t in tasks.values() if isinstance(t, dict) and t.get("claimed_at_sec") is not None
    )
    n_tasks = len(tasks) if tasks else perf.n_tasks
    claimed_share = n_claimed / n_tasks if n_tasks else None
    n_steps = health.n_steps

    out = FailureVerdict(
        trial=record.trial_name,
        job=record.job,
        arm=record.arm,
        tier=record.tier,
        setting=record.setting,
        variant=record.variant,
        mean_reward=perf.mean_reward,
        bucket="unknown",
        is_measurement_artifact=False,
        n_tasks=n_tasks,
        n_claimed=n_claimed if tasks else None,
        n_unclaimed=perf.n_unclaimed,
        claimed_share=claimed_share,
        n_steps=n_steps,
        stream_done_reason=perf.stream_done_reason or None,
        throttle_labels=list(health.throttle_labels),
        infra_error_classes=list(health.infra_error_classes),
    )

    if health.status == "in_flight":
        out.bucket = "in_flight"
        out.is_measurement_artifact = True
        out.evidence = "no result.json and no exception.txt; still running"
        return out
    if health.status == "infra_error":
        out.bucket = "infra_error"
        out.is_measurement_artifact = True
        out.evidence = f"exception.txt: {', '.join(health.infra_error_classes)}"
        return out
    if health.throttled:
        out.bucket = "throttled"
        out.is_measurement_artifact = True
        out.evidence = (
            f"{health.throttle_hits} transcript hits for "
            f"{', '.join(health.throttle_labels)}"
        )
        return out
    if health.status == "unparseable":
        out.bucket = "unparseable"
        out.is_measurement_artifact = True
        out.evidence = "finished but trajectory.json unreadable"
        return out

    if not tasks:
        out.bucket = "no_scheduling_data"
        out.is_measurement_artifact = True
        out.evidence = "verifier/scheduling_metrics.json absent or empty"
        return out

    # Short trajectory + work left on the table => killed, not a decision.
    short = n_steps is not None and n_steps <= CUTOFF_MAX_STEPS
    left_work = bool(perf.n_unclaimed) or (claimed_share is not None and claimed_share < 1.0)
    if left_work:
        out.cutoff_vs_choice = "cutoff" if short else "choice"

    if n_claimed == 0:
        if short:
            out.bucket = "never_claimed_cutoff"
            out.is_measurement_artifact = True
            out.evidence = (
                f"claimed 0/{n_tasks} tasks with only {n_steps} steps; "
                "trajectory ends abruptly => infra cut-off, discard"
            )
        else:
            out.bucket = "never_claimed_by_choice"
            out.evidence = (
                f"claimed 0/{n_tasks} tasks across {n_steps} steps with no throttling; "
                "the agent ran but never pulled work - this IS the measurement"
            )
        return out

    if claimed_share is not None and claimed_share < CLAIMED_SHARE_ABANDONED:
        if short:
            out.bucket = "never_claimed_cutoff"
            out.is_measurement_artifact = True
            out.evidence = (
                f"claimed {n_claimed}/{n_tasks} in only {n_steps} steps => cut short"
            )
        else:
            out.bucket = "abandoned_backlog"
            out.evidence = (
                f"claimed {n_claimed}/{n_tasks} tasks across {n_steps} steps, "
                f"stream_done_reason={out.stream_done_reason!r}, no throttling; "
                "the agent stopped fetching while work remained"
            )
        return out

    out.bucket = "attempted_and_failed"
    out.evidence = (
        f"claimed {n_claimed}/{n_tasks} tasks across {n_steps} steps and the "
        "verifier scored them low - this is a capability result"
    )
    return out


def summarise(verdicts: list[FailureVerdict], *, threshold: float = LOW_REWARD_THRESHOLD) -> dict[str, Any]:
    """Aggregate the low tail into measurement vs capability.

    Trials with no reward at all are counted separately from trials scoring
    zero: "the verifier never ran" and "the agent solved nothing" are different
    facts and collapsing them inflates whichever side the reader expects.
    """
    low = [v for v in verdicts if v.mean_reward is not None and v.mean_reward <= threshold]
    no_reward = [v for v in verdicts if v.mean_reward is None]

    buckets: dict[str, int] = {}
    for v in low:
        buckets[v.bucket] = buckets.get(v.bucket, 0) + 1
    n_measurement = sum(1 for v in low if v.is_measurement_artifact)

    by_arm: dict[str, dict[str, int]] = {}
    for v in low:
        row = by_arm.setdefault(f"{v.arm}-{v.setting}-{v.tier}" + (f"-{v.variant}" if v.variant else ""), {})
        row[v.bucket] = row.get(v.bucket, 0) + 1

    return {
        "threshold": threshold,
        "n_trials_scored": sum(1 for v in verdicts if v.mean_reward is not None),
        "n_low": len(low),
        "n_no_reward_file": len(no_reward),
        "no_reward_buckets": _count([v.bucket for v in no_reward]),
        "buckets": dict(sorted(buckets.items(), key=lambda kv: -kv[1])),
        "n_measurement_artifact": n_measurement,
        "n_capability": len(low) - n_measurement,
        "measurement_share_of_low_tail": n_measurement / len(low) if low else None,
        "by_cell": by_arm,
        "ambiguous_unclaimed_resolved": [
            {
                "trial": f"{v.job}/{v.trial}",
                "n_steps": v.n_steps,
                "n_claimed": v.n_claimed,
                "n_tasks": v.n_tasks,
                "verdict": v.cutoff_vs_choice,
                "stream_done_reason": v.stream_done_reason,
            }
            for v in verdicts
            if v.cutoff_vs_choice is not None and not v.stream_done_reason
        ],
    }


def _count(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
