from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


VerifyKind = Literal["online", "final"]
VerifyTrigger = Literal["complete", "session_end"]


@dataclass
class VerifyResult:
    kind: VerifyKind
    task_id: str
    attempt: int
    trigger: VerifyTrigger
    trigger_step: int | None
    trigger_ts: float | None
    started_ts: float
    finished_ts: float
    duration_sec: float
    reward: float | None
    passed: bool
    exit_code: int | None
    error: str | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    reward_path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))


@dataclass
class TaskVerificationRecord:
    task_id: str
    online_results: list[VerifyResult] = field(default_factory=list)
    final_result: VerifyResult | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "online_results": [result.to_dict() for result in self.online_results],
            "final_result": self.final_result.to_dict() if self.final_result else None,
        }
