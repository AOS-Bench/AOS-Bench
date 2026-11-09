"""
Quick smoke test: 2-task schedule, step-based injection.

Two modes:

1. Inline schedule (default, no fixture needed):

    no_proxy=127.0.0.1 ANTHROPIC_BASE_URL=http://127.0.0.1:4000 \
    ANTHROPIC_API_KEY=<key> python3 -m aos_bench.runner.smoke_test

2. Stream fixture (loads aos_bench/tests/fixtures/mini-stream):

    no_proxy=127.0.0.1 ANTHROPIC_BASE_URL=http://127.0.0.1:4000 \
    ANTHROPIC_API_KEY=<key> python3 -m aos_bench.runner.smoke_test --stream
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .schedule import Arrival, ArrivalSchedule
from .session import AOSBenchSession


def _inline_schedule() -> ArrivalSchedule:
    return ArrivalSchedule(
        arrivals=[
            Arrival(
                step=0,
                task_id="task-hello",
                instruction="Say 'Hello AOS-Bench' and list the numbers 1, 2, 3.",
            ),
            Arrival(
                step=1,
                task_id="task-reverse",
                instruction="Now list those same numbers in reverse order: 3, 2, 1.",
            ),
        ],
        max_steps=10,
    )


def _mini_stream_schedule() -> ArrivalSchedule:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "tests" / "fixtures" / "mini-stream"
    )
    return ArrivalSchedule.from_stream_toml(fixture)


async def main() -> None:
    parser = argparse.ArgumentParser(description="AOS-Bench smoke test")
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Load schedule from the mini-stream fixture instead of inline",
    )
    args = parser.parse_args()

    schedule = _mini_stream_schedule() if args.stream else _inline_schedule()

    session = AOSBenchSession(
        schedule=schedule,
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:4000"),
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )

    print("=== AOS-Bench Smoke Test ===", flush=True)
    result = await session.run()

    print(f"\ntotal_steps : {result.total_steps}")
    print(f"stop_reason : {result.stop_reason}")
    print(f"wall_time   : {result.wall_time_sec:.1f}s")
    for tr in result.task_results:
        print(f"  task '{tr.task_id}' injected at step {tr.injected_at_step}")

    assert result.total_steps >= 2, f"Expected >=2 steps, got {result.total_steps}"
    expected_ids = {a.task_id for a in schedule.arrivals}
    got_ids = {tr.task_id for tr in result.task_results}
    assert got_ids == expected_ids, f"Expected {expected_ids}, got {got_ids}"
    print("\n[PASS] Smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
