"""Tool registry, builtin tools, and MCP tool proxies.

Each tool is a coroutine that takes a JSON-decoded arguments dict and
returns a string (what the LLM will see in the tool_result). All tools
share the same shape so dispatch is uniform.
"""

from __future__ import annotations

import asyncio
import fnmatch
import glob
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .mcp_client import StdioMCPClient

ToolFn = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    output: str
    status: str  # "completed" | "error"
    wall_time_ms: int


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def has(self, name: str) -> bool:
        return name in self._tools

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                output=f"unknown tool: {name}", status="error", wall_time_ms=0
            )
        t0 = time.monotonic()
        try:
            output = await tool.fn(arguments)
            return ToolResult(
                output=output,
                status="completed",
                wall_time_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ToolResult(
                output=f"tool {name!r} raised {type(e).__name__}: {e}",
                status="error",
                wall_time_ms=int((time.monotonic() - t0) * 1000),
            )


# ── Builtin tools ─────────────────────────────────────────────────────


def _bash_tool(cwd: Path, default_timeout_s: float) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError("`command` must be a non-empty string")
        timeout_s = float(args.get("timeout_s") or default_timeout_s)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return json.dumps(
                {"timed_out": True, "timeout_s": timeout_s, "exit_code": None}
            )
        return json.dumps(
            {
                "exit_code": proc.returncode,
                "stdout": _truncate(stdout_b.decode("utf-8", errors="replace")),
                "stderr": _truncate(stderr_b.decode("utf-8", errors="replace")),
            },
            ensure_ascii=False,
        )

    return Tool(
        name="Bash",
        description=(
            "Run a shell command in the agent's working directory. "
            "Returns JSON {exit_code, stdout, stderr} or {timed_out: true}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_s": {"type": "number"},
            },
            "required": ["command"],
        },
        fn=run,
    )


def _read_tool(cwd: Path) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        path = _resolve(cwd, args.get("path"))
        offset = int(args.get("offset") or 0)
        limit = int(args.get("limit") or 2000)
        text = path.read_text(errors="replace")
        lines = text.splitlines()
        sliced = lines[offset : offset + limit]
        return "\n".join(f"{offset + i + 1}\t{line}" for i, line in enumerate(sliced))

    return Tool(
        name="Read",
        description="Read a text file with optional line offset/limit.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
        fn=run,
    )


def _write_tool(cwd: Path) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        path = _resolve(cwd, args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("`content` must be a string")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return json.dumps({"ok": True, "bytes_written": len(content.encode("utf-8"))})

    return Tool(
        name="Write",
        description="Overwrite a file with the given content.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        fn=run,
    )


def _glob_tool(cwd: Path) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        pattern = args.get("pattern")
        if not isinstance(pattern, str):
            raise ValueError("`pattern` must be a string")
        # Resolve relative patterns against cwd.
        if not pattern.startswith("/"):
            pattern = str(cwd / pattern)
        matches = glob.glob(pattern, recursive=True)
        return json.dumps(matches[:1000], ensure_ascii=False)

    return Tool(
        name="Glob",
        description="Glob filesystem matches; supports recursive `**`.",
        parameters={
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
        fn=run,
    )


def _edit_tool(cwd: Path) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        path = _resolve(cwd, args.get("path"))
        old = args.get("old_string")
        new = args.get("new_string")
        replace_all = bool(args.get("replace_all", False))
        if not isinstance(old, str) or old == "":
            raise ValueError("`old_string` must be a non-empty string")
        if not isinstance(new, str):
            raise ValueError("`new_string` must be a string")
        text = path.read_text(errors="replace")
        count = text.count(old)
        if count == 0:
            raise ValueError(f"no occurrence of old_string in {path}")
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_string occurs {count} times in {path}; "
                "pass replace_all=true or extend the match to be unique"
            )
        replaced = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        path.write_text(replaced)
        return json.dumps({"ok": True, "replacements": count if replace_all else 1})

    return Tool(
        name="Edit",
        description=(
            "In-place edit a file by replacing `old_string` with `new_string`. "
            "By default expects exactly one match (errors otherwise); pass "
            "replace_all=true to replace every occurrence."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
            },
            "required": ["path", "old_string", "new_string"],
        },
        fn=run,
    )


