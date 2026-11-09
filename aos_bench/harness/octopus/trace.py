"""AgentTrace v0.1 writer.

Two output channels:

  - Live event log: append-only JSONL, one event per line. Useful for
    `tail -f` while the agent is running.
  - Final trace: complete `agent_trace.json` written when the run ends.

See ``docs/agent_trace.md`` for the schema. M2 supports multiple runs:
one root + N subagent runs (foreground or background) linked by
``parent_run_id``.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "AgentTrace-v0.1"


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class Run:
    run_id: str
    parent_run_id: str | None
    role: str
    agent_name: str
    model: dict[str, Any]
    resource_limits: dict[str, Any] = field(default_factory=dict)
    launch_mode: str = "foreground"
    session_mode: str = "same_session"
    status: str = "running"
    timing: dict[str, float] = field(default_factory=dict)
    resource_usage: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    _seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "role": self.role,
            "launch_mode": self.launch_mode,
            "session_mode": self.session_mode,
            "agent_name": self.agent_name,
            "model": self.model,
            "status": self.status,
            "timing": self.timing,
            "resource_limits": self.resource_limits,
            "resource_usage": self.resource_usage,
            "metrics": self.metrics,
            "events": self.events,
            "artifacts": self.artifacts,
            "extra": self.extra,
        }


class TraceWriter:
    """Owns one trace + a registry of runs (root + subagents).

    Thread-safe (uses a lock) so async tasks can call ``emit`` concurrently
    without interleaving JSON lines in the event log.
    """

    def __init__(
        self,
        *,
        agent_system: dict[str, Any],
        benchmark: dict[str, Any] | None = None,
        event_log_path: Path | None = None,
        trace_out_path: Path | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._agent_system = agent_system
        self._benchmark = benchmark or {}
        self._trace_id = _new_id("octopus")
        self._event_log_path = event_log_path
        self._trace_out_path = trace_out_path
        self._event_log_fh = None
        if event_log_path is not None:
            event_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._event_log_fh = event_log_path.open("a", buffering=1)
        self._artifacts: list[dict[str, Any]] = []
        self._notes: str = ""
        self._extra: dict[str, Any] = {}
        self.root: Run | None = None
        self._runs: dict[str, Run] = {}

    def start_root(
        self,
        *,
        agent_name: str,
        model: dict[str, Any],
        resource_limits: dict[str, Any],
    ) -> Run:
        with self._lock:
            run = Run(
                run_id=_new_id("run_root"),
                parent_run_id=None,
                role="root",
                agent_name=agent_name,
                model=model,
                resource_limits=resource_limits,
                timing={"created_at": _now(), "started_at": _now()},
            )
            self._runs[run.run_id] = run
            self.root = run
            self._emit_locked(
                run, "status", phase="begin", payload={"status": "running"}
            )
            return run

    def start_subagent(
        self,
        *,
        parent_run_id: str,
        subagent_type: str,
        agent_name: str | None = None,
        model: dict[str, Any],
        resource_limits: dict[str, Any] | None = None,
        launch_mode: str = "foreground",
        source_event_id: str | None = None,
    ) -> Run:
        with self._lock:
            run = Run(
                run_id=_new_id("run_sub"),
                parent_run_id=parent_run_id,
                role="subagent",
                agent_name=agent_name or subagent_type,
                launch_mode=launch_mode,
                session_mode="child_session",
                model=model,
                resource_limits=resource_limits or {},
                timing={"created_at": _now(), "started_at": _now()},
                extra={"subagent_type": subagent_type,
                       "source_event_id": source_event_id},
            )
            self._runs[run.run_id] = run
            self._emit_locked(
                run, "status", phase="begin", payload={"status": "running"}
            )
            return run

    def emit(
        self,
        run: Run,
        kind: str,
        *,
        phase: str | None = None,
        step_id: str | None = None,
        parent_event_id: str | None = None,
        payload: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        with self._lock:
            return self._emit_locked(
                run,
                kind,
                phase=phase,
                step_id=step_id,
                parent_event_id=parent_event_id,
                payload=payload,
                metrics=metrics,
            )

    def _emit_locked(
        self,
        run: Run,
        kind: str,
        *,
        phase: str | None = None,
        step_id: str | None = None,
        parent_event_id: str | None = None,
        payload: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        run._seq += 1
        evt: dict[str, Any] = {
            "event_id": _new_id("evt"),
            "seq": run._seq,
            "timestamp": _now(),
            "kind": kind,
            "payload": payload or {},
        }
        if phase is not None:
            evt["phase"] = phase
        if step_id is not None:
            evt["step_id"] = step_id
        if parent_event_id is not None:
            evt["parent_event_id"] = parent_event_id
        if metrics is not None:
            evt["metrics"] = metrics
        run.events.append(evt)
        if self._event_log_fh is not None:
            line = json.dumps(
                {"trace_id": self._trace_id, "run_id": run.run_id, **evt},
                ensure_ascii=False,
                default=str,
            )
            self._event_log_fh.write(line + "\n")
        return evt["event_id"]

    def finish_run(
        self,
        run: Run,
        *,
        status: str,
        stop_reason: str,
        usage: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            run.status = status
            run.timing["ended_at"] = _now()
            wall_ms = int(
                (run.timing["ended_at"] - run.timing["started_at"]) * 1000
            )
            usage = dict(usage or {})
            usage.setdefault("wall_time_ms", wall_ms)
            run.resource_usage.update(usage)
            run.metrics["stop_reason"] = stop_reason
            self._emit_locked(
                run,
                "status",
                phase="completed",
                payload={"status": status, "stop_reason": stop_reason},
                metrics={"wall_time_ms": wall_ms},
            )

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def add_artifact(self, kind: str, path: str, **extra: Any) -> None:
        with self._lock:
            self._artifacts.append({"kind": kind, "path": path, **extra})

    def set_notes(self, notes: str) -> None:
        with self._lock:
            self._notes = notes

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            runs_sorted = sorted(
                self._runs.values(),
                key=lambda r: r.timing.get("created_at", 0),
            )
            runs = [r.to_dict() for r in runs_sorted]
            summary: dict[str, Any] = {}
            if self.root is not None:
                summary = {
                    "total_steps": self.root.metrics.get("step_count", 0),
                    "stop_reason": self.root.metrics.get("stop_reason"),
                    "wall_time_ms": self.root.resource_usage.get("wall_time_ms"),
                    "prompt_tokens_total": sum(
                        int(r.resource_usage.get("prompt_tokens", 0) or 0)
                        for r in self._runs.values()
                    ),
                    "completion_tokens_total": sum(
                        int(r.resource_usage.get("completion_tokens", 0) or 0)
                        for r in self._runs.values()
                    ),
                    "subagent_count": sum(
                        1 for r in self._runs.values() if r.role == "subagent"
                    ),
                }
            return {
                "schema_version": SCHEMA_VERSION,
                "trace_id": self._trace_id,
                "benchmark": self._benchmark,
                "agent_system": self._agent_system,
                "root_run_id": self.root.run_id if self.root else None,
                "runs": runs,
                "artifacts": self._artifacts,
                "summary": summary,
                "notes": self._notes,
                "extra": self._extra,
            }

    def flush_to_disk(self) -> None:
        if self._trace_out_path is not None:
            self._trace_out_path.parent.mkdir(parents=True, exist_ok=True)
            self._trace_out_path.write_text(
                json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str)
            )

    def close(self) -> None:
        with self._lock:
            if self._event_log_fh is not None:
                try:
                    self._event_log_fh.flush()
                    os.fsync(self._event_log_fh.fileno())
                except OSError:
                    pass
                self._event_log_fh.close()
                self._event_log_fh = None
