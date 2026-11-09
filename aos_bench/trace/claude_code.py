"""Claude Code native transcripts to AgentTrace.

This collector intentionally uses the same ``IncrementalClaudeParser`` as the
live monitor. Native transcripts remain the source of truth; AgentTrace is the
stable interchange and visualization contract.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from aos_bench.monitor.tail.claude import _find_root_session_dir, _find_share_dir
from aos_bench.monitor.tail.claude_parser import (
    IncrementalClaudeParser,
    parse_claude_line,
)
from aos_bench.trace.native_common import artifact, build_run, summarize, trial_status


ROOT_RUN_ID = "run_root_001"


def collect_claude_code_agent_trace(
    trial_dir: Path | str,
    *,
    trace_id: str | None = None,
    model_name: str | None = None,
    agent_version: str = "unknown",
    benchmark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trial_path = Path(trial_dir).resolve()
    share_dir = _find_share_dir(trial_path)
    if share_dir is None:
        raise FileNotFoundError(
            f"Claude Code sessions/projects not found under {trial_path}"
        )
    session_dir = _find_root_session_dir(share_dir)
    if session_dir is None:
        raise FileNotFoundError(f"Claude Code transcript not found under {share_dir}")

    labels = _load_agent_labels(session_dir)
    parsers: dict[str, IncrementalClaudeParser] = {
        ROOT_RUN_ID: IncrementalClaudeParser(
            run_id=ROOT_RUN_ID, synthesize_subagent_spawns=True
        )
    }
    events_by_run: dict[str, list[dict[str, Any]]] = {ROOT_RUN_ID: []}
    paths_by_run: dict[str, list[Path]] = {ROOT_RUN_ID: []}
    native_agent_ids: dict[str, str] = {}

    root_files = sorted(session_dir.glob("*.jsonl"))
    sub_files = sorted(
        path
        for path in session_dir.rglob("*.jsonl")
        if "subagents" in path.parts and "workflows" not in path.parts
    )
    seen_records: set[str] = set()
    for path in [*root_files, *sub_files]:
        for record in _load_records(path):
            # Newer Claude builds may mirror an inline sidechain record into a
            # dedicated subagent file. UUID is stable across both copies.
            record_uuid = record.get("uuid")
            if isinstance(record_uuid, str) and record_uuid:
                if record_uuid in seen_records:
                    continue
                seen_records.add(record_uuid)
            agent_id = record.get("agentId")
            is_sidechain = bool(record.get("isSidechain")) and bool(agent_id)
            run_id = f"run_sub_{agent_id}" if is_sidechain else ROOT_RUN_ID
            if run_id not in parsers:
                parsers[run_id] = IncrementalClaudeParser(run_id=run_id)
                events_by_run[run_id] = []
                paths_by_run[run_id] = []
                native_agent_ids[run_id] = str(agent_id)
            if path not in paths_by_run[run_id]:
                paths_by_run[run_id].append(path)
            events_by_run[run_id].extend(parsers[run_id].feed([record]))

    source_by_child = _link_claude_spawns(events_by_run, native_agent_ids)
    runs: list[dict[str, Any]] = []
    top_artifacts: list[dict[str, Any]] = []
    for run_id in [ROOT_RUN_ID, *sorted(r for r in parsers if r != ROOT_RUN_ID)]:
        parser = parsers[run_id]
        run_artifacts = [
            artifact("native_claude_transcript", path, base_dir=trial_path)
            for path in paths_by_run[run_id]
        ]
        top_artifacts.extend(run_artifacts)
        native_id = native_agent_ids.get(run_id)
        run_model = _last_model(events_by_run[run_id]) or model_name
        runs.append(
            build_run(
                run_id=run_id,
                parent_run_id=None if run_id == ROOT_RUN_ID else ROOT_RUN_ID,
                source_event_id=source_by_child.get(run_id),
                role="root" if run_id == ROOT_RUN_ID else "subagent",
                agent_name=(
                    "claude-code"
                    if run_id == ROOT_RUN_ID
                    else labels.get(native_id or "", native_id or "claude-subagent")
                ),
                model_name=run_model,
                events=events_by_run[run_id],
                prompt_tokens=parser.prompt_tokens,
                completion_tokens=parser.completion_tokens,
                first_ts=parser.first_ts,
                last_ts=parser.last_ts,
                artifacts=run_artifacts,
                session_mode="same_session",
                status=trial_status(trial_path) if run_id == ROOT_RUN_ID else None,
                task={"task_id": native_id} if native_id else None,
                extra={"native_agent_id": native_id, "is_sidechain": bool(native_id)},
            )
        )

    return {
        "schema_version": "AgentTrace-v0.1",
        "trace_id": trace_id
        or f"trace_claude_{session_dir.name}_{uuid.uuid4().hex[:8]}",
        "benchmark": benchmark or {},
        "agent_system": {
            "name": "claude-code",
            "version": agent_version,
            "entrypoint": "claude --output-format=stream-json",
            "default_model": model_name,
            "trace_source": "native",
            "extra": {},
        },
        "root_run_id": ROOT_RUN_ID,
        "runs": runs,
        "artifacts": top_artifacts,
        "summary": summarize(runs),
        "notes": "",
        "extra": {"native_session_dir": str(session_dir.relative_to(trial_path))},
    }


def _load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        record = parse_claude_line(line)
        if record is not None:
            records.append(record)
    return records


def _load_agent_labels(session_dir: Path) -> dict[str, str]:
    labels: dict[str, str] = {}
    for meta_path in session_dir.rglob("agent-*.meta.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        description = str(data.get("description") or "").strip()
        if description:
            labels[meta_path.name.removeprefix("agent-").removesuffix(".meta.json")] = (
                description[:80]
            )
    return labels


def _link_claude_spawns(
    events_by_run: dict[str, list[dict[str, Any]]],
    native_agent_ids: dict[str, str],
) -> dict[str, str]:
    root_events = events_by_run[ROOT_RUN_ID]
    pending = [event for event in root_events if event.get("kind") == "subagent_spawn"]
    source_by_child: dict[str, str] = {}
    children = sorted(
        native_agent_ids,
        key=lambda run_id: (
            events_by_run[run_id][0].get("timestamp")
            if events_by_run[run_id]
            and events_by_run[run_id][0].get("timestamp") is not None
            else float("inf")
        ),
    )
    for child_run_id, spawn in zip(children, pending, strict=False):
        native_id = native_agent_ids[child_run_id]
        spawn["payload"]["child_run_id"] = child_run_id
        spawn["payload"]["task_id"] = native_id
        source_by_child[child_run_id] = spawn["event_id"]
    return source_by_child


def _last_model(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("kind") == "step_begin":
            model = (event.get("payload") or {}).get("model")
            if isinstance(model, str) and model:
                return model
    return None
