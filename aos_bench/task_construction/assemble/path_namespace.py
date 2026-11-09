"""Workspace allocation for source tasks merged into one stream.

Each source task still gets a unique `task_id` for arrivals, verifier output,
and staging under `environment/_sources/<task_id>/`. Runtime files are no
longer placed under a per-task namespace: task-local paths such as `/app/foo`
map directly to `/workspace/foo` in the merged container. If two tasks target
the same final path, collision handling renames the later one and the path
rewrite rules keep Dockerfile, instructions, tests, and solutions in sync.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Characters that are awkward in shell paths; replace when sanitizing.
_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True)
class TaskSlot:
    """One source task's allocated metadata inside the merged stream."""

    source_dir: Path
    """Where the source task lives (read-only). Has task.toml + environment/ + tests/."""

    pool: str
    """Pool/domain label (e.g. swe, ml, sci, ops, sec, data, wcb). Metadata only."""

    task_id: str
    """Effective task_id used inside the stream — may differ from source if there was a collision."""

    workspace_dir: str
    """Shared runtime workspace root inside the container (always /workspace)."""

    source_workdir: str
    """The original WORKDIR set by the source Dockerfile (for path-rewrite). Defaults to /app."""

    @property
    def sources_subdir(self) -> str:
        """Where this task's environment/ contents are staged inside <stream>/environment/_sources/<id>/."""
        return f"_sources/{self.task_id}"


def _sanitize(value: str) -> str:
    return _SAFE_RE.sub("-", value).strip("-")


class SlotAllocator:
    """Assigns unique task_ids when source task names collide."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def allocate(
        self,
        *,
        source_dir: Path,
        pool: str,
        desired_task_id: str | None = None,
        source_workdir: str = "/app",
    ) -> TaskSlot:
        pool = _sanitize(pool)
        base = _sanitize(desired_task_id or source_dir.name)
        if not base:
            raise ValueError(f"Cannot derive a task_id from {source_dir}")

        task_id = base
        suffix = 2
        while task_id in self._used:
            task_id = f"{base}-{suffix}"
            suffix += 1
        self._used.add(task_id)

        return TaskSlot(
            source_dir=source_dir,
            pool=pool,
            task_id=task_id,
            workspace_dir="/workspace",
            source_workdir=source_workdir.rstrip("/") or "/",
        )
