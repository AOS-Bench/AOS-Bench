"""
ArrivalSchedule: defines when tasks are injected into the agent session.

Two supported input formats:

1. Flat schedule TOML/JSON (arrivals carry instruction inline):

    max_steps = 50

    [[arrivals]]
    step = 0
    task_id = "grade-python-001"
    instruction = "Grade the Python files in /workspace/submissions/..."

2. Stream directory (StreamConfig layout, see harbor.models.stream):

    <stream_dir>/
      stream.toml        # metadata / environment / session / arrivals
      tasks/
        <task_id>/
          instruction.md   # loaded as `instruction`
          tests/test.sh

   Use `ArrivalSchedule.from_stream_toml(<stream_dir>)` — it reads
   `stream.toml`, then per arrival loads `tasks/<task_id>/instruction.md`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class Arrival:
    step: int
    task_id: str
    instruction: str
    deadline_step: int | None = None

    def to_user_message(self) -> str:
        return f"[NEW TASK: {self.task_id}]\n{self.instruction}"


@dataclass
class ArrivalSchedule:
    arrivals: list[Arrival] = field(default_factory=list)
    max_steps: int = 50

    def validate(self) -> None:
        """Raise ValueError on any semantic inconsistency."""
        seen_ids: set[str] = set()
        for a in self.arrivals:
            if a.step < 0:
                raise ValueError(f"task '{a.task_id}': step must be >= 0, got {a.step}")
            if a.task_id in seen_ids:
                raise ValueError(f"duplicate task_id: '{a.task_id}'")
            seen_ids.add(a.task_id)
            if not a.task_id:
                raise ValueError("task_id must not be empty")
            if not a.instruction:
                raise ValueError(f"task '{a.task_id}': instruction must not be empty")
            if a.deadline_step is not None and a.deadline_step <= a.step:
                raise ValueError(
                    f"task '{a.task_id}': deadline_step ({a.deadline_step}) "
                    f"must be > arrival step ({a.step})"
                )

        if self.arrivals:
            max_arrival_step = max(a.step for a in self.arrivals)
            if self.max_steps <= max_arrival_step:
                raise ValueError(
                    f"max_steps ({self.max_steps}) must be > the latest arrival step "
                    f"({max_arrival_step}); agent needs at least one step after the last injection"
                )

    @classmethod
    def from_toml(cls, path: Path) -> "ArrivalSchedule":
        try:
            data = tomllib.loads(path.read_text())
        except FileNotFoundError:
            raise FileNotFoundError(f"schedule file not found: {path}")
        except Exception as e:
            raise ValueError(f"failed to parse schedule TOML '{path}': {e}") from e

        arrivals = [
            Arrival(
                step=a["step"],
                task_id=a["task_id"],
                instruction=a["instruction"],
                deadline_step=a.get("deadline_step"),
            )
            for a in data.get("arrivals", [])
        ]
        schedule = cls(
            arrivals=sorted(arrivals, key=lambda a: a.step),
            max_steps=data.get("max_steps", 50),
        )
        schedule.validate()
        return schedule

    @classmethod
    def from_json(cls, path: Path) -> "ArrivalSchedule":
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            raise FileNotFoundError(f"schedule file not found: {path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"failed to parse schedule JSON '{path}': {e}") from e

        arrivals = [Arrival(**a) for a in data.get("arrivals", [])]
        schedule = cls(
            arrivals=sorted(arrivals, key=lambda a: a.step),
            max_steps=data.get("max_steps", 50),
        )
        schedule.validate()
        return schedule

    @classmethod
    def from_stream_toml(cls, stream_path: Path) -> "ArrivalSchedule":
        """
        Load an ArrivalSchedule from a StreamConfig-style layout.

        `stream_path` may point either at the stream directory or directly at
        `stream.toml` within it. For each arrival in stream.toml, the
        corresponding `tasks/<task_id>/instruction.md` is read and used as the
        injected instruction.
        """
        if stream_path.is_dir():
            stream_dir = stream_path
            manifest = stream_dir / "stream.toml"
        else:
            manifest = stream_path
            stream_dir = manifest.parent

        if not manifest.exists():
            raise FileNotFoundError(f"stream manifest not found: {manifest}")

        try:
            data = tomllib.loads(manifest.read_text())
        except Exception as e:
            raise ValueError(f"failed to parse stream TOML '{manifest}': {e}") from e

        session = data.get("session") or {}
        max_steps = session.get("max_steps")
        if max_steps is None:
            raise ValueError(f"stream '{manifest}' missing [session].max_steps")

        tasks_root = stream_dir / "tasks"
        arrivals: list[Arrival] = []
        for entry in data.get("arrivals", []):
            task_id = entry["task_id"]
            instruction_path = tasks_root / task_id / "instruction.md"
            if not instruction_path.exists():
                raise FileNotFoundError(
                    f"instruction not found for task '{task_id}': {instruction_path}"
                )
            arrivals.append(
                Arrival(
                    step=entry["step"],
                    task_id=task_id,
                    instruction=instruction_path.read_text().strip(),
                    deadline_step=entry.get("deadline_step"),
                )
            )

        schedule = cls(
            arrivals=sorted(arrivals, key=lambda a: a.step),
            max_steps=max_steps,
        )
        schedule.validate()
        return schedule

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps({
            "max_steps": self.max_steps,
            "arrivals": [
                {
                    "step": a.step,
                    "task_id": a.task_id,
                    "instruction": a.instruction,
                    "deadline_step": a.deadline_step,
                }
                for a in self.arrivals
            ],
        }, indent=2))
