"""Tiny stdio MCP client.

We speak just enough JSON-RPC to:

  1. spawn a stdio MCP server subprocess
  2. ``initialize`` and send ``notifications/initialized``
  3. ``tools/list`` to discover its tools
  4. ``tools/call`` on demand
  5. shut it down cleanly

Skipping: resources, prompts, sampling, completion, log subscriptions,
notification streams. If a server requires those, use the upstream
``mcp`` SDK instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = "2024-11-05"


@dataclass
class MCPTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        return f"{self.server}__{self.name}"


class MCPClientError(Exception):
    pass


class StdioMCPClient:
    """One subprocess + one async JSON-RPC pump."""

    def __init__(self, server_name: str, argv: list[str], cwd: str | None = None) -> None:
        self.server_name = server_name
        self._argv = argv
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()

    async def start(self, *, init_timeout_s: float = 30.0) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=os.environ.copy(),
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await asyncio.wait_for(
                self._request(
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "octopus", "version": "0.1.0"},
                    },
                ),
                timeout=init_timeout_s,
            )
        except Exception as e:
            await self.close()
            raise MCPClientError(
                f"MCP server {self.server_name!r} failed to initialize: {e}"
            ) from e
        await self._notify("notifications/initialized", {})

    async def list_tools(self) -> list[MCPTool]:
        result = await self._request("tools/list", {})
        tools: list[MCPTool] = []
        for t in result.get("tools", []):
            tools.append(
                MCPTool(
                    server=self.server_name,
                    name=t["name"],
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {"type": "object"}),
                )
            )
        return tools

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._request(
            "tools/call", {"name": tool_name, "arguments": arguments}
        )
        # MCP tool result shape: {"content": [{"type": "text", "text": "..."}],
        # "isError": bool?}. Flatten to a single string for the LLM.
        parts: list[str] = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(json.dumps(c, ensure_ascii=False))
        return {
            "text": "\n".join(parts),
            "is_error": bool(result.get("isError", False)),
            "raw": result,
        }

    async def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.returncode is None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        finally:
            for t in (self._reader_task, self._stderr_task):
                if t is not None:
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            self._proc = None

    # ── internals ──────────────────────────────────────────────────────

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None:
            raise MCPClientError(f"MCP {self.server_name}: not started")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        result = await fut
        if "error" in result:
            err = result["error"]
            raise MCPClientError(
                f"MCP {self.server_name} {method!r}: "
                f"{err.get('code')} {err.get('message')}"
            )
        return result.get("result", {})

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._send_lock:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        while True:
            line = await stream.readline()
            if not line:
                # server closed stdout
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(
                            MCPClientError(f"MCP {self.server_name}: stdout EOF")
                        )
                self._pending.clear()
                return
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue
            mid = msg.get("id")
            if mid is None:
                # server-initiated notification — ignored in M1
                continue
            fut = self._pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        # Just drain; we don't surface server logs in M1 (could tee to a
        # file later for observability).
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return


def parse_mcp_spec(spec: str) -> tuple[str, list[str]]:
    """Parse ``--mcp name=arg1,arg2,arg3`` into (name, argv).

    Commas inside arguments can be escaped with a backslash. For shell
    quoting parity, fall back to ``shlex.split`` if the right-hand side
    does not contain a comma.
    """
    if "=" not in spec:
        raise ValueError(f"--mcp must be NAME=ARGV (got: {spec!r})")
    name, rhs = spec.split("=", 1)
    name = name.strip()
    if not name:
        raise ValueError(f"--mcp NAME must be non-empty (got: {spec!r})")
    if "," in rhs:
        argv = [a.replace("\\,", ",") for a in _split_unescaped_commas(rhs)]
    else:
        argv = shlex.split(rhs)
    if not argv:
        raise ValueError(f"--mcp ARGV must be non-empty (got: {spec!r})")
    return name, argv


def _split_unescaped_commas(s: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == ",":
            cur.append(",")
            i += 2
            continue
        if ch == ",":
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out
