from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "data"
    / "audit_v1_0_trajectories.py"
)
SPEC = importlib.util.spec_from_file_location("audit_v1_0_trajectories", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def _write_jsonl(path: Path, *events: dict[str, object]) -> None:
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def test_codex_todo_status_and_5xx_arrival_time_are_not_api_errors(
    tmp_path: Path,
) -> None:
    log = tmp_path / "codex.txt"
    _write_jsonl(
        log,
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "status": "completed",
                "error": None,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                [
                                    {
                                        "task_id": "later-task",
                                        "status": "pending",
                                        "at_sec": 599.0,
                                    }
                                ]
                            ),
                        }
                    ]
                },
            },
        },
    )

    assert AUDIT._codex_infra_errors(log) == []


def test_codex_structured_error_event_is_an_infra_error(tmp_path: Path) -> None:
    log = tmp_path / "codex.txt"
    _write_jsonl(
        log,
        {
            "type": "error",
            "message": "stream disconnected: response.failed event received",
        },
    )

    errors = AUDIT._codex_infra_errors(log)

    assert len(errors) == 1
    assert errors[0]["kind"] == "api_or_mcp"


def test_codex_failed_mcp_item_is_an_infra_error(tmp_path: Path) -> None:
    log = tmp_path / "codex.txt"
    _write_jsonl(
        log,
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "status": "failed",
                "error": "rmcp request failed",
            },
        },
    )

    assert len(AUDIT._codex_infra_errors(log)) == 1


def test_codex_failed_task_command_is_not_an_infra_error(tmp_path: Path) -> None:
    log = tmp_path / "codex.txt"
    _write_jsonl(
        log,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "status": "failed",
                "error": "tests exited with status 1",
            },
        },
    )

    assert AUDIT._codex_infra_errors(log) == []


def test_codex_unstructured_rmcp_transport_error_is_detected(tmp_path: Path) -> None:
    log = tmp_path / "codex.txt"
    log.write_text("rmcp request failed with HTTP 502 Bad Gateway\n")

    errors = AUDIT._codex_infra_errors(log)

    assert len(errors) == 1
    assert errors[0]["kind"] == "mcp_transport"
