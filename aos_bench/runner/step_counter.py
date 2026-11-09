"""
StepCounter: tracks LLM call count and lets callers await specific step thresholds.

One "step" = one AssistantMessage received from claude-agent-sdk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class StepRecord:
    step: int
    timestamp: float
    content_preview: str


class StepCounter:
    def __init__(self, log_path: Path | None = None) -> None:
        self._count = 0
        self._records: list[StepRecord] = []
        self._log_path = log_path

    @property
    def value(self) -> int:
        return self._count

    def increment(self, content_preview: str = "") -> None:
        self._count += 1
        record = StepRecord(
            step=self._count,
            timestamp=time.time(),
            content_preview=content_preview[:120],
        )
        self._records.append(record)
        if self._log_path:
            try:
                with open(self._log_path, "a") as f:
                    f.write(json.dumps({
                        "step": record.step,
                        "ts": record.timestamp,
                        "preview": record.content_preview,
                    }) + "\n")
            except OSError as e:
                logger.warning("step_counter: failed to write log: %s", e)

    async def wait_until(
        self, target: int, poll_interval: float = 0.05, timeout_sec: float | None = None
    ) -> None:
        """Block until step count >= target.

        Raises asyncio.TimeoutError if timeout_sec elapses before target is reached.
        """
        deadline = time.monotonic() + timeout_sec if timeout_sec is not None else None
        while self._count < target:
            if deadline is not None and time.monotonic() >= deadline:
                raise asyncio.TimeoutError(
                    f"step_counter: timed out waiting for step {target} "
                    f"(current={self._count}, timeout={timeout_sec}s)"
                )
            await asyncio.sleep(poll_interval)
