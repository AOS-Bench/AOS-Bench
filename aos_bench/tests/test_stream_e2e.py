"""
Phase 1.2 smoke/integration test for stream-format E2E.

Exercises the full stack minus the LLM:
  - ArrivalSchedule.from_stream_toml loads the fixture stream.toml + per-task
    instruction.md
  - A simulated "agent" prepares the workspace and writes events.jsonl
  - VerificationCoordinator tails events, runs online test.sh, then final batch
  - Per-task verify results land under state/verify_results/

The LLM-driven session is covered separately by smoke_test.py (requires
credentials); this test runs in CI without Docker or network.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from aos_bench.runner.schedule import ArrivalSchedule
from aos_bench.verifier import VerificationCoordinator


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "mini-stream"


def _write_event(events_path: Path, task_id: str, step: int) -> None:
    with open(events_path, "a") as f:
        f.write(
            json.dumps(
                {"event": "complete", "task_id": task_id, "step": step, "ts": time.time()}
            )
            + "\n"
        )


def _run_pipeline(
    tmp_path: Path,
    *,
    prepare_alpha: bool = True,
    prepare_beta: bool = True,
    alpha_content: str = "hello alpha",
    beta_content: str = "hello beta",
) -> dict[str, dict]:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()

    if prepare_alpha:
        (workspace / "alpha.txt").write_text(alpha_content)
    if prepare_beta:
        (workspace / "beta.txt").write_text(beta_content)

    async def _run() -> dict[str, dict]:
        coord = VerificationCoordinator(
            tasks_dir=FIXTURE_DIR / "tasks",
            workspace_dir=workspace,
            state_dir=state,
            timeout_sec=10.0,
            poll_interval_sec=0.05,
        )
        await coord.start()

        events_path = state / "events.jsonl"
        _write_event(events_path, "task-alpha", step=1)
        _write_event(events_path, "task-beta", step=3)

        await asyncio.sleep(0.3)
        await coord.stop_online_ingest()
        await coord.drain_online(timeout_sec=10.0)
        await coord.run_final_batch(["task-alpha", "task-beta"])
        await coord.shutdown()
        return {tid: rec.to_dict() for tid, rec in coord.summary().items()}

    return asyncio.run(_run())


class TestMiniStreamFixture:
    def test_stream_schedule_loads(self):
        schedule = ArrivalSchedule.from_stream_toml(FIXTURE_DIR)
        assert schedule.max_steps == 10
        ids = [a.task_id for a in schedule.arrivals]
        assert ids == ["task-alpha", "task-beta"]
        assert schedule.arrivals[0].step == 0
        assert schedule.arrivals[1].step == 2
        assert schedule.arrivals[0].deadline_step == 5
        assert schedule.arrivals[1].deadline_step == 8
        # Instruction bodies loaded from instruction.md
        assert "alpha.txt" in schedule.arrivals[0].instruction
        assert "beta.txt" in schedule.arrivals[1].instruction

    def test_happy_path_both_tasks_pass(self, tmp_path):
        summary = _run_pipeline(tmp_path)

        assert set(summary.keys()) == {"task-alpha", "task-beta"}
        for task_id in ("task-alpha", "task-beta"):
            rec = summary[task_id]
            assert len(rec["online_results"]) == 1, f"{task_id} missing online result"
            assert rec["online_results"][0]["passed"] is True
            assert rec["final_result"] is not None
            assert rec["final_result"]["passed"] is True
            assert rec["final_result"]["reward"] == 1.0

        # Per-task artifacts land in state/verify_results/<id>/
        state = tmp_path / "state" / "verify_results"
        assert (state / "task-alpha" / "online_001.json").exists()
        assert (state / "task-alpha" / "final.json").exists()
        assert (state / "task-beta" / "online_001.json").exists()
        assert (state / "task-beta" / "final.json").exists()
        assert (state / "index.json").exists()

    def test_missing_workspace_file_marks_failure(self, tmp_path):
        summary = _run_pipeline(tmp_path, prepare_alpha=False)

        alpha = summary["task-alpha"]
        assert alpha["final_result"]["passed"] is False
        assert alpha["final_result"]["reward"] == 0.0

        beta = summary["task-beta"]
        assert beta["final_result"]["passed"] is True

    def test_wrong_content_marks_failure(self, tmp_path):
        summary = _run_pipeline(tmp_path, alpha_content="wrong content")

        alpha = summary["task-alpha"]
        assert alpha["final_result"]["passed"] is False
        assert alpha["final_result"]["reward"] == 0.0
