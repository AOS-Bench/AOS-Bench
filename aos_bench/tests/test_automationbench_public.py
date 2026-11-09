import json
from pathlib import Path

from aos_bench.runner.automationbench_public import (
    SOURCE_REVISION,
    build_parser,
    build_upstream_command,
    validate_results,
)


def test_public_smoke_command_is_pinned_to_one_serial_example(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--source-dir",
            str(tmp_path / "source"),
            "--smoke",
            "--model",
            "test-model",
            "--base-url",
            "https://gateway.example/v1",
        ]
    )

    command = build_upstream_command(args, tmp_path / "run")

    assert SOURCE_REVISION == "a321764ace3cfbe42289e6a13abef2f0f4f56fad"
    assert command[command.index("--domains") + 1] == "public"
    assert command[command.index("--num-examples") + 1] == "1"
    assert command[command.index("--max-concurrent") + 1] == "1"
    assert "--no-ensure-complete" in command
    assert not any("api-key" in part and part != "--api-key-var" for part in command)


def test_result_health_rejects_zero_output_rollouts(tmp_path: Path) -> None:
    result = tmp_path / "results.json"
    result.write_text(
        json.dumps(
            {
                "summary": {"tasks_with_errors": 0, "tasks_with_zero_output": 1},
                "tasks": [{"name": "sales.example", "passed": False}],
            }
        )
    )

    assert validate_results(result) == ["tasks_with_zero_output=1"]


def test_result_health_does_not_require_task_pass(tmp_path: Path) -> None:
    result = tmp_path / "results.json"
    result.write_text(
        json.dumps(
            {
                "summary": {"tasks_with_errors": 0, "tasks_with_zero_output": 0},
                "tasks": [{"name": "sales.example", "passed": False}],
            }
        )
    )

    assert validate_results(result) == []


def test_public_full_command_uses_requested_concurrency(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--source-dir",
            str(tmp_path / "source"),
            "--max-concurrent",
            "24",
            "--num-examples",
            "60",
            "--no-ensure-complete",
        ]
    )

    command = build_upstream_command(args, tmp_path / "run")

    assert command[command.index("--max-concurrent") + 1] == "24"
    assert command[command.index("--num-examples") + 1] == "60"
    assert "--no-ensure-complete" in command
