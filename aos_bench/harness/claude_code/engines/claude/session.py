"""Claude session: a long-lived ClaudeSDKClient driven one turn at a time."""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from ...core.coordinator import Coordinator
from ..base import TurnResult


def _truncate(obj: Any, n: int = 600) -> Any:
    s = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    return s[:n]


def _result_has_stream_done(content: Any) -> bool:
    """Detect the aos_pool ``stream_done: true`` signal in a tool-result block.

    ``content`` may be a string, or a list of ``{type:'text', text:...}`` blocks
    (SDK shape). We look for ``stream_done`` paired with ``true`` in the raw text
    — robust to whitespace/quoting variants the pool may emit."""
    if isinstance(content, list):
        text = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    else:
        text = content if isinstance(content, str) else str(content or "")
    if "stream_done" not in text:
        return False
    lowered = text.lower().replace(" ", "").replace('"', "").replace("'", "")
    return "stream_done:true" in lowered


class ClaudeSession:
    def __init__(self, options: ClaudeAgentOptions, coord: Coordinator, who: str):
        self._options = options
        self._coord = coord
        self._who = who
        self._client: ClaudeSDKClient | None = None

    async def __aenter__(self) -> "ClaudeSession":
        self._client = ClaudeSDKClient(options=self._options)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        assert self._client is not None
        await self._client.__aexit__(*exc)

    async def query(self, text: str) -> None:
        assert self._client is not None
        await self._client.query(text)

    async def drain_turn(self) -> TurnResult:
        assert self._client is not None
        coord, who = self._coord, self._who
        result = TurnResult()
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        result.texts.append(b.text)
                        coord._log("assistant_text", who=who, text=b.text[:2000])
                    elif isinstance(b, ToolUseBlock):
                        coord._log(
                            "tool_use", who=who, name=b.name, input=_truncate(b.input)
                        )
                    elif isinstance(b, ToolResultBlock):
                        coord._log("tool_result", who=who, tool_use_id=b.tool_use_id)
                        if _result_has_stream_done(b.content):
                            result.stream_done_seen = True
            elif isinstance(msg, SystemMessage):
                pass
            elif isinstance(msg, ResultMessage):
                result.usage = {
                    "subtype": msg.subtype,
                    "num_turns": msg.num_turns,
                    "duration_ms": msg.duration_ms,
                    "total_cost_usd": msg.total_cost_usd,
                    "usage": msg.usage,
                }
                coord._log("result", who=who, **result.usage)
        return result
