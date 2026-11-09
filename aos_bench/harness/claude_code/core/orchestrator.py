"""Engine-agnostic orchestration: the solo baseline driver loop.

The driver loop (query -> drain_turn -> nudge until done) lives here and calls
into an ``AgentEngine`` for the actual session.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..engines.base import AgentEngine
from . import prompts
from .budget import ResourceBudget
from .coordinator import Coordinator


async def run_solo(
    *,
    engine: AgentEngine,
    pool_mcp_config: dict[str, Any],
    model: str,
    stream_preamble: str,
    cwd: str,
    log_dir: Path,
    max_turns: int,
    nudge_cap: int,
    budget: ResourceBudget,
) -> dict[str, Any]:
    coord = Coordinator(members=[], log_dir=log_dir)
    start = time.time()
    coord._log(
        "solo_start",
        model=model,
        pool=pool_mcp_config.get("type"),
        cpu_budget=budget.cpus,
        mem_budget_mb=budget.mem_mb,
    )
    usage: dict[str, Any] = {}
    try:
        session = engine.make_session(
            role="solo",
            name="solo",
            system_prompt=prompts.solo_system_prompt(
                cpus=budget.cpus, mem_mb=budget.mem_mb
            ),
            cwd=cwd,
            model=model,
            max_turns=max_turns,
            coord=coord,
            budget=None,
            can_spawn=False,
            pool_mcp_config=pool_mcp_config,
        )
        async with session:
            await session.query(prompts.solo_first_message(stream_preamble))
            nudges = 0
            while True:
                tr = await session.drain_turn()
                usage = tr.usage or usage
                done = any(prompts.SOLO_DONE_SENTINEL in t for t in tr.texts)
                if done or nudges >= nudge_cap:
                    break
                nudges += 1
                await session.query(
                    "Continue: call mcp__aos_pool__todo_next; do the task yourself; "
                    "report with todo_update. If stream_done and all reported, reply "
                    f"{prompts.SOLO_DONE_SENTINEL} and stop."
                )
    finally:
        coord._log("solo_end", wall_time_sec=round(time.time() - start, 2))
    config_root = log_dir / "sessions"
    for creds in config_root.glob("*/.credentials.json"):
        try:
            creds.unlink()
        except OSError:
            pass
    result = {"solo": usage, "wall_time_sec": round(time.time() - start, 2)}
    (log_dir / "team_result.json").write_text(json.dumps(result, indent=2, default=str))
    coord.close()
    return result
