#!/usr/bin/env python3
"""Monitor an AOS-Bench stream trial and log deadline progress."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path


DONE = {"completed", "failed"}


def docker_read(container: str, path: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container, "cat", path],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def load_deadlines(stream_toml: Path) -> dict[str, float | None]:
    with stream_toml.open("rb") as f:
        stream = tomllib.load(f)
    deadlines: dict[str, float | None] = {}
    for arrival in stream.get("arrivals", []):
        task_id = arrival["task_id"]
        deadlines[task_id] = arrival.get("deadline_wall_time")
    return deadlines


def infer_elapsed(events_text: str) -> float:
    starts: list[float] = []
    last_at = 0.0
    for line in events_text.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if "ts" in event and "at_sec" in event:
            starts.append(float(event["ts"]) - float(event["at_sec"]))
            last_at = max(last_at, float(event["at_sec"]))
    if not starts:
        return last_at
    starts.sort()
    start_abs = starts[len(starts) // 2]
    return max(last_at, time.time() - start_abs)


def summarize(
    tasks: list[dict],
    deadlines: dict[str, float | None],
    elapsed: float,
    warn_window_sec: float,
) -> tuple[dict[str, int], list[str], list[str], list[str]]:
    counts: dict[str, int] = {}
    overdue: list[str] = []
    due_soon: list[str] = []
    late_done: list[str] = []

    for task in tasks:
        task_id = task["task_id"]
        status = task.get("status", "<none>")
        counts[status] = counts.get(status, 0) + 1
        deadline = deadlines.get(task_id)
        if deadline is None:
            continue
        if status in DONE:
            completed_at = task.get("completed_at")
            if completed_at is not None and float(completed_at) > deadline:
                late_done.append(
                    f"{task_id} completed_at={float(completed_at):.1f}s deadline={deadline:.1f}s"
                )
        elif elapsed > deadline:
            overdue.append(f"{task_id} status={status} deadline={deadline:.1f}s")
        elif deadline - elapsed <= warn_window_sec:
            due_soon.append(
                f"{task_id} status={status} deadline={deadline:.1f}s eta={deadline - elapsed:.1f}s"
            )

    return counts, overdue, due_soon, late_done


def log_line(log_path: Path, message: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"{ts} {message}"
    print(line, flush=True)
    with log_path.open("a") as f:
        f.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--stream-toml", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--interval-sec", type=float, default=60.0)
    parser.add_argument("--warn-window-sec", type=float, default=120.0)
    parser.add_argument("--max-runtime-sec", type=float, default=0.0)
    parser.add_argument(
        "--kill-pid-on-timeout",
        type=int,
        default=None,
        help="Send SIGTERM to this PID if max-runtime-sec is reached.",
    )
    args = parser.parse_args()

    deadlines = load_deadlines(args.stream_toml)
    started = time.time()
    args.log.parent.mkdir(parents=True, exist_ok=True)

    while True:
        result_file = args.job_dir / "result.json"
        reward_files = list(args.job_dir.glob("*/verifier/reward.json"))
        if result_file.exists() and reward_files:
            reward = json.loads(reward_files[0].read_text())
            passed = sum(1 for value in reward.values() if float(value) > 0)
            total = len(reward)
            log_line(
                args.log,
                f"FINISHED result={result_file} reward={reward_files[0]} passed={passed}/{total}",
            )
            return 0

        try:
            tasks_state = json.loads(
                docker_read(args.container, "/sandbox/aos/state/tasks.json")
            )
            events_text = docker_read(args.container, "/sandbox/aos/state/events.jsonl")
        except subprocess.CalledProcessError as exc:
            if result_file.exists():
                log_line(args.log, f"FINISHED result={result_file} container_unavailable")
                return 0
            log_line(args.log, f"ERROR docker_read_failed returncode={exc.returncode}")
            return 2

        tasks = tasks_state.get("tasks", [])
        elapsed = infer_elapsed(events_text)
        counts, overdue, due_soon, late_done = summarize(
            tasks, deadlines, elapsed, args.warn_window_sec
        )
        log_line(
            args.log,
            "STATUS "
            f"elapsed={elapsed:.1f}s stream_done={tasks_state.get('stream_done')} "
            f"counts={json.dumps(counts, sort_keys=True)}",
        )
        for item in overdue:
            log_line(args.log, f"OVERDUE {item}")
        for item in due_soon:
            log_line(args.log, f"DUE_SOON {item}")
        for item in late_done:
            log_line(args.log, f"LATE_DONE {item}")

        if tasks_state.get("stream_done"):
            log_line(args.log, "STREAM_DONE waiting_for_verifier")

        if args.max_runtime_sec and time.time() - started > args.max_runtime_sec:
            log_line(args.log, "STOP max_runtime_sec reached")
            if args.kill_pid_on_timeout:
                try:
                    os.kill(args.kill_pid_on_timeout, signal.SIGTERM)
                    log_line(
                        args.log,
                        f"TIMEOUT sent SIGTERM pid={args.kill_pid_on_timeout}",
                    )
                except ProcessLookupError:
                    log_line(
                        args.log,
                        f"TIMEOUT pid_already_exited pid={args.kill_pid_on_timeout}",
                    )
            return 3

        time.sleep(args.interval_sec)


if __name__ == "__main__":
    sys.exit(main())
