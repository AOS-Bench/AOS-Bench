"""
Minimal hello-world to verify claude-agent-sdk works with local proxy.

Usage:
    ANTHROPIC_API_KEY=<key> python3 aos_bench/runner/hello_sdk.py
"""

import asyncio
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)


async def main() -> None:
    options = ClaudeAgentOptions(
        max_turns=3,
        permission_mode="bypassPermissions",
        env={
            "ANTHROPIC_BASE_URL": os.environ.get(
                "ANTHROPIC_BASE_URL", "http://127.0.0.1:4000"
            ),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
            "IS_SANDBOX": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        },
    )

    print("=== Single-turn query ===")
    async for msg in query(prompt="Say 'hello world' and nothing else.", options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if hasattr(block, "text"):
                    print(f"[assistant] {block.text}")
        elif isinstance(msg, ResultMessage):
            print(f"[result] turns={msg.num_turns} stop={msg.stop_reason}")


async def multi_turn_demo() -> None:
    """Inject a second message after turn 1 completes."""
    turn_count = 0

    def _user_msg(text: str) -> dict:
        return {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
        }

    async def message_stream():
        nonlocal turn_count
        yield _user_msg("Count from 1 to 3, one number per line.")
        # wait briefly to simulate step-triggered injection
        await asyncio.sleep(0.5)
        yield _user_msg("Now repeat the same list in reverse.")

    options = ClaudeAgentOptions(
        max_turns=6,
        permission_mode="bypassPermissions",
        env={
            "ANTHROPIC_BASE_URL": os.environ.get(
                "ANTHROPIC_BASE_URL", "http://127.0.0.1:4000"
            ),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
            "IS_SANDBOX": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        },
    )

    print("\n=== Multi-turn injection demo ===")
    async for msg in query(prompt=message_stream(), options=options):
        if isinstance(msg, AssistantMessage):
            turn_count += 1
            for block in msg.content:
                if hasattr(block, "text"):
                    print(f"[turn {turn_count}] {block.text[:120]}")
        elif isinstance(msg, ResultMessage):
            print(f"[result] turns={msg.num_turns} stop={msg.stop_reason}")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(multi_turn_demo())
