"""ClaudeEngine: builds ClaudeSDKClient-backed sessions wired to the pool MCP."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import ClaudeAgentOptions

from ...core.budget import ResourceBudget
from ...core.coordinator import Coordinator
from ..base import AgentSession
from .env import base_env
from .session import ClaudeSession


class ClaudeEngine:
    """Claude Agent SDK engine. ``config_root_factory(name)`` -> per-session
    config dir Path (or None to use defaults)."""

    def __init__(self, config_root_factory: Callable[[str], Path | None]):
        self._config_root_factory = config_root_factory

    def make_session(
        self,
        *,
        role: str,
        name: str,
        system_prompt: str,
        cwd: str,
        model: str,
        max_turns: int,
        coord: Coordinator,
        budget: ResourceBudget | None,
        can_spawn: bool,
        pool_mcp_config: dict[str, Any] | None,
        can_plan: bool = False,
        max_thinking_tokens: int | None = None,
    ) -> AgentSession:
        mcp_servers: dict[str, Any] = {}
        if pool_mcp_config is not None:
            mcp_servers["aos_pool"] = pool_mcp_config
        # Solo sessions self-claim from aos_pool and need no team MCP.
        cfg_dir = self._config_root_factory(name)
        opt_kwargs: dict[str, Any] = {}
        if max_thinking_tokens is not None:
            opt_kwargs["max_thinking_tokens"] = max_thinking_tokens
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt,
            cwd=cwd,
            permission_mode="bypassPermissions",
            max_turns=max_turns,
            mcp_servers=mcp_servers,
            env=base_env(cfg_dir) if cfg_dir is not None else {},
            setting_sources=[],
            **opt_kwargs,
        )
        return ClaudeSession(options, coord, name)
