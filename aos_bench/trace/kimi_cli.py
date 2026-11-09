from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any


def find_kimi_cli_session_dir(
    share_dir: Path,
    *,
    work_dir: Path,
    session_id: str,
    kaos: str = "local",
) -> Path:
    """Resolve a kimi-cli session directory from share dir + work dir + session id."""

    work_dir_key = hashlib.md5(str(work_dir).encode("utf-8")).hexdigest()
    dirname = work_dir_key if kaos == "local" else f"{kaos}_{work_dir_key}"
    return share_dir / "sessions" / dirname / session_id


def find_kimi_cli_latest_session_dir(
    share_dir: Path,
    *,
    work_dir: Path,
    kaos: str = "local",
) -> Path:
    """Resolve the newest kimi-cli session directory for one work dir."""

    work_dir_key = hashlib.md5(str(work_dir).encode("utf-8")).hexdigest()
    dirname = work_dir_key if kaos == "local" else f"{kaos}_{work_dir_key}"
    sessions_root = share_dir / "sessions" / dirname
    if not sessions_root.is_dir():
        raise FileNotFoundError(f"No kimi-cli sessions found for {work_dir}")

    candidates = [path for path in sessions_root.iterdir() if path.is_dir()]
    if not candidates:
        raise FileNotFoundError(f"No kimi-cli session directories found in {sessions_root}")
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def collect_kimi_cli_agent_trace(
    session_dir: Path,
    *,
    trace_id: str | None = None,
    model_name: str | None = None,
    agent_version: str = "unknown",
) -> dict[str, Any]:
    """Build a minimal AgentTrace from one kimi-cli session directory."""

    session_dir = session_dir.resolve()
    if not session_dir.is_dir():
        raise FileNotFoundError(f"Session directory not found: {session_dir}")

    root_run_id = "run_root_001"
    trace_id = trace_id or f"trace_{session_dir.name}_{uuid.uuid4().hex[:8]}"

    root_wire_path = session_dir / "wire.jsonl"
    root_records = _load_wire_records(root_wire_path)

    root_events, root_tool_calls, spawn_events, root_usage = _normalize_wire_records(
        root_records,
        run_id=root_run_id,
        synthesize_subagent_spawns=True,
    )

    runs: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []

    artifacts.append(_artifact("native_wire", root_wire_path))

    tool_call_owners = dict(root_tool_calls)
    subagent_runs: dict[str, dict[str, Any]] = {}

    for sub_dir in sorted((session_dir / "subagents").glob("*")):
        if not sub_dir.is_dir():
            continue
        meta = _read_json(sub_dir / "meta.json")
        if not meta:
            continue

        agent_id = str(meta.get("agent_id") or sub_dir.name)
        run_id = f"run_sub_{agent_id}"
        wire_path = sub_dir / "wire.jsonl"
        records = _load_wire_records(wire_path)
        events, tool_calls, _ignored, usage = _normalize_wire_records(records, run_id=run_id)
        tool_call_owners.update(tool_calls)

        first_spawn = spawn_events.get(agent_id)
        run = {
            "run_id": run_id,
            "parent_run_id": root_run_id,
            "source_event_id": first_spawn["event_id"] if first_spawn else None,
            "role": "subagent",
            "launch_mode": _subagent_launch_mode(str(meta.get("status", ""))),
            "session_mode": "same_session",
            "agent_name": str(meta.get("subagent_type") or agent_id),
            "model": {
                "provider": None,
                "name": (meta.get("launch_spec") or {}).get("effective_model") or model_name,
            },
            "status": str(meta.get("status") or "unknown"),
            "task": {
                "description": meta.get("description"),
                "prompt": _safe_read_text(sub_dir / "prompt.txt"),
                "task_id": agent_id,
                "subagent_type": meta.get("subagent_type"),
            },
            "timing": {
                "created_at": meta.get("created_at"),
                "started_at": _records_started_at(records),
                "ended_at": _records_ended_at(records),
            },
            "resource_limits": {},
            "resource_usage": usage,
            "metrics": {},
            "events": events,
            "artifacts": [
                _artifact("native_subagent_wire", wire_path),
                _artifact("native_subagent_meta", sub_dir / "meta.json"),
                _artifact("native_subagent_prompt", sub_dir / "prompt.txt"),
            ],
            "extra": {
                "native_agent_id": agent_id,
                "native_paths": {
                    "wire": str(wire_path),
                    "meta": str(sub_dir / "meta.json"),
                    "prompt": str(sub_dir / "prompt.txt"),
                },
            },
        }
        subagent_runs[agent_id] = run
        runs.append(run)

    task_events_by_run: dict[str, list[dict[str, Any]]] = {}
    task_artifacts_by_run: dict[str, list[dict[str, Any]]] = {}

    tasks_dir = session_dir / "tasks"
    if tasks_dir.is_dir():
        for task_dir in sorted(tasks_dir.glob("*")):
            if not task_dir.is_dir():
                continue
            spec = _read_json(task_dir / "spec.json")
            runtime = _read_json(task_dir / "runtime.json")
            if not spec:
                continue

            owner_run_id = _resolve_task_owner_run_id(spec, tool_call_owners, subagent_runs, root_run_id)
            task_event = _background_task_event(spec, runtime)
            task_events_by_run.setdefault(owner_run_id, []).append(task_event)
            task_artifacts_by_run.setdefault(owner_run_id, []).extend(
                [
                    _artifact("native_background_task_spec", task_dir / "spec.json"),
                    _artifact("native_background_task_runtime", task_dir / "runtime.json"),
                    _artifact("native_background_task_output", task_dir / "output.log"),
                ]
            )

    root_run = {
        "run_id": root_run_id,
        "parent_run_id": None,
        "source_event_id": None,
        "role": "root",
        "launch_mode": "inline",
        "session_mode": "same_session",
        "agent_name": "orchestrator",
        "model": {"provider": None, "name": model_name},
        "status": _root_status(root_events),
        "task": None,
        "timing": {
            "created_at": _records_started_at(root_records),
            "started_at": _records_started_at(root_records),
            "ended_at": _records_ended_at(root_records),
        },
        "resource_limits": {},
        "resource_usage": root_usage,
        "metrics": {},
        "events": root_events + task_events_by_run.get(root_run_id, []),
        "artifacts": [_artifact("native_session_dir", session_dir), *task_artifacts_by_run.get(root_run_id, [])],
        "extra": {
            "native_session_id": session_dir.name,
            "native_paths": {
                "wire": str(root_wire_path),
            },
        },
    }

    # Attach task events/artifacts to subagent runs after all runs exist.
    for run in runs:
        run_id = run["run_id"]
        run["events"].extend(task_events_by_run.get(run_id, []))
        run["artifacts"].extend(task_artifacts_by_run.get(run_id, []))
        _merge_background_task_usage(run["resource_usage"], task_events_by_run.get(run_id, []))
        artifacts.extend(run["artifacts"])

    runs.insert(0, root_run)
    _merge_background_task_usage(root_run["resource_usage"], task_events_by_run.get(root_run_id, []))
    artifacts.extend(root_run["artifacts"])

    return {
        "schema_version": "AgentTrace-v0.1",
        "trace_id": trace_id,
        "benchmark": {},
        "agent_system": {
            "name": "kimi-cli",
            "version": agent_version,
            "entrypoint": "kimi_cli.__main__:main",
            "default_model": model_name,
            "trace_source": "native",
            "extra": {},
        },
        "root_run_id": root_run_id,
        "runs": runs,
        "artifacts": artifacts,
        "summary": {
            "subagent_count_total": len(subagent_runs),
            "background_task_count": sum(len(v) for v in task_events_by_run.values()),
        },
        "notes": "",
        "extra": {},
    }


