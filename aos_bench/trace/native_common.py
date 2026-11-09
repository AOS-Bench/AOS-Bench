"""Shared helpers for native-log AgentTrace collectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def artifact(kind: str, path: Path, *, base_dir: Path | None = None) -> dict[str, Any]:
    try:
        value = path.relative_to(base_dir) if base_dir is not None else path
    except ValueError:
        value = path
    return {
        "artifact_id": f"artifact_{kind}_{path.stem}",
        "kind": kind,
        "path": str(value),
        "media_type": "application/x-ndjson" if path.suffix == ".jsonl" else None,
        "extra": {},
    }


def build_run(
    *,
    run_id: str,
    parent_run_id: str | None,
    source_event_id: str | None,
    role: str,
    agent_name: str,
    model_name: str | None,
    events: list[dict[str, Any]],
    prompt_tokens: int,
    completion_tokens: int,
    first_ts: float | None,
    last_ts: float | None,
    artifacts: list[dict[str, Any]],
    session_mode: str,
    status: str | None = None,
    task: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failed = any(
        event.get("kind") == "error"
        or (
            event.get("kind") == "tool_result"
            and (event.get("payload") or {}).get("error")
        )
        for event in events
    )
    return {
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "source_event_id": source_event_id,
        "role": role,
        "launch_mode": "inline" if role == "root" else "foreground",
        "session_mode": session_mode,
        "agent_name": agent_name,
        "model": {"provider": None, "name": model_name},
        "status": status or ("failed" if failed else "completed"),
        "task": task,
        "timing": {
            "created_at": first_ts,
            "started_at": first_ts,
            "ended_at": last_ts,
        },
        "resource_limits": {},
        "resource_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
        "metrics": {
            "event_count": len(events),
            "tool_call_count": sum(e.get("kind") == "tool_call" for e in events),
            "llm_call_count": sum(e.get("kind") == "step_begin" for e in events),
        },
        "events": events,
        "artifacts": artifacts,
        "extra": extra or {},
    }


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_count": len(runs),
        "subagent_count_total": sum(run.get("role") == "subagent" for run in runs),
        "event_count": sum(len(run.get("events", [])) for run in runs),
        "prompt_tokens": sum(
            int((run.get("resource_usage") or {}).get("prompt_tokens", 0) or 0)
            for run in runs
        ),
        "completion_tokens": sum(
            int((run.get("resource_usage") or {}).get("completion_tokens", 0) or 0)
            for run in runs
        ),
    }


def trial_status(trial_dir: Path) -> str:
    result_path = trial_dir / "result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    if result.get("exception_info"):
        return "failed"
    if result.get("finished_at"):
        return "completed"
    return "unknown"
