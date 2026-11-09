from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path

from .result import VerifyKind, VerifyResult, VerifyTrigger


def _truncate(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


class VerificationRunner:
    def __init__(
        self,
        *,
        tasks_dir: Path,
        workspace_dir: Path,
        timeout_sec: float = 120.0,
        temp_root: Path | None = None,
    ) -> None:
        self._tasks_dir = tasks_dir
        self._workspace_dir = workspace_dir
        self._timeout_sec = timeout_sec
        self._temp_root = temp_root or Path("/tmp/aos_verify")

    async def run(
        self,
        *,
        task_id: str,
        kind: VerifyKind,
        attempt: int,
        trigger: VerifyTrigger,
        trigger_step: int | None,
        trigger_ts: float | None,
    ) -> VerifyResult:
        started_ts = time.time()
        job_root = self._temp_root / f"{task_id}-{kind}-{attempt:03d}-{uuid.uuid4().hex[:8]}"
        out_dir = job_root / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        test_script = self._tasks_dir / task_id / "tests" / "test.sh"
        if not test_script.exists():
            finished_ts = time.time()
            shutil.rmtree(job_root, ignore_errors=True)
            return VerifyResult(
                kind=kind,
                task_id=task_id,
                attempt=attempt,
                trigger=trigger,
                trigger_step=trigger_step,
                trigger_ts=trigger_ts,
                started_ts=started_ts,
                finished_ts=finished_ts,
                duration_sec=finished_ts - started_ts,
                reward=None,
                passed=False,
                exit_code=None,
                error=f"missing_test_script:{test_script}",
            )

        env = os.environ.copy()
        env["AOS_VERIFY_OUT"] = str(out_dir)
        env["AOS_TASK_ID"] = task_id
        env["AOS_VERIFY_KIND"] = kind
        env.setdefault("PYTHONUNBUFFERED", "1")

        exit_code: int | None = None
        stdout_tail: str | None = None
        stderr_tail: str | None = None
        error: str | None = None
        reward: float | None = None
        reward_path: str | None = None

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(test_script),
                cwd=str(self._workspace_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_sec)
                exit_code = proc.returncode
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                exit_code = proc.returncode
                error = "timeout"

            stdout_tail = _truncate(stdout.decode("utf-8", errors="replace"))
            stderr_tail = _truncate(stderr.decode("utf-8", errors="replace"))

            reward, reward_path = self._read_reward(task_id=task_id, out_dir=out_dir)
            if reward is None and error is None:
                error = "no_reward_file"
        except Exception as exc:
            error = f"runner_error:{exc}"
        finally:
            finished_ts = time.time()
            shutil.rmtree(job_root, ignore_errors=True)

        passed = reward is not None and reward >= 1.0 and error != "timeout"
        return VerifyResult(
            kind=kind,
            task_id=task_id,
            attempt=attempt,
            trigger=trigger,
            trigger_step=trigger_step,
            trigger_ts=trigger_ts,
            started_ts=started_ts,
            finished_ts=finished_ts,
            duration_sec=finished_ts - started_ts,
            reward=reward,
            passed=passed,
            exit_code=exit_code,
            error=error,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            reward_path=reward_path,
        )

    def _read_reward(self, *, task_id: str, out_dir: Path) -> tuple[float | None, str | None]:
        candidates = [
            out_dir / "reward.txt",
            Path("/logs/verifier") / task_id / "reward.txt",
            Path("/logs/verifier/reward.txt"),
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                raw = path.read_text().strip()
                if not raw:
                    continue
                return float(raw), str(path)
            except ValueError:
                continue
        return None, None
