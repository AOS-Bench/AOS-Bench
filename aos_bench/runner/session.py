"""
AOSBenchSession: runs a claude-agent-sdk session with step counting + message injection.

Usage:
    session = AOSBenchSession(schedule, options)
    result = await session.run()
    print(result.total_steps, result.task_results)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

from .schedule import ArrivalSchedule
from .step_counter import StepCounter

logger = logging.getLogger(__name__)

# How long to wait for a scheduled step before giving up (seconds).
# Set to None for no timeout (use only in tests or when max_turns will stop the session).
DEFAULT_INJECTION_TIMEOUT_SEC = 300.0


def _user_msg(text: str) -> dict:
    return {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }


@dataclass
class TaskResult:
    task_id: str
    injected_at_step: int
    injection_ts: float


@dataclass
class SessionResult:
    total_steps: int
    stop_reason: str
    wall_time_sec: float
    task_results: list[TaskResult] = field(default_factory=list)


class AOSBenchSession:
    def __init__(
        self,
        schedule: ArrivalSchedule,
        *,
        base_url: str = "http://127.0.0.1:4000",
        api_key: str = "",
        model: str | None = None,
        cwd: str | Path | None = None,
        steps_log_path: Path | None = None,
        injection_timeout_sec: float | None = DEFAULT_INJECTION_TIMEOUT_SEC,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._schedule = schedule
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._cwd = cwd
        self._steps_log_path = steps_log_path
        self._injection_timeout_sec = injection_timeout_sec
        self._extra_env = extra_env or {}

        self._counter = StepCounter(log_path=steps_log_path)
        self._task_results: list[TaskResult] = []

    def _build_options(self) -> ClaudeAgentOptions:
        env: dict[str, str] = {
            "ANTHROPIC_BASE_URL": self._base_url,
            "ANTHROPIC_API_KEY": self._api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
            "IS_SANDBOX": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        env.update(self._extra_env)

        return ClaudeAgentOptions(
            max_turns=self._schedule.max_steps,
            permission_mode="bypassPermissions",
            model=self._model,
            cwd=str(self._cwd) if self._cwd else None,
            env=env,
        )

    def _build_message_stream(self):
        schedule = self._schedule
        counter = self._counter
        task_results = self._task_results
        timeout = self._injection_timeout_sec

        async def _stream():
            step0_tasks = [a for a in schedule.arrivals if a.step == 0]
            later_tasks = [a for a in schedule.arrivals if a.step > 0]

            if step0_tasks:
                combined = "\n\n---\n\n".join(a.to_user_message() for a in step0_tasks)
                for a in step0_tasks:
                    task_results.append(TaskResult(
                        task_id=a.task_id,
                        injected_at_step=0,
                        injection_ts=time.time(),
                    ))
                yield _user_msg(combined)

            for arrival in later_tasks:
                try:
                    await counter.wait_until(arrival.step, timeout_sec=timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "session: timed out waiting for step %d to inject task '%s' "
                        "(current=%d, timeout=%.0fs) — task dropped",
                        arrival.step, arrival.task_id, counter.value, timeout or 0,
                    )
                    continue

                if counter.value >= schedule.max_steps:
                    logger.warning(
                        "session: step %d >= max_steps %d when trying to inject '%s' — task dropped",
                        counter.value, schedule.max_steps, arrival.task_id,
                    )
                    continue

                task_results.append(TaskResult(
                    task_id=arrival.task_id,
                    injected_at_step=counter.value,
                    injection_ts=time.time(),
                ))
                logger.info("session: injected task '%s' at step %d", arrival.task_id, counter.value)
                yield _user_msg(arrival.to_user_message())

        return _stream()

    async def run(self) -> SessionResult:
        options = self._build_options()
        stream = self._build_message_stream()
        start = time.time()
        stop_reason = "unknown"

        async for msg in query(prompt=stream, options=options):
            if isinstance(msg, AssistantMessage):
                preview = ""
                for block in msg.content:
                    if hasattr(block, "text"):
                        preview = block.text
                        break
                self._counter.increment(content_preview=preview)

            elif isinstance(msg, ResultMessage):
                stop_reason = msg.stop_reason or "end_turn"

        return SessionResult(
            total_steps=self._counter.value,
            stop_reason=stop_reason,
            wall_time_sec=time.time() - start,
            task_results=self._task_results,
        )
