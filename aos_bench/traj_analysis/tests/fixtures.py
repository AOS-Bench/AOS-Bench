"""Synthetic trial trees for the traj_analysis tests.

Deliberately hand-built rather than copied from the jobs directory: the tests must
pin the *contract* (field names, the -1 sentinel, the two timestamp formats,
the exact throttle phrases) so a schema drift in the real export shows up as a
failing test rather than as a silently wrong report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CGROUP_HEADER = (
    "sample\tmemory_current\tmemory_max\tmemory_peak\tswap_current\tswap_max\tmem_low\t"
    "mem_high\tmem_max\tmem_oom\tmem_oom_kill\tmem_oom_group_kill\tcpu_usage_usec\t"
    "cpu_user_usec\tcpu_system_usec\tcpu_nr_periods\tcpu_nr_throttled\tcpu_throttled_usec\t"
    "cpu_quota_usec\tcpu_period_usec\tio_rbytes\tio_wbytes\tio_rios\tio_wios\tpids_current\t"
    "pids_max\tpids_events_max"
)


def cgroup_row(
    sample: int,
    *,
    memory_current: int,
    memory_peak: int,
    cpu_usage_usec: int,
    pids_current: int,
    cpu_quota_usec: int = 400000,
    memory_max: int = 8589934592,
) -> str:
    """One TSV row with the -1 sentinels a sandbox without cpu.stat writes in the cpu.stat columns.

    ``cpu_quota_usec`` and ``memory_max`` are parameters, not constants: they
    are how a trial declares its sandbox shape, and the 2c4g probe writes
    ``200000`` / ``4 GiB`` where the main grid writes ``400000`` / ``8 GiB``.
    ``-1`` is accepted for either, to exercise the sentinel path.
    """
    cells = [
        str(sample),
        str(memory_current),
        str(memory_max),
        str(memory_peak),
        "-1",
        "-1",
        "-1",
        "-1",
        "-1",
        "0",
        "0",
        "-1",
        str(cpu_usage_usec),
        "-1",
        "-1",
        "-1",  # cpu_nr_periods: unavailable without cpu.stat
        "-1",  # cpu_nr_throttled
        "-1",  # cpu_throttled_usec
        str(cpu_quota_usec),
        "100000",
        "-1",
        "-1",
        "-1",
        "-1",
        str(pids_current),
        "-1",
        "-1",
    ]
    return "\t".join(cells)


def step(
    step_id: int,
    timestamp: str,
    *,
    tool: str | None = None,
    arguments: Any = None,
    source: str = "agent",
    observation: str | None = None,
) -> dict[str, Any]:
    calls = []
    if tool is not None:
        calls.append(
            {
                "tool_call_id": f"call_{step_id}",
                "function_name": tool,
                "arguments": arguments if arguments is not None else {},
            }
        )
    out: dict[str, Any] = {
        "step_id": step_id,
        "timestamp": timestamp,
        "source": source,
        "model_name": "test-model",
        "message": f"step {step_id}",
        "tool_calls": calls,
        "extra": {},
    }
    if observation is not None:
        out["observation"] = {"results": [{"source_call_id": f"call_{step_id}", "content": observation}]}
    return out


def write_trial(
    root: Path,
    job: str,
    trial: str,
    *,
    harness: str = "codex",
    steps: list[dict[str, Any]] | None = None,
    rewards: dict[str, float] | None = None,
    transcript: str | None = None,
    stderr_transcript: str | None = None,
    exception: str | None = None,
    write_result: bool = True,
    cgroup_rows: list[str] | None = None,
    scheduling: dict[str, Any] | None = None,
    final_metrics: dict[str, Any] | None = None,
    corrupt_trajectory: bool = False,
) -> Path:
    trial_dir = root / job / trial
    (trial_dir / "agent").mkdir(parents=True, exist_ok=True)
    (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)

    if corrupt_trajectory:
        (trial_dir / "agent/trajectory.json").write_text('{"steps": [', encoding="utf-8")
    elif steps is not None:
        (trial_dir / "agent/trajectory.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "session_id": "sess",
                    "agent": {"name": harness, "version": "0", "model_name": "test-model"},
                    "steps": steps,
                    "final_metrics": final_metrics
                    or {
                        "total_prompt_tokens": 1000,
                        "total_completion_tokens": 100,
                        "total_cached_tokens": 800,
                        "total_steps": len(steps),
                        "extra": {"reasoning_output_tokens": 50, "total_tokens": 1100},
                    },
                }
            ),
            encoding="utf-8",
        )

    if transcript is not None:
        (trial_dir / f"agent/{harness}.txt").write_text(transcript, encoding="utf-8")
    if stderr_transcript is not None:
        (trial_dir / f"agent/{harness}.stderr.txt").write_text(stderr_transcript, encoding="utf-8")

    if cgroup_rows is not None:
        (trial_dir / "agent/.harbor-cgroup-resources.tsv").write_text(
            CGROUP_HEADER + "\n" + "\n".join(cgroup_rows) + "\n", encoding="utf-8"
        )

    if rewards is not None:
        (trial_dir / "verifier/reward.json").write_text(json.dumps(rewards), encoding="utf-8")

    if scheduling is not None:
        (trial_dir / "verifier/scheduling_metrics.json").write_text(
            json.dumps(scheduling), encoding="utf-8"
        )

    if exception is not None:
        (trial_dir / "exception.txt").write_text(exception, encoding="utf-8")

    if write_result:
        (trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "task_name": trial.split("__")[0],
                    "trial_name": trial,
                    "agent_info": {"name": harness, "version": "0", "model_info": None},
                    # Mirrors the real harbor schema: the agent wall-clock budget
                    # lives here, and the scarcity analysis reads it per trial
                    # rather than assuming a constant.
                    "config": {
                        "trials_dir": f"jobs/{job}",
                        "timeout_multiplier": 1.0,
                        "agent_timeout_multiplier": None,
                        "agent": {
                            "name": harness,
                            "model_name": "test-model",
                            "override_timeout_sec": 43200.0,
                            "max_timeout_sec": 43200.0,
                        },
                    },
                    "verifier_result": {"rewards": rewards or {}},
                    "exception_info": None,
                    "started_at": "2026-09-19T03:00:00.000000Z",
                    "finished_at": "2026-09-19T03:20:00.000000Z",
                    "environment_setup": {
                        "started_at": "2026-09-19T03:00:10.000000Z",
                        "finished_at": "2026-09-19T03:02:10.000000Z",
                    },
                    "agent_setup": {
                        "started_at": "2026-09-19T03:02:10.000000Z",
                        "finished_at": "2026-09-19T03:03:10.000000Z",
                    },
                    "agent_execution": {
                        "started_at": "2026-09-19T03:03:10.000000Z",
                        "finished_at": "2026-09-19T03:18:10.000000Z",
                    },
                    "verifier": {
                        "started_at": "2026-09-19T03:18:20.000000Z",
                        "finished_at": "2026-09-19T03:19:50.000000Z",
                    },
                }
            ),
            encoding="utf-8",
        )
    return trial_dir


def scheduling_metrics(
    *,
    n_tasks: int,
    n_passed: int,
    makespan_sec: float,
    throughput: float,
    n_terminal: int | None = None,
    n_unclaimed: int = 0,
) -> dict[str, Any]:
    terminal = n_tasks if n_terminal is None else n_terminal
    tasks = {}
    for i in range(n_tasks):
        tasks[f"task-{i}"] = {
            "status": "completed" if i < terminal else "ready",
            "scheduled_at_sec": 0.0,
            "arrived_at_sec": 0.0,
            "claimed_at_sec": 1.0,
            "terminal_at_sec": 100.0 if i < terminal else None,
            "wait_sec": 1.0,
            "service_sec": 99.0,
            "turnaround_sec": 100.0,
            "reward": 1.0 if i < n_passed else 0.0,
        }
    return {
        "schema_version": "1.0",
        "pool_start_ts": 1789788870.0,
        "stream_done_reason": "all_completed",
        "tasks": tasks,
        "aggregates": {
            "n_tasks": n_tasks,
            "n_passed": n_passed,
            "n_unclaimed": n_unclaimed,
            "n_timeout": 0,
            "makespan_sec": makespan_sec,
            "mean_flow_time_sec": 100.0,
            "mean_wait_sec": 1.0,
            "mean_service_sec": 99.0,
            "throughput_tasks_per_hour": throughput,
            "mean_realized_concurrency": 2.0,
            "peak_realized_concurrency": 3,
        },
        "curves": {},
    }


def stream_scheduling(
    *,
    tasks: dict[str, dict[str, Any]],
    makespan_sec: float,
    concurrency: list[list[float]] | None = None,
    backlog: list[list[float]] | None = None,
    mean_concurrency: float = 1.0,
    peak_concurrency: int = 1,
    stream_done_reason: str | None = "all_completed",
    n_unclaimed: int = 0,
) -> dict[str, Any]:
    """Scheduling metrics with explicit task stamps and curves.

    Distinct from :func:`scheduling_metrics`, which fabricates a uniform task
    table for the throughput tests. Round-2 scheduling analysis needs control
    over *when* each task arrived, was claimed and terminated, plus the
    ``curves`` payload, so the tests can pin the integration window and the
    starve-vs-drain distinction rather than accepting whatever the code says.
    """
    return {
        "schema_version": "1.0",
        "pool_start_ts": 1789788870.0,
        "stream_done_reason": stream_done_reason,
        "tasks": tasks,
        "aggregates": {
            "n_tasks": len(tasks),
            "n_passed": sum(1 for t in tasks.values() if (t.get("reward") or 0) >= 1.0),
            "n_unclaimed": n_unclaimed,
            "n_timeout": 0,
            "makespan_sec": makespan_sec,
            "mean_wait_sec": 0.0,
            "mean_service_sec": 0.0,
            "throughput_tasks_per_hour": 1.0,
            "mean_realized_concurrency": mean_concurrency,
            "peak_realized_concurrency": peak_concurrency,
        },
        "curves": {
            "concurrency": concurrency if concurrency is not None else [],
            "backlog": backlog if backlog is not None else [],
            "completed": [],
        },
    }


def task(
    *,
    arrived: float,
    claimed: float | None,
    terminal: float | None,
    reward: float = 0.0,
    status: str = "completed",
) -> dict[str, Any]:
    """One task-table entry with wait/service/turnaround kept self-consistent."""
    wait = None if claimed is None else claimed - arrived
    service = None if claimed is None or terminal is None else terminal - claimed
    return {
        "status": status,
        "scheduled_at_sec": arrived,
        "arrived_at_sec": arrived,
        "claimed_at_sec": claimed,
        "terminal_at_sec": terminal,
        "wait_sec": wait,
        "service_sec": service,
        "turnaround_sec": None if wait is None or service is None else wait + service,
        "reward": reward,
    }
