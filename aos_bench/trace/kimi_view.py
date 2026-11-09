"""Render a kimi-cli wire JSONL log into a human-readable per-step view.

Kimi streams ``ContentPart`` (assistant text) and ``ToolCallPart`` (tool
argument JSON) one tiny fragment at a time. Reading the raw log makes
your eyes bleed. This tool groups them by step so you see one message
+ one tool call + one tool result per turn, in order.

Usage:
    python3 -m aos_bench.trace.kimi_view <path/to/kimi-cli.txt>
    python3 -m aos_bench.trace.kimi_view <path> --json   # structured
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class _PendingTool:
    call_id: str
    name: str
    args_buf: str = ""


@dataclass
class _Step:
    n: int
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    token_usage: dict[str, Any] | None = None
    pending: _PendingTool | None = None

    def finalize_pending(self) -> None:
        if self.pending is None:
            return
        try:
            args = json.loads(self.pending.args_buf) if self.pending.args_buf else {}
        except json.JSONDecodeError:
            args = {"_raw": self.pending.args_buf}
        self.tool_calls.append(
            {"id": self.pending.call_id, "name": self.pending.name, "args": args}
        )
        self.pending = None


def _iter_events(path: Path):
    """Yield wire event payloads, tolerating multi-line JSON-RPC messages."""
    buffer = ""
    for line in path.read_text().splitlines():
        if line.lstrip().startswith('{"jsonrpc"'):
            if buffer:
                yield from _parse(buffer)
            buffer = line
        elif buffer:
            buffer += "\n" + line
        try:
            json.loads(buffer, strict=False)
        except json.JSONDecodeError:
            continue
        yield from _parse(buffer)
        buffer = ""
    if buffer:
        yield from _parse(buffer)


def _parse(raw: str):
    try:
        msg = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        return
    if msg.get("method") == "event":
        yield ("event", msg.get("params", {}))
    elif "id" in msg and "method" not in msg:
        yield ("response", msg)


def group_into_steps(path: Path) -> tuple[list[_Step], dict[str, Any] | None]:
    steps: list[_Step] = []
    cur: _Step | None = None
    response: dict[str, Any] | None = None
    initial_user: str | None = None

    for kind, payload in _iter_events(path):
        if kind == "response":
            response = payload
            continue
        etype = payload.get("type")
        body = payload.get("payload", {}) or {}

        if etype == "TurnBegin":
            initial_user = body.get("user_input")
            continue

        if etype == "StepBegin":
            if cur is not None:
                cur.finalize_pending()
                steps.append(cur)
            cur = _Step(n=body.get("n", len(steps) + 1))
            continue

        if cur is None:
            continue

        if etype == "ContentPart":
            t = body.get("type")
            if t == "text":
                cur.text += body.get("text", "")
            elif t == "think":
                cur.reasoning += body.get("think", "")
        elif etype == "ToolCall":
            cur.finalize_pending()
            fn = body.get("function", {})
            cur.pending = _PendingTool(
                call_id=body.get("id", ""),
                name=fn.get("name", ""),
                args_buf=fn.get("arguments", "") or "",
            )
        elif etype == "ToolCallPart":
            if cur.pending is not None:
                cur.pending.args_buf += body.get("arguments_part") or ""
        elif etype == "ToolResult":
            cur.finalize_pending()
            cur.tool_results[body.get("tool_call_id", "")] = body.get("return_value")
        elif etype == "StepInterrupted":
            cur.interrupted = True
        elif etype == "StatusUpdate":
            cur.token_usage = body.get("token_usage")
        elif etype == "TurnEnd":
            cur.finalize_pending()
            steps.append(cur)
            cur = None

    if cur is not None:
        cur.finalize_pending()
        steps.append(cur)

    meta: dict[str, Any] = {}
    if initial_user is not None:
        meta["initial_user_input"] = initial_user
    if response is not None:
        if "result" in response:
            meta["final_response"] = response["result"]
        elif "error" in response:
            meta["final_error"] = response["error"]
    return steps, meta or None


def _short_args(args: Any, max_chars: int = 240) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= max_chars else s[:max_chars] + "…"


def _short_result(value: Any, max_chars: int = 600) -> str:
    if isinstance(value, dict):
        out = value.get("output")
        if out is not None:
            if isinstance(out, list):
                # ToolResult.output is sometimes a list of {type,text,...}
                parts = [p.get("text", str(p)) if isinstance(p, dict) else str(p) for p in out]
                s = "\n".join(parts)
            else:
                s = str(out)
            msg = value.get("message") or ""
            if msg and msg != "Command executed successfully.":
                s = f"{s}\n[message] {msg}" if s else msg
            if value.get("is_error"):
                s = f"[ERROR] {s}"
            return s if len(s) <= max_chars else s[:max_chars] + "…"
    s = json.dumps(value, ensure_ascii=False)
    return s if len(s) <= max_chars else s[:max_chars] + "…"


def render_markdown(steps: list[_Step], meta: dict[str, Any] | None) -> str:
    out: list[str] = []
    if meta and meta.get("initial_user_input"):
        out.append("# Initial user input\n")
        text = meta["initial_user_input"]
        if isinstance(text, list):
            text = json.dumps(text, ensure_ascii=False, indent=2)
        out.append("```")
        out.append(text.rstrip())
        out.append("```\n")

    for s in steps:
        header = f"## Step {s.n}"
        if s.interrupted:
            header += "  (interrupted)"
        out.append(header)
        if s.reasoning.strip():
            out.append("**reasoning:**")
            out.append("> " + s.reasoning.strip().replace("\n", "\n> "))
            out.append("")
        if s.text.strip():
            out.append("**assistant:**")
            out.append(s.text.strip())
            out.append("")
        for tc in s.tool_calls:
            args_str = _short_args(tc["args"])
            out.append(f"**→ tool call:** `{tc['name']}({args_str})`")
            res = s.tool_results.get(tc["id"])
            if res is None:
                out.append(f"  *(no result captured for call_id={tc['id']!r})*")
            else:
                out.append("  **← result:** " + _short_result(res).replace("\n", "\n  "))
            out.append("")
        if s.token_usage:
            tu = s.token_usage
            out.append(
                f"<sub>tokens: input={tu.get('input_other',0)}, "
                f"output={tu.get('output',0)}, "
                f"cache_read={tu.get('input_cache_read',0)}</sub>"
            )
            out.append("")

    if meta and "final_response" in meta:
        out.append("## Final response")
        out.append("```json")
        out.append(json.dumps(meta["final_response"], ensure_ascii=False, indent=2))
        out.append("```")
    elif meta and "final_error" in meta:
        out.append("## Final error")
        out.append("```json")
        out.append(json.dumps(meta["final_error"], ensure_ascii=False, indent=2))
        out.append("```")

    return "\n".join(out) + "\n"


def render_json(steps: list[_Step], meta: dict[str, Any] | None) -> str:
    payload = {
        "meta": meta or {},
        "steps": [
            {
                "n": s.n,
                "interrupted": s.interrupted,
                "text": s.text,
                "reasoning": s.reasoning,
                "tool_calls": s.tool_calls,
                "tool_results": s.tool_results,
                "token_usage": s.token_usage,
            }
            for s in steps
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", type=Path, help="Path to kimi-cli wire JSONL")
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of Markdown",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write to file instead of stdout",
    )
    args = p.parse_args()

    if not args.path.is_file():
        print(f"file not found: {args.path}", file=sys.stderr)
        return 2

    steps, meta = group_into_steps(args.path)
    text = render_json(steps, meta) if args.json else render_markdown(steps, meta)

    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
