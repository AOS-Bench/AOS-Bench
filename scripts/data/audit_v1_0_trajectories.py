#!/usr/bin/env python3
"""Strictly audit completed Harbor stream trajectories for infra failures."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any

from harbor.utils.trajectory_validator import TrajectoryValidator


TERMINAL_TASK_STATES = {"completed", "failed"}
SEVERE_MCP_MARKERS = (
    'MCP server "aos_pool" is not connected',
    'MCP server "aos_pool" tool',
    "sent no response or progress",
)
CODEX_INFRA_EVENT_TYPES = {"api_error", "error", "response.failed", "turn.failed"}
CODEX_FAILED_ITEM_STATES = {"error", "failed"}
RMCP_ERROR_RE = re.compile(
    r"(?i)\brmcp\b[^\n]*(?:HTTP\s+5\d\d|error|timed?\s*out|timeout)"
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _claude_infra_errors(path: Path) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if not block.get("is_error"):
                continue
            content = str(block.get("content", ""))
            if "The operation timed out." in content:
                # A blocking todo_next poll reaches its requested deadline by design.
                continue
            if any(marker in content for marker in SEVERE_MCP_MARKERS):
                errors.append(
                    {"line": line_number, "kind": "mcp", "message": content[:500]}
                )
        if event.get("type") == "result":
            api_status = event.get("api_error_status")
            if event.get("is_error") or api_status not in (None, ""):
                errors.append(
                    {
                        "line": line_number,
                        "kind": "api",
                        "message": str(event)[:500],
                    }
                )
        elif event.get("type") in {"api_error", "error"}:
            errors.append(
                {"line": line_number, "kind": "api", "message": line[:500]}
            )
    return errors


def _codex_infra_errors(path: Path) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Codex normally emits JSONL. Retain a narrow fallback for transport
            # diagnostics written directly by rmcp, but never scan serialized
            # tool output: todo_list contains fields such as
            # `status="pending", at_sec=599`, which previously matched the broad
            # `status ... 5xx` API-error regex.
            if RMCP_ERROR_RE.search(line):
                errors.append(
                    {
                        "line": line_number,
                        "kind": "mcp_transport",
                        "message": line[:500],
                    }
                )
            continue

        event_type = str(event.get("type") or "").lower()
        item = event.get("item") or {}
        item_type = str(item.get("type") or "").lower()
        item_status = str(item.get("status") or "").lower()
        item_error = item.get("error")
        if (
            event_type in CODEX_INFRA_EVENT_TYPES
            or (
                item_type == "mcp_tool_call"
                and (
                    item_status in CODEX_FAILED_ITEM_STATES
                    or item_error not in (None, "", False)
                )
            )
        ):
            errors.append(
                {
                    "line": line_number,
                    "kind": "api_or_mcp",
                    "message": line[:500],
                }
            )
    return errors


def audit_trial(result_path: Path) -> dict[str, Any]:
    trial_dir = result_path.parent
    result = _load_json(result_path)
    trajectory_path = trial_dir / "agent/trajectory.json"
    tasks_path = trial_dir / "dispatcher/tasks.json"
    raw_logs = list((trial_dir / "agent").glob("*.txt"))

    atif_valid = False
    atif_error = None
    try:
        atif_valid = bool(TrajectoryValidator().validate(trajectory_path))
    except Exception as exc:  # validation errors belong in the report
        atif_error = f"{type(exc).__name__}: {exc}"

    task_states: list[str] = []
    dispatcher_error = None
    try:
        task_data = _load_json(tasks_path)
        tasks = task_data.get("tasks", task_data) if isinstance(task_data, dict) else task_data
        if isinstance(tasks, dict):
            tasks = list(tasks.values())
        task_states = [
            str(task.get("status")) for task in tasks if isinstance(task, dict)
        ]
    except Exception as exc:
        dispatcher_error = f"{type(exc).__name__}: {exc}"

    infra_errors: list[dict[str, Any]] = []
    for raw_log in raw_logs:
        if raw_log.name == "claude-code.txt":
            infra_errors.extend(_claude_infra_errors(raw_log))
        elif raw_log.name == "codex.txt":
            infra_errors.extend(_codex_infra_errors(raw_log))

    agent_result = result.get("agent_result") or {}
    input_tokens = int(agent_result.get("n_input_tokens") or 0)
    cache_tokens = int(agent_result.get("n_cache_tokens") or 0)
    cache_ratio = cache_tokens / input_tokens if input_tokens else None
    terminal_dispatcher = bool(task_states) and all(
        state in TERMINAL_TASK_STATES for state in task_states
    )
    clean = (
        atif_valid
        and result.get("exception_info") is None
        and dispatcher_error is None
        and terminal_dispatcher
        and not infra_errors
    )
    return {
        "trial": str(trial_dir),
        "clean": clean,
        "atif_valid": atif_valid,
        "atif_error": atif_error,
        "exception_info": result.get("exception_info"),
        "dispatcher_error": dispatcher_error,
        "dispatcher_states": dict(Counter(task_states)),
        "terminal_dispatcher": terminal_dispatcher,
        "infra_errors": infra_errors,
        "cache_ratio": cache_ratio,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result_paths = sorted(args.run_root.glob("jobs/**/result.json"))
    trial_paths = [
        path for path in result_paths if (path.parent / "agent/trajectory.json").is_file()
    ]
    trials = [audit_trial(path) for path in trial_paths]
    report = {
        "run_root": str(args.run_root),
        "trials": trials,
        "summary": {
            "total": len(trials),
            "clean": sum(bool(trial["clean"]) for trial in trials),
            "invalid": sum(not bool(trial["clean"]) for trial in trials),
        },
    }
    output = args.output or (args.run_root / "trajectory-audit.json")
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps(report["summary"], sort_keys=True))
    for trial in trials:
        if not trial["clean"]:
            print(f"INVALID {trial['trial']}")


if __name__ == "__main__":
    main()
