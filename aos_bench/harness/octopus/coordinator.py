"""Coordinator — owns subagent spawning and the agent/agent_status/agent_wait tools.

One Coordinator per Octopus process. It:

- Holds the trace, llm, default agent config, and the registry of base
  tools (builtin + MCP proxies).
- Knows about subagent type configs (model / system_prompt / max_steps /
  tool allowlist).
- Builds per-Run ``ToolRegistry`` instances that include the right
  builtin/MCP tools plus the ``agent`` / ``agent_status`` / ``agent_wait``
  tools bound to the calling Run as parent.
- Tracks background subagent ``asyncio.Task``s so the LLM can poll/wait
  on them via ``agent_status`` / ``agent_wait``.

Foreground subagent: parent ``await``s the child Agent. Background:
parent gets a ``task_id`` immediately, and may later call
``agent_status`` / ``agent_wait``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .agent import Agent, AgentConfig, AgentResult
from .llm import LLMClient
from .resource_monitor import ProcessSampler
from .tools import Tool, ToolRegistry
from .trace import Run, TraceWriter

logger = logging.getLogger("octopus.coordinator")


@dataclass
class SubagentType:
    """Static config for a named subagent type."""

    name: str
    system_prompt: str | None = None
    model: str | None = None  # falls back to root model
    max_steps: int | None = None  # falls back to root max_steps
    tools: list[str] | None = None  # whitelist; None = inherit all


@dataclass
class _Background:
    run_id: str
    subagent_type: str
    task: asyncio.Task[AgentResult]


class Coordinator:
    def __init__(
        self,
        *,
        trace: TraceWriter,
        llm: LLMClient,
        default_cfg: AgentConfig,
        base_tools: list[Tool],
        subagent_types: dict[str, SubagentType] | None = None,
        resource_sampling_interval_s: float | None = 1.0,
    ) -> None:
        self._trace = trace
        self._llm = llm
        self._default_cfg = default_cfg
        self._base_tools = base_tools
        self._subagent_types = dict(subagent_types or {})
        self._background: dict[str, _Background] = {}
        self._results: dict[str, AgentResult] = {}
        self._failures: dict[str, str] = {}
        self._resource_sampling_interval_s = resource_sampling_interval_s

    @property
    def has_subagents(self) -> bool:
        return bool(self._subagent_types)

    async def run_root(self, *, instruction: str, system: str | None = None) -> AgentResult:
        run = self._trace.start_root(
            agent_name=self._default_cfg.agent_name,
            model={"provider": "openai-compat", "name": self._default_cfg.model},
            resource_limits={"max_steps": self._default_cfg.max_steps},
        )
        registry = self._build_registry_for(run, allowlist=None)
        agent = Agent(
            cfg=self._default_cfg,
            llm=self._llm,
            tools=registry,
            trace=self._trace,
            run=run,
            instruction=instruction,
            system=system,
        )
        sampler: ProcessSampler | None = None
        if self._resource_sampling_interval_s and self._resource_sampling_interval_s > 0:
            sampler = ProcessSampler(
                trace=self._trace, run=run,
                interval_s=self._resource_sampling_interval_s,
            )
            await sampler.start()
        try:
            result = await agent.run()
        finally:
            if sampler is not None:
                await sampler.stop()
            await self._drain_background()
            self._trace.flush_to_disk()
        return result

    async def shutdown(self) -> None:
        await self._drain_background(cancel=True)

    # ── subagent surface ────────────────────────────────────────────────

    async def spawn_subagent(
        self,
        *,
        parent_run: Run,
        description: str,
        prompt: str,
        subagent_type: str,
        run_in_background: bool,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        st = self._subagent_types.get(subagent_type)
        if st is None:
            return {
                "ok": False,
                "error": f"unknown subagent_type {subagent_type!r}; known: "
                         f"{sorted(self._subagent_types)}",
            }

        child_model_name = st.model or self._default_cfg.model
        child_max_steps = st.max_steps or self._default_cfg.max_steps
        child_run = self._trace.start_subagent(
            parent_run_id=parent_run.run_id,
            subagent_type=subagent_type,
            agent_name=subagent_type,
            model={"provider": "openai-compat", "name": child_model_name},
            resource_limits={"max_steps": child_max_steps},
            launch_mode="background" if run_in_background else "foreground",
        )
        spawn_evt_id = self._trace.emit(
            parent_run,
            "subagent_spawn",
            phase="completed",
            payload={
                "child_run_id": child_run.run_id,
                "subagent_type": subagent_type,
                "description": description,
                "prompt": prompt,
                "launch_mode": "background" if run_in_background else "foreground",
            },
        )
        child_run.extra["source_event_id"] = spawn_evt_id

        registry = self._build_registry_for(child_run, allowlist=st.tools)
        child_cfg = AgentConfig(
            model=child_model_name,
            agent_name=subagent_type,
            max_steps=child_max_steps,
            max_tool_calls_per_step=self._default_cfg.max_tool_calls_per_step,
        )
        child_agent = Agent(
            cfg=child_cfg,
            llm=self._llm,
            tools=registry,
            trace=self._trace,
            run=child_run,
            instruction=prompt,
            system=st.system_prompt,
        )

        if run_in_background:
            task = asyncio.create_task(
                self._await_and_record(child_run.run_id, child_agent.run()),
                name=f"octopus-bg-{subagent_type}-{child_run.run_id[-6:]}",
            )
            self._background[child_run.run_id] = _Background(
                run_id=child_run.run_id, subagent_type=subagent_type, task=task,
            )
            return {
                "ok": True,
                "task_id": child_run.run_id,
                "status": "running_background",
                "subagent_type": subagent_type,
            }

        coro = child_agent.run()
        try:
            if timeout_s is not None and timeout_s > 0:
                result = await asyncio.wait_for(coro, timeout=timeout_s)
            else:
                result = await coro
        except asyncio.TimeoutError:
            self._trace.emit(
                child_run, "error", phase="error",
                payload={"error_type": "TimeoutError",
                         "message": f"foreground subagent exceeded {timeout_s}s"},
            )
            self._trace.finish_run(
                child_run, status="failed", stop_reason="timeout",
            )
            return {
                "ok": False,
                "task_id": child_run.run_id,
                "status": "timeout",
                "timeout_s": timeout_s,
            }
        except Exception as e:
            return {
                "ok": False,
                "task_id": child_run.run_id,
                "status": "failed",
                "error": f"{type(e).__name__}: {e}",
            }

        return {
            "ok": True,
            "task_id": child_run.run_id,
            "status": "completed",
            "subagent_type": subagent_type,
            "stop_reason": result.stop_reason,
            "output": result.final_message,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "wall_time_ms": result.wall_time_ms,
        }

    def get_background_status(self, task_id: str) -> dict[str, Any]:
        bg = self._background.get(task_id)
        if bg is None:
            return {"ok": False, "error": f"unknown task_id {task_id!r}"}
        if not bg.task.done():
            return {
                "ok": True, "task_id": task_id,
                "subagent_type": bg.subagent_type, "status": "running_background",
            }
        if task_id in self._failures:
            return {
                "ok": False, "task_id": task_id, "status": "failed",
                "subagent_type": bg.subagent_type, "error": self._failures[task_id],
            }
        result = self._results.get(task_id)
        if result is None:
            return {"ok": False, "task_id": task_id, "status": "unknown"}
        return self._format_done(task_id, bg.subagent_type, result)

    async def wait_background(self, task_id: str, timeout_s: float) -> dict[str, Any]:
        bg = self._background.get(task_id)
        if bg is None:
            return {"ok": False, "error": f"unknown task_id {task_id!r}"}
        try:
            await asyncio.wait_for(asyncio.shield(bg.task), timeout=max(0.0, timeout_s))
        except asyncio.TimeoutError:
            return {
                "ok": True, "task_id": task_id,
                "subagent_type": bg.subagent_type,
                "status": "running_background", "timed_out": True,
            }
        return self.get_background_status(task_id)

    # ── internals ───────────────────────────────────────────────────────

    async def _await_and_record(self, run_id: str, coro) -> AgentResult:
        try:
            result = await coro
        except Exception as e:
            self._failures[run_id] = f"{type(e).__name__}: {e}"
            raise
        self._results[run_id] = result
        return result

    async def _drain_background(self, *, cancel: bool = False) -> None:
        if not self._background:
            return
        tasks = [b.task for b in self._background.values() if not b.task.done()]
        if cancel:
            for t in tasks:
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _format_done(
        self, task_id: str, subagent_type: str, result: AgentResult
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "task_id": task_id,
            "subagent_type": subagent_type,
            "status": "completed",
            "stop_reason": result.stop_reason,
            "output": result.final_message,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "wall_time_ms": result.wall_time_ms,
        }

    def _build_registry_for(
        self, parent_run: Run, allowlist: list[str] | None
    ) -> ToolRegistry:
        registry = ToolRegistry()
        for t in self._base_tools:
            if allowlist is None or t.name in allowlist:
                registry.register(t)
        if self._subagent_types:
            for tool in _build_agent_tools(self, parent_run):
                # Always available to parents that have any subagent_types
                # configured. Use allowlist to opt-out: include "agent" in
                # allowlist explicitly to enable, or omit it to disable,
                # by passing allowlist=[...]. None = include.
                if allowlist is None or tool.name in allowlist:
                    registry.register(tool)
        return registry


# ── tool factories ─────────────────────────────────────────────────────


def _build_agent_tools(coord: Coordinator, parent_run: Run) -> list[Tool]:
    return [
        _agent_tool(coord, parent_run),
        _agent_status_tool(coord),
        _agent_wait_tool(coord),
    ]


def _agent_tool(coord: Coordinator, parent_run: Run) -> Tool:
    known = sorted(coord._subagent_types)

    async def run(args: dict[str, Any]) -> str:
        description = str(args.get("description") or "").strip()
        prompt = args.get("prompt")
        subagent_type = str(args.get("subagent_type") or "").strip()
        run_in_background = bool(args.get("run_in_background", False))
        timeout_s = args.get("timeout_s")
        if not prompt or not isinstance(prompt, str):
            return json.dumps({"ok": False, "error": "`prompt` is required"})
        if not subagent_type:
            return json.dumps({"ok": False, "error": "`subagent_type` is required"})
        if not description:
            description = subagent_type
        result = await coord.spawn_subagent(
            parent_run=parent_run,
            description=description,
            prompt=prompt,
            subagent_type=subagent_type,
            run_in_background=run_in_background,
            timeout_s=float(timeout_s) if timeout_s is not None else None,
        )
        return json.dumps(result, ensure_ascii=False)

    return Tool(
        name="agent",
        description=(
            "Delegate a sub-task to a specialized subagent. With "
            "run_in_background=false (default), this blocks until the "
            "subagent finishes and returns its final message. With "
            "run_in_background=true, returns a task_id immediately; query "
            "with `agent_status` or block with `agent_wait`. "
            f"Known subagent_types: {known}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string",
                                "description": "Short summary of the sub-task."},
                "prompt": {"type": "string",
                           "description": "Full instruction to send to the subagent."},
                "subagent_type": {"type": "string", "enum": known or [""]},
                "run_in_background": {"type": "boolean", "default": False},
                "timeout_s": {"type": "number",
                              "description": "Foreground timeout in seconds; ignored if background."},
            },
            "required": ["prompt", "subagent_type"],
        },
        fn=run,
    )


def _agent_status_tool(coord: Coordinator) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        task_id = args.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return json.dumps({"ok": False, "error": "`task_id` is required"})
        return json.dumps(coord.get_background_status(task_id), ensure_ascii=False)

    return Tool(
        name="agent_status",
        description=(
            "Check the status of a background subagent by task_id. Returns "
            "status: running_background | completed | failed."
        ),
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        fn=run,
    )


def _agent_wait_tool(coord: Coordinator) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        task_id = args.get("task_id")
        timeout_s = float(args.get("timeout_s") or 60.0)
        if not isinstance(task_id, str) or not task_id:
            return json.dumps({"ok": False, "error": "`task_id` is required"})
        result = await coord.wait_background(task_id, timeout_s=timeout_s)
        return json.dumps(result, ensure_ascii=False)

    return Tool(
        name="agent_wait",
        description=(
            "Block up to timeout_s seconds for a background subagent to "
            "finish. Returns the same shape as `agent_status`, with an "
            "extra `timed_out: true` flag if the timeout elapsed first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "timeout_s": {"type": "number", "default": 60.0},
            },
            "required": ["task_id"],
        },
        fn=run,
    )


def load_subagent_types(path: str) -> dict[str, SubagentType]:
    """Load SubagentType configs from a TOML file.

    Format:

        [[subagents]]
        name = "general"
        model = "openai/gpt-5"        # optional
        max_steps = 30                # optional
        system_prompt = "..."         # optional
        tools = ["Bash", "Read"]      # optional whitelist
    """
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

    from pathlib import Path

    data = tomllib.loads(Path(path).read_text())
    out: dict[str, SubagentType] = {}
    for entry in data.get("subagents", []):
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"subagent entry missing `name`: {entry!r}")
        out[name] = SubagentType(
            name=name,
            system_prompt=entry.get("system_prompt"),
            model=entry.get("model"),
            max_steps=entry.get("max_steps"),
            tools=entry.get("tools"),
        )
    return out
