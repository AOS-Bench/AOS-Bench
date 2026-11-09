"""CLI entry: python -m aos_bench.harness.claude_code --solo."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .core import orchestrator
from .core.budget import ResourceBudget
from .engines.claude.engine import ClaudeEngine


def _int_env(key: str, default: int) -> int:
    """int() of an env var, treating an unset OR empty value as the default.

    harbor forwards knobs via ``--ae KEY=VALUE``; an unset smoke override lands
    as ``KEY=`` (empty string), so ``int(os.environ.get(KEY, "24000"))`` would
    raise on ``int("")``. This collapses empty -> default."""
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run a single Claude agent against an AOS aos_pool stream."
    )
    p.add_argument("--pool-url", required=True, help="aos_pool MCP HTTP url.")
    p.add_argument(
        "--worker-model", default=os.environ.get("CLAUDE_TEAM_WORKER_MODEL", "sonnet")
    )
    p.add_argument(
        "--instruction-file",
        required=True,
        help="File with the AOS STREAM_PREAMBLE (agent mission).",
    )
    p.add_argument("--cwd", default="/workspace")
    p.add_argument("--log-dir", default="/logs/agent")
    p.add_argument(
        "--worker-max-turns",
        type=int,
        default=_int_env("CLAUDE_TEAM_WORKER_MAX_TURNS", 200),
    )
    p.add_argument(
        "--lead-nudge-cap",
        type=int,
        default=_int_env("CLAUDE_TEAM_LEAD_NUDGE_CAP", 8),
        help="Cap on driver-loop nudge continuations.",
    )
    p.add_argument(
        "--cpu-budget",
        type=int,
        default=_int_env("CLAUDE_TEAM_CPU_BUDGET", 0),
        help="CPU-core budget (0 = read live container).",
    )
    p.add_argument(
        "--mem-budget-mb",
        type=int,
        default=_int_env("CLAUDE_TEAM_MEM_BUDGET_MB", 0),
        help="Memory budget MB (0 = read live container).",
    )
    p.add_argument(
        "--solo",
        action="store_true",
        default=os.environ.get("CLAUDE_TEAM_SOLO", "") in ("1", "true", "yes"),
        help="No-team baseline: ONE agent self-claims + solves (the only mode).",
    )
    return p


def resolve_budget(cpus: int | None, mem_mb: int | None) -> ResourceBudget:
    return ResourceBudget.from_args(cpus, mem_mb)


def _config_root_factory(log_dir: Path):
    config_root = log_dir / "sessions"

    def factory(name: str) -> Path:
        return config_root / name

    return factory


def main() -> None:
    args = build_parser().parse_args()
    stream_preamble = Path(args.instruction_file).read_text()
    engine = ClaudeEngine(config_root_factory=_config_root_factory(Path(args.log_dir)))
    budget = resolve_budget(args.cpu_budget, args.mem_budget_mb)

    results = asyncio.run(
        orchestrator.run_solo(
            engine=engine,
            pool_mcp_config={"type": "http", "url": args.pool_url},
            model=args.worker_model,
            stream_preamble=stream_preamble,
            cwd=args.cwd,
            log_dir=Path(args.log_dir),
            max_turns=args.worker_max_turns,
            nudge_cap=args.lead_nudge_cap,
            budget=budget,
        )
    )
    print(json.dumps(results, default=str))