_GREP_DEFAULT_IGNORE_DIRS = {".git", ".hg", ".svn", "node_modules",
                             "__pycache__", ".venv", ".tox", "dist", "build"}
_GREP_MAX_FILE_BYTES = 1_000_000


def _grep_tool(cwd: Path) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("`pattern` must be a non-empty string")
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e
        root = _resolve(cwd, args.get("path") or ".")
        if not root.exists():
            raise ValueError(f"path does not exist: {root}")
        include = args.get("include")
        max_results = int(args.get("max_results") or 200)
        max_per_file = int(args.get("max_per_file") or 50)

        # Walk synchronously but yield to the loop occasionally so a
        # huge tree doesn't block the event loop indefinitely.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _grep_blocking,
            root, rx, include, max_results, max_per_file,
        )

    return Tool(
        name="Grep",
        description=(
            "Search for a regex pattern across files. Returns JSON "
            "{matches: [{file, line, text}, ...], truncated: bool}. "
            "Skips common build / VCS directories."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string",
                         "description": "Directory or file to search; default cwd."},
                "include": {"type": "string",
                            "description": "Glob filter on filenames, e.g. '*.py'."},
                "ignore_case": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "default": 200},
                "max_per_file": {"type": "integer", "default": 50},
            },
            "required": ["pattern"],
        },
        fn=run,
    )


def _grep_blocking(root: Path, rx: re.Pattern[str], include: str | None,
                   max_results: int, max_per_file: int) -> str:
    matches: list[dict[str, Any]] = []
    truncated = False

    def consider_file(path: Path) -> bool:
        nonlocal truncated
        try:
            size = path.stat().st_size
        except OSError:
            return True
        if size > _GREP_MAX_FILE_BYTES:
            return True
        try:
            with path.open("r", errors="replace") as f:
                in_file = 0
                for lineno, line in enumerate(f, 1):
                    if rx.search(line):
                        matches.append({
                            "file": str(path),
                            "line": lineno,
                            "text": line.rstrip("\n")[:500],
                        })
                        in_file += 1
                        if len(matches) >= max_results:
                            truncated = True
                            return False
                        if in_file >= max_per_file:
                            break
        except OSError:
            pass
        return True

    if root.is_file():
        consider_file(root)
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _GREP_DEFAULT_IGNORE_DIRS]
            for fname in filenames:
                if include and not fnmatch.fnmatch(fname, include):
                    continue
                if not consider_file(Path(dirpath) / fname):
                    break
            if truncated:
                break

    return json.dumps(
        {"matches": matches, "truncated": truncated, "count": len(matches)},
        ensure_ascii=False,
    )


def builtin_tools(*, cwd: Path, bash_timeout_s: float = 600.0) -> list[Tool]:
    return [
        _bash_tool(cwd, bash_timeout_s),
        _read_tool(cwd),
        _write_tool(cwd),
        _edit_tool(cwd),
        _glob_tool(cwd),
        _grep_tool(cwd),
    ]


# ── MCP proxy tool ────────────────────────────────────────────────────


def mcp_proxy_tool(client: StdioMCPClient, raw_name: str, qualified_name: str,
                   description: str, parameters: dict[str, Any]) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        result = await client.call_tool(raw_name, args)
        if result["is_error"]:
            return json.dumps({"is_error": True, "text": result["text"]})
        return result["text"]

    return Tool(
        name=qualified_name,
        description=description or f"MCP tool: {qualified_name}",
        parameters=parameters or {"type": "object", "properties": {}},
        fn=run,
    )


# ── helpers ───────────────────────────────────────────────────────────


def _resolve(cwd: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("`path` must be a non-empty string")
    p = Path(raw)
    if not p.is_absolute():
        p = cwd / p
    return p


def _truncate(s: str, limit: int = 64 * 1024) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…[truncated {len(s) - limit} bytes]"
