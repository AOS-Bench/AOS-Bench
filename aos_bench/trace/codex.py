"""Codex rollout JSONL to AgentTrace using the live monitor parser."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from aos_bench.monitor.tail.codex import (
    _find_all_rollouts,
    _find_share_dir,
    _peek_session_meta,
)
from aos_bench.monitor.tail.codex_parser import IncrementalCodexParser, parse_codex_line
from aos_bench.trace.native_common import artifact, build_run, summarize, trial_status


ROOT_RUN_ID = "run_root_001"


def collect_codex_agent_trace(
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
            f"Codex sessions date tree not found under {trial_path}"
        )
    rollouts = _find_all_rollouts(share_dir)
    if not rollouts:
        raise FileNotFoundError(f"Codex rollout JSONL not found under {share_dir}")

    inventory: list[tuple[Path, dict[str, Any]]] = []
    root_thread_id: str | None = None
    for path in rollouts:
        meta = _peek_session_meta(path)
        if meta is None:
            continue
        if root_thread_id is None and not meta.get("parent_thread_id"):
            root_thread_id = meta.get("id")
        inventory.append((path, meta))
    if not inventory:
        raise ValueError(f"No readable Codex session_meta records under {share_dir}")
    root_thread_id = root_thread_id or inventory[0][1].get("id")

    run_id_by_thread = {
        str(meta.get("id")): (
            ROOT_RUN_ID
            if meta.get("id") == root_thread_id
            else f"run_sub_{meta.get('id')}"
        )
        for _, meta in inventory
        if meta.get("id")
    }
    parsed: list[
        tuple[Path, dict[str, Any], str, IncrementalCodexParser, list[dict[str, Any]]]
    ] = []
    spawn_source: dict[str, str] = {}
    for path, meta in inventory:
        thread_id = str(meta.get("id") or path.stem)
        run_id = run_id_by_thread.get(thread_id, f"run_sub_{thread_id}")
        parser = IncrementalCodexParser(run_id=run_id)
        records = _load_records(path)
        events = parser.feed(records)
        for event in events:
            if event.get("kind") == "subagent_spawn":
                child_run_id = (event.get("payload") or {}).get("child_run_id")
                if isinstance(child_run_id, str):
                    spawn_source[child_run_id] = event["event_id"]
        parsed.append((path, meta, run_id, parser, events))

    runs: list[dict[str, Any]] = []
    top_artifacts: list[dict[str, Any]] = []
    for path, meta, run_id, parser, events in parsed:
        thread_id = str(meta.get("id") or path.stem)
        parent_thread_id = meta.get("parent_thread_id")
        parent_run_id = (
            None
            if run_id == ROOT_RUN_ID
            else run_id_by_thread.get(str(parent_thread_id), ROOT_RUN_ID)
        )
        run_artifacts = [artifact("native_codex_rollout", path, base_dir=trial_path)]
        top_artifacts.extend(run_artifacts)
        nickname = meta.get("agent_nickname")
        role = "root" if run_id == ROOT_RUN_ID else "subagent"
        runs.append(
            build_run(
                run_id=run_id,
                parent_run_id=parent_run_id,
                source_event_id=spawn_source.get(run_id),
                role=role,
                agent_name=(
                    "codex" if role == "root" else f"codex/{nickname or thread_id[:24]}"
                ),
                model_name=parser.default_model or model_name,
                events=events,
                prompt_tokens=parser.prompt_tokens,
                completion_tokens=parser.completion_tokens,
                first_ts=parser.first_ts,
                last_ts=parser.last_ts,
                artifacts=run_artifacts,
                session_mode="child_session" if role == "subagent" else "same_session",
                status=trial_status(trial_path) if role == "root" else None,
                task={"task_id": thread_id, "subagent_type": parser.agent_role}
                if role == "subagent"
                else None,
                extra={
                    "native_thread_id": thread_id,
                    "native_parent_thread_id": parent_thread_id,
                    "native_session_id": parser.session_id,
                    "originator": parser.originator,
                    "cli_version": parser.cli_version,
                },
            )
        )
    runs.sort(
        key=lambda run: (run["run_id"] != ROOT_RUN_ID, run["timing"]["started_at"] or 0)
    )

    detected_version = next(
        (parser.cli_version for _, _, _, parser, _ in parsed if parser.cli_version),
        agent_version,
    )
    return {
        "schema_version": "AgentTrace-v0.1",
        "trace_id": trace_id or f"trace_codex_{root_thread_id}_{uuid.uuid4().hex[:8]}",
        "benchmark": benchmark or {},
        "agent_system": {
            "name": "codex",
            "version": detected_version,
            "entrypoint": "codex exec --json",
            "default_model": model_name,
            "trace_source": "native",
            "extra": {},
        },
        "root_run_id": ROOT_RUN_ID,
        "runs": runs,
        "artifacts": top_artifacts,
        "summary": summarize(runs),
        "notes": "",
        "extra": {"native_sessions_dir": str(share_dir.relative_to(trial_path))},
    }


def _load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        record = parse_codex_line(line)
        if record is not None:
            records.append(record)
    return records
