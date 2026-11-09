"""Octopus Agent — single-Run tool loop.

Drives one ``Run``: feed messages to the LLM, dispatch tool calls,
emit AgentTrace events. The Coordinator is responsible for creating the
Run (root or subagent) and for building the per-Run ToolRegistry.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMClient
from .tools import ToolRegistry, ToolResult
from .trace import Run, TraceWriter

logger = logging.getLogger("octopus.agent")


@dataclass
class AgentConfig:
    model: str
    agent_name: str = "octopus-default"
    max_steps: int = 50
    max_tool_calls_per_step: int = 16


@dataclass
class AgentResult:
    run_id: str
    stop_reason: str
    total_steps: int
    prompt_tokens: int
    completion_tokens: int
    final_message: str | None
    wall_time_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "stop_reason": self.stop_reason,
            "total_steps": self.total_steps,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "final_message": self.final_message,
            "wall_time_ms": self.wall_time_ms,
        }


@dataclass
class _Counters:
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    final_message: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    """Run-scoped agent. Constructed by the Coordinator per-Run."""

    def __init__(
        self,
        *,
        cfg: AgentConfig,
        llm: LLMClient,
        tools: ToolRegistry,
        trace: TraceWriter,
        run: Run,
        instruction: str,
        system: str | None = None,
    ) -> None:
        self._cfg = cfg
        self._llm = llm
        self._tools = tools
        self._trace = trace
        self._run = run
        self._instruction = instruction
        self._system = system

    async def run(self) -> AgentResult:
        run = self._run
        c = _Counters()
        if self._system:
            c.messages.append({"role": "system", "content": self._system})
        c.messages.append({"role": "user", "content": self._instruction})
        self._trace.emit(
            run,
            "message",
            phase="completed",
            payload={"role": "user", "content": self._instruction},
        )

        t0 = time.monotonic()
        stop_reason = "unknown"
        try:
            stop_reason = await self._loop(run, c)
        except Exception as e:
            logger.exception("octopus agent crashed (run_id=%s)", run.run_id)
            self._trace.emit(
                run,
                "error",
                phase="error",
                payload={"error_type": type(e).__name__, "message": str(e)},
            )
            self._trace.finish_run(
                run,
                status="failed",
                stop_reason=f"crash:{type(e).__name__}",
                usage={
                    "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens,
                },
            )
            raise

        wall_ms = int((time.monotonic() - t0) * 1000)
        run.metrics["step_count"] = c.steps
        run.metrics["tool_call_count"] = c.tool_calls
        self._trace.finish_run(
            run,
            status="completed",
            stop_reason=stop_reason,
            usage={
                "prompt_tokens": c.prompt_tokens,
                "completion_tokens": c.completion_tokens,
            },
        )
        return AgentResult(
            run_id=run.run_id,
            stop_reason=stop_reason,
            total_steps=c.steps,
            prompt_tokens=c.prompt_tokens,
            completion_tokens=c.completion_tokens,
            final_message=c.final_message,
            wall_time_ms=wall_ms,
        )

    async def _loop(self, run: Run, c: _Counters) -> str:
        while c.steps < self._cfg.max_steps:
            step_id = f"step_{c.steps + 1:04d}"
            self._trace.emit(run, "step_begin", step_id=step_id)
            t0 = time.monotonic()
            try:
                resp = await self._llm.chat(
                    messages=c.messages,
                    tools=self._tools.schemas(),
                    model=self._cfg.model,
                )
            except Exception as e:
                self._trace.emit(
                    run,
                    "error",
                    step_id=step_id,
                    phase="error",
                    payload={"where": "llm.chat", "message": str(e),
                             "error_type": type(e).__name__},
                )
                raise
            c.steps += 1
            c.prompt_tokens += resp.prompt_tokens
            c.completion_tokens += resp.completion_tokens
            self._trace.emit(
                run,
                "step_end",
                step_id=step_id,
                phase="completed",
                payload={"finish_reason": resp.finish_reason},
                metrics={
                    "prompt_tokens": resp.prompt_tokens,
                    "completion_tokens": resp.completion_tokens,
                    "wall_time_ms": int((time.monotonic() - t0) * 1000),
                },
            )

            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if resp.content is not None:
                assistant_msg["content"] = resp.content
                self._trace.emit(
                    run,
                    "message",
                    step_id=step_id,
                    phase="completed",
                    payload={"role": "assistant", "content": resp.content},
                )
            if resp.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in resp.tool_calls
                ]
            c.messages.append(assistant_msg)

            if not resp.tool_calls:
                if resp.content:
                    c.final_message = resp.content
                return "natural_end"

            tool_calls = resp.tool_calls[: self._cfg.max_tool_calls_per_step]
            for tc in tool_calls:
                self._trace.emit(
                    run,
                    "tool_call",
                    step_id=step_id,
                    phase="begin",
                    payload={
                        "tool_call_id": tc.id,
                        "tool_name": tc.name,
                        "arguments": tc.arguments,
                    },
                )
                result: ToolResult = await self._tools.dispatch(tc.name, tc.arguments)
                c.tool_calls += 1
                self._trace.emit(
                    run,
                    "tool_result",
                    step_id=step_id,
                    phase=("completed" if result.status == "completed" else "error"),
                    payload={
                        "tool_call_id": tc.id,
                        "tool_name": tc.name,
                        "output": result.output,
                        "status": result.status,
                    },
                    metrics={"wall_time_ms": result.wall_time_ms},
                )
                c.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result.output,
                    }
                )

        return "max_steps_reached"
