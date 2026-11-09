"""opencode JSONL stream -> AgentTrace v0.1.

Consumes the JSONL produced by the patched ``opencode run --format=json``
(see ``aos_bench/harness/opencode``'s ``cli/cmd/run.ts``) and rebuilds the
session tree as an AgentTrace dict per ``docs/agent_trace.md`` §15.

Source events we expect (anything else is preserved as ``raw_ref`` only):

- ``session_created`` / ``session_updated``: per-session metadata
  (``parentID`` is the linkage); used to discover runs and build the tree.
- ``session_status``: lifecycle (running / idle).
- ``subagent_spawn``: emitted when the ``task`` tool transitions to running;
  carries ``tool_call_id`` and ``child_session_id`` so a parent's tool call
  can be tied to its child run.
- ``step_start`` / ``step_finish``: turn boundaries; ``step_finish.part``
  carries token + cost metrics.
- ``text`` / ``reasoning``: assistant text blocks.
- ``tool_use``: a tool part once it's ``completed`` or ``error``.
- ``error``: session-level errors.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any


_AGENT_NAME_DEFAULT = "orchestrator"


def collect_opencode_agent_trace(
    jsonl_path: Path | str,
    *,
    trace_id: str | None = None,
    model_name: str | None = None,
    agent_version: str = "unknown",
    benchmark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse an opencode JSONL stream into an AgentTrace dict.

    ``jsonl_path`` is the file captured from ``opencode run --format=json``
    stdout (the patched build emits the richer event set documented above).

    The returned dict matches AgentTrace v0.1 (§17 minimum subset, plus
    optional fields where the JSONL gives us enough signal): top-level
    ``schema_version``, ``trace_id``, ``agent_system``, ``root_run_id``,
    ``runs`` with per-session events, ``artifacts`` linking back to the
    source JSONL, and a ``summary``.
    """
    path = Path(jsonl_path)
    records = list(_iter_records(path))

    if not records:
        # Stream was empty — synthesize a degenerate trace so downstream
        # consumers don't have to special-case "agent never ran".
        return _empty_trace(
            trace_id=trace_id or _make_trace_id(path),
            model_name=model_name,
            agent_version=agent_version,
            benchmark=benchmark,
            jsonl_path=str(path) if path.exists() else None,
        )

    # First pass: build session inventory + parent map.
    session_index = _index_sessions(records)
    root_session_id = _resolve_root_session(records, session_index)

    # Second pass: distribute events into per-session buckets.
    by_session = _group_events_by_session(records, root_session_id)

    runs: list[dict[str, Any]] = []
    runs_by_session: dict[str, dict[str, Any]] = {}
    spawn_links: list[dict[str, Any]] = []  # populated for cross-session linkage

    for session_id, raw_events in by_session.items():
        run = _build_run(
            session_id=session_id,
            raw_events=raw_events,
            session_index=session_index,
            root_session_id=root_session_id,
            model_name=model_name,
            spawn_links=spawn_links,
        )
        runs.append(run)
        runs_by_session[session_id] = run

    # Third pass: stitch parent_run_id / source_event_id using spawn_links.
    _resolve_run_tree(runs_by_session, session_index, root_session_id)
    _attach_source_events(runs_by_session, spawn_links)

    # Re-order so root is first; subagent runs follow.
    root_run_id = _make_run_id(root_session_id, role="root")
    runs.sort(key=lambda r: (0 if r["run_id"] == root_run_id else 1, r["run_id"]))

    summary = _summarise(runs, root_run_id=root_run_id)

    return {
        "schema_version": "AgentTrace-v0.1",
        "trace_id": trace_id or _make_trace_id(path),
        "benchmark": benchmark or {},
        "agent_system": {
            "name": "opencode",
            "version": agent_version,
            "entrypoint": "opencode run --format=json",
            "default_model": model_name,
            "trace_source": "native",
            "extra": {},
        },
        "root_run_id": root_run_id,
        "runs": runs,
        "artifacts": [_artifact("native_jsonl", path)] if path.exists() else [],
        "summary": summary,
        "notes": "",
        "extra": {},
    }


