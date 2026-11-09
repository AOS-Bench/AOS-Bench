"""Octopus CLI.

    python -m aos_bench.harness.octopus run \\
        --model openrouter/z-ai/glm-4.7 \\
        --base-url https://openrouter.ai/api/v1 \\
        --api-key-env OPENROUTER_API_KEY \\
        --cwd /workspace \\
        --prompt-file aos_bench/harness/octopus/prompts/stream_preamble.md \\
        --mcp aos_pool=python3,-m,aos_bench.pool.server,--schedule,/sandbox/aos/schedule.json,--state-dir,/sandbox/aos/state \\
        --subagents aos_bench/harness/octopus/prompts/subagents.toml \\
        --trace-out /logs/agent/agent_trace.json \\
        --event-log /logs/agent/events.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .agent import AgentConfig
from .coordinator import Coordinator, load_subagent_types
from .llm import OpenAICompatClient
from .mcp_client import StdioMCPClient, parse_mcp_spec
from .tools import builtin_tools, mcp_proxy_tool
from .trace import TraceWriter


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", required=True, help="Model name (e.g. openrouter/z-ai/glm-4.7)")
    p.add_argument("--base-url", default=None, help="OpenAI-compatible base URL")
    p.add_argument("--api-key", default=None, help="API key (avoid; prefer --api-key-env)")
    p.add_argument("--api-key-env", default="OPENAI_API_KEY",
                   help="Env var to read the API key from (default: OPENAI_API_KEY)")
    p.add_argument("--cwd", default=".", help="Working directory for tools")
    p.add_argument("--prompt", default=None, help="Inline initial user message")
    p.add_argument("--prompt-file", default=None, help="Path to file with initial user message")
    p.add_argument("--system", default=None, help="Inline system prompt")
    p.add_argument("--system-file", default=None, help="Path to file with system prompt")
    p.add_argument("--mcp", action="append", default=[],
                   help="Register an MCP server: NAME=ARG1,ARG2,... (repeatable)")
    p.add_argument("--subagents", default=None,
                   help="Path to a TOML file defining subagent types. Enables the "
                        "`agent` / `agent_status` / `agent_wait` tools.")
    p.add_argument("--max-steps", type=int, default=50)
    p.add_argument("--bash-timeout-s", type=float, default=600.0)
    p.add_argument("--llm-timeout-s", type=float, default=600.0)
    p.add_argument("--mcp-init-timeout-s", type=float, default=30.0)
    p.add_argument("--agent-name", default="octopus-default")
    p.add_argument("--trace-out", default=None, help="Path to write final agent_trace.json")
    p.add_argument("--event-log", default=None, help="Path to append live JSONL events")
    p.add_argument("--no-builtin-tools", action="store_true",
                   help="Disable bash/read/write/edit/glob/grep (only expose MCP tools)")
    p.add_argument("--resource-sampling-interval-s", type=float, default=1.0,
                   help="Sample CPU/RSS every N seconds; 0 to disable.")
    p.add_argument("--verbose", "-v", action="store_true")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="octopus",
        description="AOS-Bench's lightweight, observable canary agent harness.",
    )
    p.add_argument("--version", action="version", version=f"octopus {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="Run a single agent session")
    _add_run_args(run_p)

    args = p.parse_args(argv)
    if args.cmd != "run":
        p.error(f"unknown subcommand: {args.cmd}")
        return 2

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    instruction = _read_text(args.prompt, args.prompt_file, "prompt")
    system = _read_text(args.system, args.system_file, "system", required=False)
    api_key = args.api_key or os.environ.get(args.api_key_env, "")
    cwd = Path(args.cwd).resolve()
    if not cwd.exists():
        print(f"--cwd does not exist: {cwd}", file=sys.stderr)
        return 2

    trace = TraceWriter(
        agent_system={
            "name": "octopus",
            "version": __version__,
            "default_model": args.model,
            "trace_source": "native",
        },
        event_log_path=Path(args.event_log) if args.event_log else None,
        trace_out_path=Path(args.trace_out) if args.trace_out else None,
    )

    try:
        return asyncio.run(
            _run_async(
                args=args,
                instruction=instruction,
                system=system,
                api_key=api_key,
                cwd=cwd,
                trace=trace,
            )
        )
    finally:
        trace.close()


async def _run_async(*, args, instruction, system, api_key, cwd, trace) -> int:
    base_tools = []
    if not args.no_builtin_tools:
        base_tools.extend(builtin_tools(cwd=cwd, bash_timeout_s=args.bash_timeout_s))

    mcp_clients: list[StdioMCPClient] = []
    coord: Coordinator | None = None
    try:
        for spec in args.mcp:
            name, argv = parse_mcp_spec(spec)
            client = StdioMCPClient(name, argv, cwd=str(cwd))
            await client.start(init_timeout_s=args.mcp_init_timeout_s)
            mcp_clients.append(client)
            for tool in await client.list_tools():
                base_tools.append(
                    mcp_proxy_tool(
                        client,
                        raw_name=tool.name,
                        qualified_name=tool.qualified_name,
                        description=tool.description,
                        parameters=tool.input_schema,
                    )
                )

        subagent_types = (
            load_subagent_types(args.subagents) if args.subagents else {}
        )

        llm = OpenAICompatClient(
            base_url=args.base_url,
            api_key=api_key,
            timeout_s=args.llm_timeout_s,
        )

        coord = Coordinator(
            trace=trace,
            llm=llm,
            default_cfg=AgentConfig(
                model=args.model,
                agent_name=args.agent_name,
                max_steps=args.max_steps,
            ),
            base_tools=base_tools,
            subagent_types=subagent_types,
            resource_sampling_interval_s=(
                args.resource_sampling_interval_s
                if args.resource_sampling_interval_s > 0 else None
            ),
        )
        result = await coord.run_root(instruction=instruction, system=system)
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    finally:
        if coord is not None:
            await coord.shutdown()
        for c in mcp_clients:
            try:
                await c.close()
            except Exception:
                pass


def _read_text(inline: str | None, path: str | None, label: str, required: bool = True) -> str | None:
    if inline and path:
        raise SystemExit(f"--{label} and --{label}-file are mutually exclusive")
    if inline is not None:
        return inline
    if path is not None:
        return Path(path).read_text()
    if required:
        raise SystemExit(f"either --{label} or --{label}-file is required")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
