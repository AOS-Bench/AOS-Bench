"""Tests for the low-tail failure taxonomy.

The load-bearing contract is the ambiguity rule: ``n_unclaimed > 0`` with an
empty ``stream_done_reason`` means *infra cut-off* when the trajectory is
short and *the agent chose to stop* when it is long. Getting that backwards
either discards a real behavioural finding or publishes an infra failure as a
model result, so both directions are pinned here.
"""

from __future__ import annotations

from pathlib import Path

from aos_bench.traj_analysis import failures, health, loader, performance
from aos_bench.traj_analysis.tests import fixtures


def _verdict(tmp_path: Path, **kwargs) -> failures.FailureVerdict:
    trial = kwargs.pop("trial", "s1__X")
    job = kwargs.pop("job", "main-glm-stream-medium")
    trial_dir = fixtures.write_trial(tmp_path, job, trial, **kwargs)
    record = loader.load_trial(trial_dir, job=job, arm="glm", tier="medium", setting="stream")
    return failures.classify_failure(
        record, health.classify(record), performance.compute(record)
    )


def _steps(n: int) -> list[dict]:
    return [
        fixtures.step(i, f"2026-09-19T03:{i // 60:02d}:{i % 60:02d}.000000Z", tool="Bash")
        for i in range(n)
    ]


def _sched(*, n_tasks: int, n_claimed: int, done_reason: str | None) -> dict:
    tasks = {}
    for i in range(n_tasks):
        claimed = float(i) if i < n_claimed else None
        tasks[f"task-{i}"] = fixtures.task(
            arrived=0.0,
            claimed=claimed,
            terminal=(claimed + 10.0) if claimed is not None else None,
            status="completed" if claimed is not None else "ready",
        )
    return fixtures.stream_scheduling(
        tasks=tasks,
        makespan_sec=1000.0,
        concurrency=[[0.0, 1], [1000.0, 0]],
        backlog=[[0.0, max(0, n_tasks - n_claimed)]],
        stream_done_reason=done_reason,
        n_unclaimed=n_tasks - n_claimed,
    )


def test_long_trajectory_leaving_work_is_the_agents_choice(tmp_path: Path) -> None:
    """The glm heavy-plus signature: hundreds of steps, backlog abandoned."""
    verdict = _verdict(
        tmp_path,
        steps=_steps(242),
        rewards={f"task-{i}": 0.0 for i in range(10)},
        scheduling=_sched(n_tasks=10, n_claimed=2, done_reason=None),
    )
    assert verdict.bucket == "abandoned_backlog"
    assert verdict.is_measurement_artifact is False
    assert verdict.cutoff_vs_choice == "choice"


def test_short_trajectory_leaving_work_is_an_infra_cutoff(tmp_path: Path) -> None:
    """Same unclaimed backlog, but the agent barely ran: discard, not a result."""
    verdict = _verdict(
        tmp_path,
        steps=_steps(4),
        rewards={f"task-{i}": 0.0 for i in range(10)},
        scheduling=_sched(n_tasks=10, n_claimed=2, done_reason=None),
    )
    assert verdict.bucket == "never_claimed_cutoff"
    assert verdict.is_measurement_artifact is True
    assert verdict.cutoff_vs_choice == "cutoff"


def test_claimed_nothing_across_a_long_run_is_a_behavioural_result(tmp_path: Path) -> None:
    verdict = _verdict(
        tmp_path,
        steps=_steps(200),
        rewards={f"task-{i}": 0.0 for i in range(10)},
        scheduling=_sched(n_tasks=10, n_claimed=0, done_reason=None),
    )
    assert verdict.bucket == "never_claimed_by_choice"
    assert verdict.is_measurement_artifact is False


def test_claimed_everything_and_scored_low_is_capability(tmp_path: Path) -> None:
    verdict = _verdict(
        tmp_path,
        steps=_steps(150),
        rewards={f"task-{i}": 0.0 for i in range(10)},
        scheduling=_sched(n_tasks=10, n_claimed=10, done_reason="all_completed"),
    )
    assert verdict.bucket == "attempted_and_failed"
    assert verdict.is_measurement_artifact is False


def test_throttling_outranks_abandoned_backlog(tmp_path: Path) -> None:
    """A throttled trial that also abandoned work is reported as throttled.

    The throttle is the upstream cause; labelling the symptom would hide the
    fact that the number is not a model result at all.
    """
    verdict = _verdict(
        tmp_path,
        steps=_steps(242),
        harness="claude-code",
        transcript="... 触发平台限流 bucket=project_rpm,limit=100 ...",
        rewards={f"task-{i}": 0.0 for i in range(10)},
        scheduling=_sched(n_tasks=10, n_claimed=2, done_reason=None),
    )
    assert verdict.bucket == "throttled"
    assert verdict.is_measurement_artifact is True


def test_infra_error_outranks_everything(tmp_path: Path) -> None:
    verdict = _verdict(
        tmp_path,
        steps=_steps(2),
        exception="ImagePullBackOff while starting sandbox",
        rewards={"task-0": 0.0},
    )
    assert verdict.bucket == "infra_error"
    assert verdict.is_measurement_artifact is True


def test_in_flight_trial_is_not_a_failure(tmp_path: Path) -> None:
    verdict = _verdict(tmp_path, steps=_steps(10), write_result=False)
    assert verdict.bucket == "in_flight"


def test_missing_scheduling_metrics_is_flagged_not_guessed(tmp_path: Path) -> None:
    verdict = _verdict(tmp_path, steps=_steps(100), rewards={"task-0": 0.0})
    assert verdict.bucket == "no_scheduling_data"
    assert verdict.is_measurement_artifact is True


def test_summarise_separates_measurement_from_capability(tmp_path: Path) -> None:
    verdicts = [
        _verdict(
            tmp_path,
            trial="s1__a",
            steps=_steps(242),
            rewards={f"t{i}": 0.0 for i in range(10)},
            scheduling=_sched(n_tasks=10, n_claimed=2, done_reason=None),
        ),
        _verdict(
            tmp_path,
            trial="s2__b",
            steps=_steps(4),
            rewards={f"t{i}": 0.0 for i in range(10)},
            scheduling=_sched(n_tasks=10, n_claimed=2, done_reason=None),
        ),
        _verdict(
            tmp_path,
            trial="s3__c",
            steps=_steps(100),
            rewards={f"t{i}": 1.0 for i in range(10)},
            scheduling=_sched(n_tasks=10, n_claimed=10, done_reason="all_completed"),
        ),
    ]
    out = failures.summarise(verdicts)
    # Only the two zero-scoring trials are in the low tail.
    assert out["n_low"] == 2
    assert out["n_measurement_artifact"] == 1
    assert out["n_capability"] == 1
    assert out["measurement_share_of_low_tail"] == 0.5


def test_summarise_counts_missing_reward_files_separately(tmp_path: Path) -> None:
    """'The verifier never ran' must not be folded into 'scored zero'."""
    verdicts = [_verdict(tmp_path, steps=_steps(10), write_result=False)]
    out = failures.summarise(verdicts)
    assert out["n_low"] == 0
    assert out["n_no_reward_file"] == 1
    assert out["no_reward_buckets"] == {"in_flight": 1}