# ── Loading ──────────────────────────────────────────────────────────────


def _iter_records(path: Path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # opencode CLI also writes non-JSON UI noise to stdout when not
            # in --format=json mode; tolerate stray lines so operators can
            # `tee` the same file for both paths.
            continue
        if isinstance(obj, dict):
            yield obj


def _make_trace_id(path: Path) -> str:
    name = path.stem if path else "opencode"
    return f"trace_{name}_{uuid.uuid4().hex[:8]}"


# ── Session inventory ────────────────────────────────────────────────────


def _index_sessions(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build {sessionID: {parentID, info, …}} from session_* events."""
    index: dict[str, dict[str, Any]] = {}
    for rec in records:
        rtype = rec.get("type")
        if rtype not in ("session_created", "session_updated"):
            continue
        sid = rec.get("sessionID")
        if not sid:
            continue
        entry = index.setdefault(sid, {"sessionID": sid, "parentID": None, "info": {}})
        # Prefer the most recent non-null parentID we've seen.
        parent = rec.get("parentID")
        if parent and not entry["parentID"]:
            entry["parentID"] = parent
        info = rec.get("info") or {}
        if info:
            # Merge: newer keys win.
            entry["info"].update(info)
    return index


def _resolve_root_session(
    records: list[dict[str, Any]],
    session_index: dict[str, dict[str, Any]],
) -> str:
    """Pick the session that has no parent (= the user-invoked root).

    Falls back to the sessionID on the first event we observe, so even a
    pre-patched opencode stream (which only emits a few event types tagged
    with the root sessionID) still gives us a sensible root.
    """
    for sid, entry in session_index.items():
        if not entry["parentID"]:
            return sid
    for rec in records:
        sid = rec.get("sessionID")
        if sid:
            return sid
    return "session_unknown"


def _group_events_by_session(
    records: list[dict[str, Any]],
    root_session_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Bucket records by their originating sessionID, root-first.

    We preserve insertion order (= temporal order) within each bucket and
    keep the root bucket first so downstream serializers keep root events
    on top of the trace's run list.
    """
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # Touch the root bucket first so its key is visited first by `dict`'s
    # insertion-ordered iteration even when the first event isn't the
    # root's session_created.
    by_session[root_session_id]
    for rec in records:
        sid = rec.get("sessionID") or root_session_id
        by_session[sid].append(rec)
    return by_session


# ── Run building ─────────────────────────────────────────────────────────


def _make_run_id(session_id: str, *, role: str) -> str:
    if role == "root":
        return f"run_root_{session_id}"
    return f"run_sub_{session_id}"


def _build_run(
    *,
    session_id: str,
    raw_events: list[dict[str, Any]],
    session_index: dict[str, dict[str, Any]],
    root_session_id: str,
    model_name: str | None,
    spawn_links: list[dict[str, Any]],
) -> dict[str, Any]:
    role = "root" if session_id == root_session_id else "subagent"
    run_id = _make_run_id(session_id, role=role)

    events: list[dict[str, Any]] = []
    seq = 0
    started_at: float | None = None
    ended_at: float | None = None
    status: str = "running"
    prompt_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0
    cached_tokens = 0
    cost_usd = 0.0
    open_tool_calls: dict[str, str] = {}  # tool_call_id -> event_id

    info = (session_index.get(session_id) or {}).get("info") or {}
    parent_session_id = (session_index.get(session_id) or {}).get("parentID")
    session_title = info.get("title")
    session_agent = info.get("agent")
    session_model_id = info.get("modelID")
    session_provider_id = info.get("providerID")

    for raw in raw_events:
        ts = _normalize_ts(raw.get("timestamp"))
        if ts is not None:
            if started_at is None or ts < started_at:
                started_at = ts
            if ended_at is None or ts > ended_at:
                ended_at = ts

        rtype = raw.get("type")
        if rtype == "session_created":
            # Recorded once on the run via metadata above; no per-event entry.
            continue
        if rtype == "session_updated":
            # Carries token/cost progressively in opencode; we collect a
            # single status event per run rather than spamming per-update.
            continue
        if rtype == "session_status":
            payload_status = raw.get("status") or {}
            stype = payload_status.get("type")
            if stype == "idle":
                status = "completed"
            elif stype == "running":
                status = "running"
            elif stype == "error":
                status = "failed"
            seq += 1
            events.append(
                _event(seq, ts, "status", {"status": payload_status}, raw=raw)
            )
            continue

        if rtype == "step_start":
            seq += 1
            events.append(_event(seq, ts, "step_begin", {"part": raw.get("part")}, raw=raw))
            continue

        if rtype == "step_finish":
            part = raw.get("part") or {}
            tokens = part.get("tokens") or {}
            cost = part.get("cost") or 0
            in_tok = tokens.get("input", 0) or 0
            out_tok = tokens.get("output", 0) or 0
            r_tok = tokens.get("reasoning", 0) or 0
            cache = tokens.get("cache") or {}
            c_read = cache.get("read", 0) or 0
            c_write = cache.get("write", 0) or 0
            prompt_tokens += in_tok + c_read
            completion_tokens += out_tok
            reasoning_tokens += r_tok
            cached_tokens += c_read
            cost_usd += float(cost)
            seq += 1
            events.append(
                _event(
                    seq,
                    ts,
                    "step_end",
                    {"part": part},
                    metrics={
                        "prompt_tokens": in_tok + c_read,
                        "completion_tokens": out_tok,
                        "reasoning_tokens": r_tok or None,
                        "cached_tokens": c_read or None,
                        "cost_usd": float(cost) if cost else None,
                        "cache_write_tokens": c_write or None,
                    },
                    raw=raw,
                )
            )
            continue

        if rtype == "text":
            part = raw.get("part") or {}
            seq += 1
            events.append(
                _event(
                    seq,
                    ts,
                    "message",
                    {"role": "assistant", "content": part.get("text", "")},
                    raw=raw,
                )
            )
            continue

        if rtype == "reasoning":
            part = raw.get("part") or {}
            seq += 1
            events.append(
                _event(
                    seq,
                    ts,
                    "reasoning",
                    {"content": part.get("text", "")},
                    raw=raw,
                )
            )
            continue

        if rtype == "tool_use":
            part = raw.get("part") or {}
            state = part.get("state") or {}
            call_id = part.get("callID") or part.get("id") or ""
            tool_name = part.get("tool", "")
            tool_input = state.get("input") or {}
            tool_output = state.get("output")
            tool_status = state.get("status")
            seq += 1
            call_event = _event(
                seq,
                ts,
                "tool_call",
                {
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "arguments": tool_input
                    if isinstance(tool_input, dict)
                    else {"value": tool_input},
                },
                raw=raw,
            )
            events.append(call_event)
            open_tool_calls[call_id] = call_event["event_id"]

            # opencode's --format=json only emits tool_use once the tool has
            # completed/errored, so the result follows immediately.
            seq += 1
            events.append(
                _event(
                    seq,
                    ts,
                    "tool_result",
                    {
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "output": _stringify_output(tool_output),
                        "status": tool_status,
                    },
                    raw=raw,
                    parent_event_id=call_event["event_id"],
                )
            )
            continue

        if rtype == "subagent_spawn":
            child_session = raw.get("child_session_id")
            tool_call_id = raw.get("tool_call_id")
            seq += 1
            spawn_event = _event(
                seq,
                ts,
                "subagent_spawn",
                {
                    "child_run_id": _make_run_id(child_session, role="subagent")
                    if child_session
                    else None,
                    "tool_call_id": tool_call_id,
                    "child_session_id": child_session,
                    "input": raw.get("input"),
                },
                raw=raw,
                parent_event_id=open_tool_calls.get(tool_call_id),
            )
            events.append(spawn_event)
            spawn_links.append(
                {
                    "parent_session_id": session_id,
                    "child_session_id": child_session,
                    "spawn_event_id": spawn_event["event_id"],
                }
            )
            continue

        if rtype == "error":
            seq += 1
            events.append(
                _event(
                    seq,
                    ts,
                    "error",
                    {"error": raw.get("error")},
                    raw=raw,
                )
            )
            if status == "running":
                status = "failed"
            continue

        # Unknown / future event types: keep them addressable without
        # losing data, but don't try to interpret them.
        seq += 1
        events.append(
            _event(
                seq,
                ts,
                f"native:{rtype}",
                {k: v for k, v in raw.items() if k not in ("type", "timestamp")},
                raw=raw,
            )
        )

    if status == "running" and events and events[-1]["kind"] != "status":
        # Stream ended without an idle status — assume the session is still
        # in flight from this trace's vantage point.
        status = "running"

    run = {
        "run_id": run_id,
        "parent_run_id": None,
        "source_event_id": None,
        "role": role,
        "launch_mode": "inline" if role == "root" else "foreground",
        "session_mode": "child_session" if role == "subagent" else "same_session",
        "agent_name": session_agent or _AGENT_NAME_DEFAULT,
        "model": {
            "provider": session_provider_id,
            "name": session_model_id or model_name,
        },
        "status": status,
        "task": {"description": session_title} if session_title else None,
        "timing": {
            "created_at": started_at,
            "started_at": started_at,
            "ended_at": ended_at,
        },
        "resource_limits": {},
        "resource_usage": {
            "wall_time_ms": int((ended_at - started_at) * 1000)
            if started_at is not None and ended_at is not None
            else None,
            "prompt_tokens": prompt_tokens or None,
            "completion_tokens": completion_tokens or None,
            "cost_usd": cost_usd or None,
        },
        "metrics": {
            "prompt_tokens": prompt_tokens or None,
            "completion_tokens": completion_tokens or None,
            "reasoning_tokens": reasoning_tokens or None,
            "cached_tokens": cached_tokens or None,
            "cost_usd": cost_usd or None,
        },
        "events": events,
        "artifacts": [],
        "extra": {
            "native_session_id": session_id,
            "native_parent_session_id": parent_session_id,
        },
    }
    return run


def _resolve_run_tree(
    runs_by_session: dict[str, dict[str, Any]],
    session_index: dict[str, dict[str, Any]],
    root_session_id: str,
) -> None:
    """Set ``parent_run_id`` on each subagent run from session_index."""
    root_run_id = _make_run_id(root_session_id, role="root")
    for session_id, run in runs_by_session.items():
        if session_id == root_session_id:
            run["parent_run_id"] = None
            continue
        parent_session = (session_index.get(session_id) or {}).get("parentID")
        if parent_session and parent_session in runs_by_session:
            run["parent_run_id"] = runs_by_session[parent_session]["run_id"]
        else:
            # Subagent without a session_created event for its parent —
            # fall back to root so the run is still reachable in the tree.
            run["parent_run_id"] = root_run_id


def _attach_source_events(
    runs_by_session: dict[str, dict[str, Any]],
    spawn_links: list[dict[str, Any]],
) -> None:
    """Set ``source_event_id`` on subagent runs from subagent_spawn events."""
    for link in spawn_links:
        child_session = link.get("child_session_id")
        if not child_session:
            continue
        child_run = runs_by_session.get(child_session)
        if not child_run:
            continue
        child_run["source_event_id"] = link["spawn_event_id"]


# ── Helpers ──────────────────────────────────────────────────────────────


def _event(
    seq: int,
    timestamp: float | None,
    kind: str,
    payload: dict[str, Any],
    *,
    raw: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    parent_event_id: str | None = None,
) -> dict[str, Any]:
    metrics_clean = (
        {k: v for k, v in metrics.items() if v is not None} if metrics else None
    )
    return {
        "event_id": f"evt_{seq:06d}",
        "seq": seq,
        "timestamp": timestamp,
        "kind": kind,
        "phase": None,
        "step_id": None,
        "parent_event_id": parent_event_id,
        "payload": payload,
        "metrics": metrics_clean or None,
        "raw_ref": _raw_ref(raw),
        "extra": {},
    }


def _raw_ref(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return {
        "source": "opencode-jsonl",
        "native_type": raw.get("type"),
    }


def _normalize_ts(ts: Any) -> float | None:
    """opencode emits ms timestamps; we expose seconds in AgentTrace."""
    if ts is None:
        return None
    try:
        return float(ts) / 1000.0
    except (TypeError, ValueError):
        return None


def _stringify_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(output)


def _artifact(kind: str, path: Path) -> dict[str, Any]:
    return {
        "artifact_id": f"art_{uuid.uuid4().hex[:8]}",
        "run_id": None,
        "kind": kind,
        "path": str(path),
    }


def _empty_trace(
    *,
    trace_id: str,
    model_name: str | None,
    agent_version: str,
    benchmark: dict[str, Any] | None,
    jsonl_path: str | None,
) -> dict[str, Any]:
    root_run_id = "run_root_unknown"
    return {
        "schema_version": "AgentTrace-v0.1",
        "trace_id": trace_id,
        "benchmark": benchmark or {},
        "agent_system": {
            "name": "opencode",
            "version": agent_version,
            "entrypoint": "opencode run --format=json",
            "default_model": model_name,
            "trace_source": "native",
            "extra": {},
        },
        "root_run_id": root_run_id,
        "runs": [
            {
                "run_id": root_run_id,
                "parent_run_id": None,
                "source_event_id": None,
                "role": "root",
                "launch_mode": "inline",
                "session_mode": "same_session",
                "agent_name": _AGENT_NAME_DEFAULT,
                "model": {"provider": None, "name": model_name},
                "status": "idle",
                "task": None,
                "timing": {"created_at": None, "started_at": None, "ended_at": None},
                "resource_limits": {},
                "resource_usage": {},
                "metrics": {},
                "events": [],
                "artifacts": [],
                "extra": {"native_session_id": None, "native_parent_session_id": None},
            }
        ],
        "artifacts": [
            {
                "artifact_id": f"art_{uuid.uuid4().hex[:8]}",
                "run_id": None,
                "kind": "native_jsonl",
                "path": jsonl_path,
            }
        ]
        if jsonl_path
        else [],
        "summary": {
            "total_runs": 1,
            "total_events": 0,
            "subagent_count_total": 0,
        },
        "notes": "opencode emitted no JSONL events",
        "extra": {},
    }


def _summarise(runs: list[dict[str, Any]], *, root_run_id: str) -> dict[str, Any]:
    total_runs = len(runs)
    total_events = sum(len(r["events"]) for r in runs)
    subagents = [r for r in runs if r["run_id"] != root_run_id]
    total_prompt_tokens = sum(
        r["metrics"].get("prompt_tokens") or 0 for r in runs if r.get("metrics")
    )
    total_completion_tokens = sum(
        r["metrics"].get("completion_tokens") or 0 for r in runs if r.get("metrics")
    )
    total_cost_usd = sum(
        r["metrics"].get("cost_usd") or 0.0 for r in runs if r.get("metrics")
    )
    return {
        "total_runs": total_runs,
        "total_events": total_events,
        "subagent_count_total": len(subagents),
        "subagent_count_background": 0,  # opencode task tool is foreground only
        "peak_parallel_subagents": _peak_parallel(runs),
        "total_prompt_tokens": total_prompt_tokens or None,
        "total_completion_tokens": total_completion_tokens or None,
        "total_cost_usd": total_cost_usd if total_cost_usd else None,
    }


def _peak_parallel(runs: list[dict[str, Any]]) -> int:
    """Estimate the max number of subagent runs that overlapped in time."""
    intervals: list[tuple[float, float]] = []
    for run in runs:
        if run["role"] != "subagent":
            continue
        timing = run.get("timing") or {}
        start = timing.get("started_at")
        end = timing.get("ended_at")
        if start is None or end is None:
            continue
        intervals.append((float(start), float(end)))
    if not intervals:
        return 0
    edges: list[tuple[float, int]] = []
    for s, e in intervals:
        edges.append((s, +1))
        edges.append((e, -1))
    edges.sort()
    cur = peak = 0
    for _, delta in edges:
        cur += delta
        peak = max(peak, cur)
    return peak
