"""
CLI entry point for running an AOS-Bench session.

Usage:
    python -m aos_bench.runner.run --schedule path/to/schedule.toml [--cwd /workspace]
    python -m aos_bench.runner.run --schedule path/to/schedule.json [--cwd /workspace]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .schedule import ArrivalSchedule
from .session import AOSBenchSession
from aos_bench.verifier import VerificationCoordinator


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AOS-Bench session runner")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--schedule", help="Path to arrival_schedule.toml or .json (flat format)")
    src.add_argument(
        "--stream",
        help="Path to a stream directory (or its stream.toml). Instructions are loaded from tasks/<id>/instruction.md.",
    )
    p.add_argument("--cwd", default=None, help="Working directory for the agent")
    p.add_argument("--base-url", default=None, help="ANTHROPIC_BASE_URL override")
    p.add_argument("--model", default=None, help="Model name override")
    p.add_argument("--steps-log", default=None, help="Path to write steps.jsonl")
    p.add_argument("--result-json", default=None, help="Path to write session result JSON")
    p.add_argument("--injection-timeout", type=float, default=300.0,
                   help="Seconds to wait for a scheduled step before dropping a task (default: 300)")
    p.add_argument("--tasks-dir", default=None, help="Path to stream tasks/ directory for verification")
    p.add_argument("--state-dir", default=None, help="Path to state directory containing events.jsonl")
    p.add_argument("--workspace-dir", default=None, help="Workspace directory used when running verifier test.sh")
    p.add_argument("--verify-timeout", type=float, default=120.0,
                   help="Seconds to allow each verification script to run (default: 120)")
    return p.parse_args()


async def main() -> None:
    args = parse_args()

    try:
        if args.stream:
            schedule = ArrivalSchedule.from_stream_toml(Path(args.stream))
        else:
            schedule_path = Path(args.schedule)
            if schedule_path.suffix == ".toml":
                schedule = ArrivalSchedule.from_toml(schedule_path)
            else:
                schedule = ArrivalSchedule.from_json(schedule_path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: invalid schedule — {e}", file=sys.stderr)
        sys.exit(1)

    # When loading from a stream, default tasks-dir to its tasks/ subdir.
    if args.stream and not args.tasks_dir:
        stream_path = Path(args.stream)
        stream_root = stream_path if stream_path.is_dir() else stream_path.parent
        default_tasks_dir = stream_root / "tasks"
        if default_tasks_dir.exists():
            args.tasks_dir = str(default_tasks_dir)

    base_url = args.base_url or os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:4000")
    steps_log = Path(args.steps_log) if args.steps_log else None

    session = AOSBenchSession(
        schedule=schedule,
        base_url=base_url,
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        model=args.model,
        cwd=args.cwd,
        steps_log_path=steps_log,
        injection_timeout_sec=args.injection_timeout,
    )

    verifier: VerificationCoordinator | None = None
    if args.tasks_dir and args.state_dir:
        workspace_dir = Path(args.workspace_dir or args.cwd or "/workspace")
        verifier = VerificationCoordinator(
            tasks_dir=Path(args.tasks_dir),
            workspace_dir=workspace_dir,
            state_dir=Path(args.state_dir),
            timeout_sec=args.verify_timeout,
        )
        await verifier.start()

    print(f"[aos-bench] Starting session: {len(schedule.arrivals)} tasks, max_steps={schedule.max_steps}", flush=True)
    result = await session.run()

    verification_summary: dict[str, dict] = {}
    if verifier is not None:
        await verifier.stop_online_ingest()
        await verifier.drain_online(timeout_sec=args.verify_timeout)
        await verifier.run_final_batch([tr.task_id for tr in result.task_results])
        await verifier.shutdown()
        verification_summary = {
            task_id: record.to_dict()
            for task_id, record in verifier.summary().items()
        }

    print(f"\n[aos-bench] Session complete", flush=True)
    print(f"  total_steps : {result.total_steps}", flush=True)
    print(f"  stop_reason : {result.stop_reason}", flush=True)
    print(f"  wall_time   : {result.wall_time_sec:.1f}s", flush=True)
    print(f"  tasks injected:", flush=True)
    for tr in result.task_results:
        print(f"    {tr.task_id} @ step {tr.injected_at_step}", flush=True)
    if verification_summary:
        print(f"  verification: {len(verification_summary)} task records", flush=True)

    if args.result_json:
        out = {
            "total_steps": result.total_steps,
            "stop_reason": result.stop_reason,
            "wall_time_sec": result.wall_time_sec,
            "task_results": [
                {"task_id": tr.task_id, "injected_at_step": tr.injected_at_step}
                for tr in result.task_results
            ],
        }
        if verification_summary:
            out["verification"] = verification_summary
        Path(args.result_json).write_text(json.dumps(out, indent=2))
        print(f"  result written to {args.result_json}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
