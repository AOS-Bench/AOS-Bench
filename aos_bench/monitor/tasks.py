"""Extract per-task dispatcher lifecycle from a root agent's wire events.

Two reconstruction paths, tried in order:

1. **Snapshot path** — if the agent ever called ``todo_list``, the most
   recent ToolResult carries the dispatcher's authoritative view of
   every task (status, ready_at, claimed_at, completed_at, note,
   plus the stream-toml ``at_sec``). One ``todo_list`` result is the
   freshest, complete picture; we use it verbatim.

2. **Reconstruction path** — if the agent never calls ``todo_list``
   (true under the post-2026-06 system prompt, which doesn't mandate
   polling), we rebuild the lifecycle from the individual
   ``todo_next`` / ``todo_update`` ToolCall+Result pairs we DO see:
     - ``todo_next`` ToolResult — emitted when the dispatcher hands
       out a task; reveals task_id + claimed_at (= response timestamp).
     - ``todo_update`` ToolCall — agent's report of terminal status
       for a task; reveals completed_at + agent-reported status/note.
   This gives a partial-but-correct view: tasks the agent never
   interacted with are invisible (which is fine — they wouldn't show
   up in the snapshot path either, since the dispatcher's agent-view
   filters out PENDING).

Two entrypoints:
- ``extract_task_lifecycle`` — re-parses on demand (used by the API endpoint
  as a fallback for trials with small event volume)
- ``extract_task_lifecycle_from_events`` — same parser run against a
  pre-collected event list (used by KimiSessionTail which keeps an uncapped
  rolling buffer of root events so the latest todo_list snapshot is never
  lost to events_tail truncation)
"""

from __future__ import annotations

import json
from typing import Any, Iterable


