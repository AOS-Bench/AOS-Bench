"""Engine seam: the minimal contract the orchestrator needs from any engine.

An engine builds long-lived agent *sessions* (lead / worker / solo) and drives
them one assistant turn at a time. ``claude`` is implemented; ``codex`` is a
documented stub (engines/codex/README.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

Usage = dict[str, Any]


@dataclass
class TurnResult:
    """Result of consuming one assistant turn."""

    usage: Usage = field(default_factory=dict)
    texts: list[str] = field(default_factory=list)
    # True if any tool RESULT this turn reported the pool's `stream_done`. Lets
    # the wfsdk top-up guard distinguish "worker stopped because the stream is
    # drained" from "worker stopped because it finished its one task" — only the
    # former should halt launching replacement workers.
    stream_done_seen: bool = False


@runtime_checkable
class AgentSession(Protocol):
    async def __aenter__(self) -> "AgentSession": ...
    async def __aexit__(self, *exc: Any) -> None: ...
    async def query(self, text: str) -> None: ...
    async def drain_turn(self) -> TurnResult: ...


@runtime_checkable
class AgentEngine(Protocol):
    def make_session(
        self,
        *,
        role: str,
        name: str,
        system_prompt: str,
        cwd: str,
        model: str,
        max_turns: int,
        coord: Any,
        budget: Any,
        can_spawn: bool,
        pool_mcp_config: dict[str, Any] | None,
        can_plan: bool = False,
        max_thinking_tokens: int | None = None,
    ) -> AgentSession: ...
