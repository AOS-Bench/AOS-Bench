from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aos_bench.verifier import VerificationCoordinator, VerificationRunner


def _write_test_script(tasks_dir: Path, task_id: str, body: str) -> None:
    test_path = tasks_dir / task_id / "tests" / "test.sh"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(body)
    test_path.chmod(0o755)


class TestVerificationRunner:
    def test_runner_reads_reward_from_aos_verify_out(self, tmp_path):
        workspace_dir = tmp_path / "workspace"
        tasks_dir = tmp_path / "tasks"
        workspace_dir.mkdir()

        _write_test_script(
            tasks_dir,
            "task-a",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "mkdir -p \"$AOS_VERIFY_OUT\"\n"
            "echo 1 > \"$AOS_VERIFY_OUT/reward.txt\"\n"
            "echo ok\n",
        )

        runner = VerificationRunner(tasks_dir=tasks_dir, workspace_dir=workspace_dir)
        result = asyncio.run(
            runner.run(
                task_id="task-a",
                kind="final",
                attempt=1,
                trigger="session_end",
                trigger_step=None,
                trigger_ts=None,
            )
        )

        assert result.passed is True
        assert result.reward == 1.0
        assert result.reward_path is not None

    def test_runner_marks_missing_reward_file_as_failure(self, tmp_path):
        workspace_dir = tmp_path / "workspace"
        tasks_dir = tmp_path / "tasks"
        workspace_dir.mkdir()

        _write_test_script(
            tasks_dir,
            "task-b",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "echo no reward\n",
        )

        runner = VerificationRunner(tasks_dir=tasks_dir, workspace_dir=workspace_dir)
        result = asyncio.run(
            runner.run(
                task_id="task-b",
                kind="final",
                attempt=1,
                trigger="session_end",
                trigger_step=None,
                trigger_ts=None,
            )
        )

        assert result.passed is False
        assert result.error == "no_reward_file"


class TestVerificationCoordinator:
    def test_online_and_final_results_are_both_persisted(self, tmp_path):
        workspace_dir = tmp_path / "workspace"
        tasks_dir = tmp_path / "tasks"
        state_dir = tmp_path / "state"
        workspace_dir.mkdir()
        state_dir.mkdir()

        _write_test_script(
            tasks_dir,
            "task-c",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "mkdir -p \"$AOS_VERIFY_OUT\"\n"
            "echo 1 > \"$AOS_VERIFY_OUT/reward.txt\"\n",
        )

        async def _run() -> VerificationCoordinator:
            coordinator = VerificationCoordinator(
                tasks_dir=tasks_dir,
                workspace_dir=workspace_dir,
                state_dir=state_dir,
                timeout_sec=5.0,
                poll_interval_sec=0.05,
            )
            await coordinator.start()
            (state_dir / "events.jsonl").write_text(
                json.dumps({"event": "complete", "task_id": "task-c", "step": 3, "ts": 10.0}) + "\n"
            )
            await asyncio.sleep(0.2)
            await coordinator.stop_online_ingest()
            await coordinator.drain_online(timeout_sec=5.0)
            await coordinator.run_final_batch(["task-c"])
            return coordinator

        coordinator = asyncio.run(_run())
        summary = coordinator.summary()
        assert "task-c" in summary
        assert len(summary["task-c"].online_results) == 1
        assert summary["task-c"].online_results[0].passed is True
        assert summary["task-c"].final_result is not None
        assert summary["task-c"].final_result.passed is True
        assert (state_dir / "verify_results" / "task-c" / "online_001.json").exists()
        assert (state_dir / "verify_results" / "task-c" / "final.json").exists()

    def test_final_batch_runs_without_complete_event(self, tmp_path):
        workspace_dir = tmp_path / "workspace"
        tasks_dir = tmp_path / "tasks"
        state_dir = tmp_path / "state"
        workspace_dir.mkdir()
        state_dir.mkdir()

        _write_test_script(
            tasks_dir,
            "task-d",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "mkdir -p \"$AOS_VERIFY_OUT\"\n"
            "echo 1 > \"$AOS_VERIFY_OUT/reward.txt\"\n",
        )

        async def _run() -> VerificationCoordinator:
            coordinator = VerificationCoordinator(
                tasks_dir=tasks_dir,
                workspace_dir=workspace_dir,
                state_dir=state_dir,
                timeout_sec=5.0,
            )
            await coordinator.start()
            await coordinator.stop_online_ingest()
            await coordinator.drain_online(timeout_sec=5.0)
            await coordinator.run_final_batch(["task-d"])
            return coordinator

        coordinator = asyncio.run(_run())
        summary = coordinator.summary()
        assert "task-d" in summary
        assert summary["task-d"].online_results == []
        assert summary["task-d"].final_result is not None
        assert summary["task-d"].final_result.passed is True