def _load_wire_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        parsed = json.loads(raw)
        if parsed.get("type") == "metadata":
            continue
        if "timestamp" in parsed and "message" in parsed:
            records.append(parsed)
    return records


def _normalize_wire_records(
    records: list[dict[str, Any]],
    *,
    run_id: str,
    synthesize_subagent_spawns: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    tool_calls: dict[str, str] = {}
    spawn_events: dict[str, dict[str, Any]] = {}
    prompt_tokens = 0
    completion_tokens = 0
    step_id: str | None = None
    current_step_n: int | None = None
    seq = 0

    for record in records:
        timestamp = record["timestamp"]
        message = record["message"]
        msg_type = message.get("type", "unknown")
        payload = message.get("payload", {})

        if msg_type == "StepBegin":
            current_step_n = payload.get("n")
            step_id = f"{run_id}_step_{current_step_n}"

        if msg_type == "StatusUpdate":
            token_usage = payload.get("token_usage") or {}
            prompt_tokens += (
                int(token_usage.get("input_other", 0))
                + int(token_usage.get("input_cache_read", 0))
                + int(token_usage.get("input_cache_creation", 0))
            )
            completion_tokens += int(token_usage.get("output", 0))

        if msg_type == "ToolCall":
            tool_call_id = payload.get("id")
            if isinstance(tool_call_id, str) and tool_call_id:
                tool_calls[tool_call_id] = run_id

        if synthesize_subagent_spawns and msg_type == "SubagentEvent":
            agent_id = payload.get("agent_id")
            if isinstance(agent_id, str) and agent_id and agent_id not in spawn_events:
                seq += 1
                spawn = {
                    "event_id": f"{run_id}_evt_{seq:04d}",
                    "seq": seq,
                    "timestamp": timestamp,
                    "kind": "subagent_spawn",
                    "phase": "begin",
                    "step_id": step_id,
                    "parent_event_id": None,
                    "payload": {
                        "child_run_id": f"run_sub_{agent_id}",
                        "subagent_type": payload.get("subagent_type"),
                        "description": None,
                        "prompt": None,
                        "launch_mode": "foreground",
                        "task_id": agent_id,
                    },
                    "metrics": {},
                    "raw_ref": {
                        "source": "kimi-cli-wire",
                        "path": None,
                        "message_type": msg_type,
                    },
                    "extra": {
                        "parent_tool_call_id": payload.get("parent_tool_call_id"),
                    },
                }
                spawn_events[agent_id] = spawn
                events.append(spawn)

        seq += 1
        events.append(
            {
                "event_id": f"{run_id}_evt_{seq:04d}",
                "seq": seq,
                "timestamp": timestamp,
                "kind": _map_wire_kind(msg_type),
                "phase": _default_phase(msg_type),
                "step_id": step_id,
                "parent_event_id": None,
                "payload": payload,
                "metrics": _event_metrics(msg_type, payload),
                "raw_ref": {
                    "source": "kimi-cli-wire",
                    "path": None,
                    "message_type": msg_type,
                },
                "extra": {},
            }
        )

    usage = {
        "wall_time_ms": _wall_time_ms(records),
        "prompt_tokens": prompt_tokens or None,
        "completion_tokens": completion_tokens or None,
    }
    _merge_foreground_tool_usage(usage, events)
    return events, tool_calls, spawn_events, usage


def _map_wire_kind(msg_type: str) -> str:
    return {
        "TurnBegin": "turn_begin",
        "TurnEnd": "turn_end",
        "StepBegin": "step_begin",
        "StepInterrupted": "step_end",
        "CompactionBegin": "compaction",
        "CompactionEnd": "compaction",
        "StatusUpdate": "status",
        "ToolCall": "tool_call",
        "ToolResult": "tool_result",
        "SubagentEvent": "subagent_event",
        "ApprovalRequest": "approval_request",
        "ApprovalResponse": "approval_response",
        "HookTriggered": "hook",
        "HookResolved": "hook",
        "Notification": "status",
    }.get(msg_type, "message")


def _default_phase(msg_type: str) -> str:
    if msg_type.endswith("Begin"):
        return "begin"
    if msg_type.endswith("End") or msg_type in {"ToolResult", "ApprovalResponse", "HookResolved"}:
        return "completed"
    if msg_type in {"StatusUpdate", "Notification"}:
        return "update"
    return "completed"


def _event_metrics(msg_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if msg_type == "StatusUpdate":
        token_usage = payload.get("token_usage") or {}
        return {
            "prompt_tokens": (
                int(token_usage.get("input_other", 0))
                + int(token_usage.get("input_cache_read", 0))
                + int(token_usage.get("input_cache_creation", 0))
            ),
            "completion_tokens": int(token_usage.get("output", 0)),
        }
    if msg_type == "ToolResult":
        return_value = payload.get("return_value") or {}
        extras = return_value.get("extras") or {}
        shell_usage = extras.get("shell_resource_usage") or {}
        if shell_usage:
            return {
                "cpu_time_ms": shell_usage.get("cpu_time_ms"),
                "memory_current_kb": shell_usage.get("memory_current_kb"),
                "memory_peak_kb": shell_usage.get("memory_peak_kb"),
                "read_bytes": shell_usage.get("read_bytes"),
                "write_bytes": shell_usage.get("write_bytes"),
            }
    return {}


def _wall_time_ms(records: list[dict[str, Any]]) -> int | None:
    if not records:
        return None
    return int((records[-1]["timestamp"] - records[0]["timestamp"]) * 1000)


def _records_started_at(records: list[dict[str, Any]]) -> float | None:
    return records[0]["timestamp"] if records else None


def _records_ended_at(records: list[dict[str, Any]]) -> float | None:
    return records[-1]["timestamp"] if records else None


def _safe_read_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact(kind: str, path: Path) -> dict[str, Any]:
    return {"kind": kind, "path": str(path)}


def _subagent_launch_mode(status: str) -> str:
    if status == "running_background":
        return "background"
    if status in {"running_foreground", "completed", "failed", "killed", "idle"}:
        return "foreground"
    return "unknown"


def _root_status(events: list[dict[str, Any]]) -> str:
    if any(event["kind"] == "turn_end" for event in events):
        return "completed"
    return "running" if events else "unknown"


def _resolve_task_owner_run_id(
    spec: dict[str, Any],
    tool_call_owners: dict[str, str],
    subagent_runs: dict[str, dict[str, Any]],
    root_run_id: str,
) -> str:
    owner_role = spec.get("owner_role")
    owner_agent_id = spec.get("owner_agent_id")
    kind = spec.get("kind")

    # In current kimi-cli semantics, bash background tasks are root-owned.
    # This must take precedence over tool_call_id matching because tool call
    # ids are only unique within one run and can collide with subagent-local ids.
    if kind == "bash" and owner_role == "root":
        return root_run_id

    if owner_role == "subagent" and isinstance(owner_agent_id, str) and owner_agent_id in subagent_runs:
        return subagent_runs[owner_agent_id]["run_id"]

    if isinstance(owner_agent_id, str) and owner_agent_id in subagent_runs:
        return subagent_runs[owner_agent_id]["run_id"]
    kind_payload = spec.get("kind_payload") or {}
    if kind == "agent":
        agent_id = kind_payload.get("agent_id")
        if isinstance(agent_id, str) and agent_id in subagent_runs:
            return subagent_runs[agent_id]["run_id"]
    tool_call_id = spec.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id in tool_call_owners:
        return tool_call_owners[tool_call_id]
    return root_run_id


def _background_task_event(spec: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    task_id = spec.get("id", "unknown")
    updated_at = runtime.get("updated_at") or runtime.get("started_at") or runtime.get("finished_at")
    return {
        "event_id": f"task_evt_{task_id}",
        "seq": 10_000,
        "timestamp": updated_at,
        "kind": "background_task",
        "phase": "completed" if runtime.get("finished_at") is not None else "update",
        "step_id": None,
        "parent_event_id": None,
        "payload": {
            "task_id": task_id,
            "kind": spec.get("kind"),
            "description": spec.get("description"),
            "tool_call_id": spec.get("tool_call_id"),
            "owner_role": spec.get("owner_role"),
            "owner_agent_id": spec.get("owner_agent_id"),
            "owner_subagent_type": spec.get("owner_subagent_type"),
            "status": runtime.get("status"),
            "child_pid": runtime.get("child_pid"),
            "worker_pid": runtime.get("worker_pid"),
            "resource_scope": runtime.get("resource_scope"),
            "cgroup_path": runtime.get("cgroup_path"),
            "exit_code": runtime.get("exit_code"),
            "timed_out": runtime.get("timed_out"),
        },
        "metrics": {
            "wall_time_ms": _task_wall_time_ms(runtime),
            "cpu_time_ms": runtime.get("cpu_time_ms"),
            "memory_current_kb": runtime.get("memory_current_kb"),
            "memory_peak_kb": runtime.get("memory_peak_kb"),
            "read_bytes": runtime.get("read_bytes"),
            "write_bytes": runtime.get("write_bytes"),
        },
        "raw_ref": {
            "source": "kimi-cli-task",
            "path": None,
            "task_id": task_id,
        },
        "extra": {
            "kind_payload": spec.get("kind_payload"),
            "cwd": spec.get("cwd"),
        },
    }


def _task_wall_time_ms(runtime: dict[str, Any]) -> int | None:
    started_at = runtime.get("started_at")
    finished_at = runtime.get("finished_at")
    if started_at is None or finished_at is None:
        return None
    return int((finished_at - started_at) * 1000)


def _merge_background_task_usage(resource_usage: dict[str, Any], task_events: list[dict[str, Any]]) -> None:
    cpu_time_ms = 0
    memory_current_kb = 0
    memory_peak_kb = 0
    read_bytes = 0
    write_bytes = 0

    for event in task_events:
        metrics = event.get("metrics") or {}
        cpu_time_ms += int(metrics.get("cpu_time_ms") or 0)
        memory_current_kb = max(memory_current_kb, int(metrics.get("memory_current_kb") or 0))
        memory_peak_kb = max(memory_peak_kb, int(metrics.get("memory_peak_kb") or 0))
        read_bytes += int(metrics.get("read_bytes") or 0)
        write_bytes += int(metrics.get("write_bytes") or 0)

    if cpu_time_ms:
        resource_usage["cpu_time_ms"] = (resource_usage.get("cpu_time_ms") or 0) + cpu_time_ms
    if memory_current_kb:
        resource_usage["memory_current_kb"] = max(
            int(resource_usage.get("memory_current_kb") or 0),
            memory_current_kb,
        )
    if memory_peak_kb:
        resource_usage["memory_peak_kb"] = max(
            int(resource_usage.get("memory_peak_kb") or 0),
            memory_peak_kb,
        )
    if read_bytes:
        resource_usage["read_bytes"] = (resource_usage.get("read_bytes") or 0) + read_bytes
    if write_bytes:
        resource_usage["write_bytes"] = (resource_usage.get("write_bytes") or 0) + write_bytes


def _merge_foreground_tool_usage(resource_usage: dict[str, Any], events: list[dict[str, Any]]) -> None:
    cpu_time_ms = 0
    memory_current_kb = 0
    memory_peak_kb = 0
    read_bytes = 0
    write_bytes = 0

    for event in events:
        if event.get("kind") != "tool_result":
            continue
        payload = event.get("payload") or {}
        return_value = payload.get("return_value") or {}
        extras = return_value.get("extras") or {}
        shell_usage = extras.get("shell_resource_usage") or {}
        cpu_time_ms += int(shell_usage.get("cpu_time_ms") or 0)
        memory_current_kb = max(memory_current_kb, int(shell_usage.get("memory_current_kb") or 0))
        memory_peak_kb = max(memory_peak_kb, int(shell_usage.get("memory_peak_kb") or 0))
        read_bytes += int(shell_usage.get("read_bytes") or 0)
        write_bytes += int(shell_usage.get("write_bytes") or 0)

    if cpu_time_ms:
        resource_usage["cpu_time_ms"] = (resource_usage.get("cpu_time_ms") or 0) + cpu_time_ms
    if memory_current_kb:
        resource_usage["memory_current_kb"] = max(
            int(resource_usage.get("memory_current_kb") or 0),
            memory_current_kb,
        )
    if memory_peak_kb:
        resource_usage["memory_peak_kb"] = max(
            int(resource_usage.get("memory_peak_kb") or 0),
            memory_peak_kb,
        )
    if read_bytes:
        resource_usage["read_bytes"] = (resource_usage.get("read_bytes") or 0) + read_bytes
    if write_bytes:
        resource_usage["write_bytes"] = (resource_usage.get("write_bytes") or 0) + write_bytes