def extract_task_lifecycle_from_events(root_events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same as ``extract_task_lifecycle`` — explicit name to highlight that
    the caller passes the full (uncapped) event stream, not a capped tail.
    """
    return extract_task_lifecycle(root_events)


def _result_text(payload: dict[str, Any]) -> str | None:
    """Pull the text content out of a ToolResult payload. Returns None
    if the result isn't a single text block (e.g. error responses).

    Three return_value shapes are handled:
    - kimi-cli  → dict ``{"output": <str|list[{type,text}]>, ...}``
    - claude (envelope) → str ``'{"result": "<inner-json-text>"}'`` where
      the inner text is the actual MCP body; we unwrap one JSON layer.
    - claude (flattened) → plain string holding the MCP body itself (the
      parser flattens MCP text blocks to one string); returned as-is so
      downstream body parsers see the raw JSON (incl. todo_list arrays)."""
    rv = payload.get("return_value")
    if isinstance(rv, str):
        if not rv:
            return None
        try:
            outer = json.loads(rv)
        except (json.JSONDecodeError, ValueError):
            return rv
        if isinstance(outer, dict) and "result" in outer:
            inner = outer.get("result")
            if isinstance(inner, str):
                return inner
            if isinstance(inner, dict):
                # already-parsed body — re-stringify so callers get text
                return json.dumps(inner)
        return rv
    # Kimi path
    if not isinstance(rv, dict):
        return None
    out = rv.get("output")
    if isinstance(out, list) and out:
        first = out[0]
        if isinstance(first, dict):
            text = first.get("text")
            if isinstance(text, str):
                return text
    elif isinstance(out, str):
        return out
    return None


def _parse_json_args(arg_str: str | dict | None) -> dict[str, Any]:
    if isinstance(arg_str, dict):
        return arg_str
    if not isinstance(arg_str, str):
        return {}
    try:
        v = json.loads(arg_str)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _parse_tool_result_body(text: str) -> dict[str, Any]:
    """Parse a todo_* ToolResult body, unwrapping bridge envelopes.

    Plain MCP results are the response JSON itself; the Workflow-mode
    bridge wraps it one level deeper as ``{"result": "<json-string>"}``.
    """
    body = _parse_json_args(text)
    inner = body.get("result")
    if "task_id" not in body and isinstance(inner, str):
        unwrapped = _parse_json_args(inner)
        if unwrapped:
            return unwrapped
    return body


def _strip_mcp_prefix(name: str) -> str:
    """Normalize MCP tool names like ``mcp__aos_pool__todo_next`` to the
    bare ``todo_next`` form. Claude Code emits the prefixed form; kimi-cli
    emits the bare form. The dispatcher reconstruction code matches on the
    bare form, so this is the single normalization point."""
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return "__".join(parts[2:])
    return name


def extract_task_lifecycle(root_events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk a root run's events and return the dispatcher's task list.

    Falls through two paths (see module docstring): snapshot path when
    ``todo_list`` was ever called, else reconstruction from
    ``todo_next`` + ``todo_update`` pairs. Returns ``[]`` only when
    neither path yields any tasks.
    """
    events = list(root_events)

    # ── Pass 1: index tool_call_id → function_name, plus capture
    # todo_update intents (agent-side status + note) keyed by task_id,
    # AND capture todo_next call timestamps keyed by tool_call_id so
    # we can attribute the corresponding ToolResult.
    call_name: dict[str, str] = {}
    update_notes: dict[str, dict[str, Any]] = {}
    next_call_ts: dict[str, float] = {}
    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        if kind != "tool_call":
            continue
        tcid = payload.get("id")
        fname = (payload.get("function") or {}).get("name")
        if not (isinstance(tcid, str) and isinstance(fname, str)):
            continue
        # Claude-Code emits MCP tool names as ``mcp__<server>__<tool>`` while
        # kimi-cli emits the bare tool name. Normalize to the bare form so
        # the dispatcher panel populates for both.
        fname = _strip_mcp_prefix(fname)
        call_name[tcid] = fname
        if _is_todo_tool(fname, "todo_update"):
            args = _parse_json_args((payload.get("function") or {}).get("arguments"))
            tid = args.get("task_id")
            if isinstance(tid, str):
                update_notes[tid] = {
                    "agent_reported_status": args.get("status"),
                    "agent_note": args.get("note"),
                    "agent_reported_at": ev.get("timestamp"),
                }
        elif _is_todo_tool(fname, "todo_next"):
            ts = ev.get("timestamp")
            if isinstance(ts, (int, float)):
                next_call_ts[tcid] = float(ts)

    # ── Pass 2: snapshot path — latest todo_list ToolResult wins.
    latest_text: str | None = None
    for ev in reversed(events):
        if ev.get("kind") != "tool_result":
            continue
        payload = ev.get("payload") or {}
        tcid = payload.get("tool_call_id")
        if not _is_todo_tool(call_name.get(tcid), "todo_list"):
            continue
        latest_text = _result_text(payload)
        if latest_text:
            break

    if latest_text:
        rows = _rows_from_todo_list_snapshot(latest_text, update_notes)
        if rows:
            return rows
        # Unparseable snapshot (unknown envelope) → fall through.

    # ── Pass 3: reconstruction path — replay todo_next/todo_update.
    return _reconstruct_from_todo_next_update(
        events, call_name, next_call_ts, update_notes
    )


def _rows_from_todo_list_snapshot(
    latest_text: str, update_notes: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    try:
        rows = json.loads(latest_text)
    except (json.JSONDecodeError, ValueError):
        return []
    # Workflow-bridge envelope: {"result": "<json-string>"}.
    if isinstance(rows, dict) and isinstance(rows.get("result"), str):
        try:
            rows = json.loads(rows["result"])
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid = row.get("task_id")
        if not isinstance(tid, str):
            continue
        entry = {
            "task_id": tid,
            "at_sec": row.get("at_sec"),
            "ready_at": row.get("ready_at"),
            "claimed_at": row.get("claimed_at"),
            "completed_at": row.get("completed_at"),
            "status": row.get("status"),
            "note": row.get("note", ""),
        }
        n = update_notes.get(tid)
        if n:
            entry.update(n)
        out.append(entry)
    out.sort(
        key=lambda r: r.get("at_sec") if isinstance(r.get("at_sec"), (int, float)) else 1e18
    )
    return out


def _reconstruct_from_todo_next_update(
    events: list[dict[str, Any]],
    call_name: dict[str, str],
    next_call_ts: dict[str, float],
    update_notes: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build per-task lifecycle from todo_next ToolResults +
    todo_update ToolCalls alone. Used when the agent never calls
    todo_list — task list view falls back to "only what we've seen".
    """
    # task_id → entry. Order preserved by first-claimed time, since
    # under reconstruction we don't have at_sec from stream.toml.
    by_task: dict[str, dict[str, Any]] = {}

    first_ts: float | None = None
    for ev in events:
        ts = ev.get("timestamp")
        if isinstance(ts, (int, float)) and first_ts is None:
            first_ts = float(ts)
    if first_ts is None:
        return []

    for ev in events:
        if ev.get("kind") != "tool_result":
            continue
        payload = ev.get("payload") or {}
        tcid = payload.get("tool_call_id")
        if not _is_todo_tool(call_name.get(tcid), "todo_next"):
            continue
        text = _result_text(payload)
        if not text:
            continue
        body = _parse_tool_result_body(text)
        tid = body.get("task_id")
        if not isinstance(tid, str) or tid == "<FINISH>":
            continue
        claim_ts = next_call_ts.get(tcid)
        # Use the ToolCall timestamp (when agent asked) as a more honest
        # "claim time" than the ToolResult ts (which includes dispatcher
        # block wait). Fall back to result ts if call ts missing.
        if claim_ts is None and isinstance(ev.get("timestamp"), (int, float)):
            claim_ts = float(ev["timestamp"])
        entry = by_task.setdefault(
            tid,
            {
                "task_id": tid,
                "at_sec": None,            # unknown without todo_list snapshot
                "ready_at": None,
                "claimed_at": None,
                "completed_at": None,
                "status": "in_progress",   # claimed but no terminal report yet
                "note": "",
            },
        )
        if claim_ts is not None and first_ts is not None:
            offset = claim_ts - first_ts
            if entry["claimed_at"] is None or offset < entry["claimed_at"]:
                entry["claimed_at"] = offset

    # Apply todo_update reports → completed_at + final status.
    for tid, note in update_notes.items():
        entry = by_task.setdefault(
            tid,
            {
                "task_id": tid,
                "at_sec": None,
                "ready_at": None,
                "claimed_at": None,
                "completed_at": None,
                "status": "in_progress",
                "note": "",
            },
        )
        report_ts = note.get("agent_reported_at")
        if isinstance(report_ts, (int, float)) and first_ts is not None:
            entry["completed_at"] = float(report_ts) - first_ts
        # Map agent_reported_status → status field used by the frontend.
        ars = note.get("agent_reported_status")
        if isinstance(ars, str):
            entry["status"] = ars
        entry["agent_reported_status"] = ars
        entry["agent_note"] = note.get("agent_note")
        entry["agent_reported_at"] = report_ts

    # Order by first claim time (None last).
    out = list(by_task.values())
    out.sort(
        key=lambda r: r.get("claimed_at") if isinstance(r.get("claimed_at"), (int, float)) else 1e18
    )
    return out


def _is_todo_tool(name: Any, suffix: str) -> bool:
    """Match both bare (``todo_update``) and MCP-prefixed
    (``mcp__aos_pool__todo_update``) tool names."""
    return isinstance(name, str) and (name == suffix or name.endswith(f"__{suffix}"))


def attribute_task_executors(
    runs_events: dict[str, Iterable[dict[str, Any]]],
) -> dict[str, dict[str, str]]:
    """Map ``task_id`` → executor run ids by scanning EVERY run's events.

    The dispatcher doesn't know which agent inside the container did the
    work — only the transcripts do. Attribution signals, strongest first:

      - ``reported_by``: the run whose ``todo_update`` ToolCall named the
        task (terminal report — whoever reports usually did/owned it).
      - ``claimed_by``: the run whose ``todo_next`` ToolResult returned the
        task_id.

    Returns ``{task_id: {"claimed_by": run_id?, "reported_by": run_id?}}``.
    Works on capped ``events_tail`` — long runs may lose early claims, which
    degrades to "no attribution" (frontend renders the neutral color).
    """
    out: dict[str, dict[str, str]] = {}

    def slot(tid: str) -> dict[str, str]:
        return out.setdefault(tid, {})

    for run_id, events in runs_events.items():
        next_call_ids: set[str] = set()
        for ev in events:
            kind = ev.get("kind")
            payload = ev.get("payload") or {}
            if kind == "tool_call":
                fn = payload.get("function") or {}
                name = fn.get("name") or payload.get("name")
                if _is_todo_tool(name, "todo_update"):
                    args = _parse_json_args(fn.get("arguments"))
                    tid = args.get("task_id")
                    if isinstance(tid, str):
                        slot(tid)["reported_by"] = run_id
                elif _is_todo_tool(name, "todo_next"):
                    tcid = payload.get("id") or payload.get("tool_call_id")
                    if isinstance(tcid, str):
                        next_call_ids.add(tcid)
            elif kind == "tool_result":
                tcid = payload.get("tool_call_id") or payload.get("id")
                if tcid not in next_call_ids:
                    continue
                text = _result_text(payload)
                if not text:
                    continue
                body = _parse_tool_result_body(text)
                tid = body.get("task_id")
                if isinstance(tid, str) and tid != "<FINISH>":
                    slot(tid).setdefault("claimed_by", run_id)
    return out
