from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from .result import TaskVerificationRecord, VerifyResult
from .runner import VerificationRunner
from .watcher import EventsWatcher


@dataclass
class _OnlineRequest:
    task_id: str
    trigger_step: int | None
    trigger_ts: float | None


class VerificationCoordinator:
    def __init__(
        self,
        *,
        tasks_dir: Path,
        workspace_dir: Path,
        state_dir: Path,
        timeout_sec: float = 120.0,
        poll_interval_sec: float = 0.2,
    ) -> None:
        self._tasks_dir = tasks_dir
        self._workspace_dir = workspace_dir
        self._state_dir = state_dir
        self._verify_dir = state_dir / "verify_results"
        self._watcher = EventsWatcher(state_dir / "events.jsonl", poll_interval_sec=poll_interval_sec)
        self._runner = VerificationRunner(
            tasks_dir=tasks_dir,
            workspace_dir=workspace_dir,
            timeout_sec=timeout_sec,
        )
        self._records: dict[str, TaskVerificationRecord] = {}
        self._online_seen: set[str] = set()
        self._online_queue: asyncio.Queue[_OnlineRequest | None] = asyncio.Queue()
        self._watch_task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        self._online_ingest_open = False

    async def start(self) -> None:
        self._verify_dir.mkdir(parents=True, exist_ok=True)
        self._online_ingest_open = True
        await self._watcher.replay(self._handle_event)
        self._watch_task = asyncio.create_task(self._watcher.watch(self._handle_event))
        self._worker_task = asyncio.create_task(self._online_worker())

    async def stop_online_ingest(self) -> None:
        self._online_ingest_open = False
        self._watcher.stop()
        if self._watch_task is not None:
            await self._watch_task
            self._watch_task = None
        await self._online_queue.put(None)

    async def drain_online(self, timeout_sec: float = 60.0) -> None:
        if self._worker_task is None:
            return
        await asyncio.wait_for(self._worker_task, timeout=timeout_sec)
        self._worker_task = None

    async def run_final_batch(self, arrived_task_ids: list[str]) -> None:
        for task_id in arrived_task_ids:
            record = self._records.setdefault(task_id, TaskVerificationRecord(task_id=task_id))
            if record.final_result is not None:
                continue
            result = await self._runner.run(
                task_id=task_id,
                kind="final",
                attempt=1,
                trigger="session_end",
                trigger_step=None,
                trigger_ts=None,
            )
            record.final_result = result
            self._write_result(task_id, result)
        self._write_index()

    async def shutdown(self) -> None:
        if self._watch_task is not None or self._worker_task is not None:
            await self.stop_online_ingest()
            await self.drain_online()
        self._write_index()

    def summary(self) -> dict[str, TaskVerificationRecord]:
        return self._records

    async def _handle_event(self, event: dict) -> None:
        if not self._online_ingest_open:
            return
        task_id = event.get("task_id")
        if not task_id or task_id in self._online_seen:
            return
        self._online_seen.add(task_id)
        await self._online_queue.put(
            _OnlineRequest(
                task_id=task_id,
                trigger_step=event.get("step"),
                trigger_ts=event.get("ts"),
            )
        )

    async def _online_worker(self) -> None:
        while True:
            req = await self._online_queue.get()
            if req is None:
                self._online_queue.task_done()
                break
            record = self._records.setdefault(req.task_id, TaskVerificationRecord(task_id=req.task_id))
            attempt = len(record.online_results) + 1
            result = await self._runner.run(
                task_id=req.task_id,
                kind="online",
                attempt=attempt,
                trigger="complete",
                trigger_step=req.trigger_step,
                trigger_ts=req.trigger_ts,
            )
            record.online_results.append(result)
            self._write_result(req.task_id, result)
            self._write_index()
            self._online_queue.task_done()

    def _write_result(self, task_id: str, result: VerifyResult) -> None:
        task_dir = self._verify_dir / task_id
        if result.kind == "online":
            filename = f"online_{result.attempt:03d}.json"
        else:
            filename = "final.json"
        result.write_json(task_dir / filename)

    def _write_index(self) -> None:
        payload = {
            task_id: record.to_dict()
            for task_id, record in sorted(self._records.items())
        }
        self._verify_dir.mkdir(parents=True, exist_ok=True)
        (self._verify_dir / "index.json").write_text(json.dumps(payload, indent=2))
