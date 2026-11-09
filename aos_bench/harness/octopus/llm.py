"""Thin OpenAI-compatible chat client wrapper.

We don't try to abstract over multiple providers. We just take an
OpenAI-shaped chat-completions endpoint (stock OpenAI, OpenRouter,
local sglang/vLLM, LiteLLM proxy …) and call it.

The async ``openai`` SDK is required at runtime; we import lazily so
test code that mocks the LLM layer does not need ``openai`` installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    raw: dict[str, Any]


class LLMClient(Protocol):
    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> LLMResponse: ...


class OpenAICompatClient:
    """OpenAI-compatible chat client built on the async ``openai`` SDK."""

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str,
        timeout_s: float = 600.0,
    ) -> None:
        # Lazy import keeps the test path import-free.
        from openai import AsyncOpenAI  # type: ignore[import-not-found]

        self._client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_s
        )

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await self._client.chat.completions.create(**kwargs)
        return _parse_response(resp)


def _parse_response(resp: Any) -> LLMResponse:
    """Convert an openai ChatCompletion object to our ``LLMResponse``."""
    import json

    choice = resp.choices[0]
    msg = choice.message
    tool_calls: list[ToolCall] = []
    for tc in (getattr(msg, "tool_calls", None) or []):
        raw_args = tc.function.arguments
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {"_raw": raw_args}
        tool_calls.append(
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=args or {},
            )
        )
    usage = getattr(resp, "usage", None)
    return LLMResponse(
        content=getattr(msg, "content", None),
        tool_calls=tool_calls,
        finish_reason=getattr(choice, "finish_reason", None),
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        raw=resp.model_dump() if hasattr(resp, "model_dump") else {},
    )
