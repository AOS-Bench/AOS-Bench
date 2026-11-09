from __future__ import annotations

import json
from pathlib import Path

import pytest

from aos_bench.task_construction.assemble.refine_merge_stream.orchestrator import (
    OVERLAP_METRIC,
)
from aos_bench.task_construction.scripts import v1_low_overlap_scan as scan


def _write_stream(path: Path, task_ids: list[str]) -> None:
    path.mkdir(parents=True)
    arrivals = "\n".join(
        f'[[arrivals]]\narrive_wall_time = {index}\ntask_id = "{task_id}"\n'
        for index, task_id in enumerate(task_ids)
    )
    (path / "stream.toml").write_text(
        'version = "1.0"\n[metadata]\nname = "candidate"\n' + arrivals
    )


def _write_passed_state(
    run_root: Path,
    record: dict[str, object],
    stream_dir: Path,
    task_ids: list[str],
) -> Path:
    state_path = (
        run_root / "results" / str(record["bucket"]) / str(record["name"])
        / "state.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "status": "passed",
        "attempt": 3,
        "stream_dir": str(stream_dir),
        "rounds": [{
            "build": {"passed": True},
            "oracle": {
                "passed": True,
                "details": {
                    "returncode": 0,
                    "failed_tasks": [],
                    "missing_tasks": [],
                    "rewards": {task_id: 1.0 for task_id in task_ids},
                },
            },
        }],
    }))
    return state_path


def _write_registry(path: Path, state_path: Path, stream_dir: Path, tasks: list[str]) -> None:
    path.write_text(json.dumps({
        "schema_version": 1,
        "overlap_metric": OVERLAP_METRIC,
        "max_overlap_ratio_exclusive": 0.60,
        "streams": [{
            "name": stream_dir.name,
            "state_path": str(state_path.resolve()),
            "stream_dir": str(stream_dir),
            "task_ids": tasks,
        }],
    }))


def test_successful_uses_final_canonical_tasks_instead_of_manifest(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    stream_dir = tmp_path / "candidate"
    final_tasks = ["replacement-a", "replacement-b"]
    _write_stream(stream_dir, final_tasks)
    record = {
        "bucket": "heavy",
        "name": "candidate",
        "path": str(stream_dir),
        "tasks": ["stale-a", "stale-b"],
    }
    state_path = _write_passed_state(run_root, record, stream_dir, final_tasks)
    registry = run_root / "successful-streams.json"
    _write_registry(registry, state_path, stream_dir, final_tasks)

    result = scan._successful(record, run_root, registry)

    assert result is not None
    assert result["tasks"] == final_tasks
    assert set(result["rewards"]) == set(final_tasks)


def test_successful_requires_registry_membership_even_before_file_exists(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    stream_dir = tmp_path / "candidate"
    tasks = ["a", "b"]
    _write_stream(stream_dir, tasks)
    record = {
        "bucket": "heavy",
        "name": "candidate",
        "path": str(stream_dir),
        "tasks": tasks,
    }
    _write_passed_state(run_root, record, stream_dir, tasks)

    assert scan._successful(
        record,
        run_root,
        run_root / "successful-streams.json",
    ) is None


def test_monitor_snapshot_scopes_target_and_active_workers_to_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    record = {
        "bucket": "heavy",
        "name": "candidate",
        "path": str(tmp_path / "candidate"),
        "tasks": ["a"],
    }
    run_dir = run_root / "results" / "heavy" / "candidate"
    unrelated = tmp_path / "another-run" / "results" / "heavy" / "other"
    monkeypatch.setattr(
        scan,
        "_active_run_dirs",
        lambda: {str(run_dir.resolve()), str(unrelated.resolve())},
    )

    snapshot = scan._monitor_snapshot(
        [record],
        run_root,
        run_root / "successful-streams.json",
        {"heavy": 4},
    )

    assert snapshot["target"] == {"heavy": 4}
    assert snapshot["active_workers"] == 1
    assert snapshot["active_run_dirs"] == [str(run_dir.resolve())]


def test_monitor_snapshot_counts_historical_registry_entries_globally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    registry = run_root / "successful-streams.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({
        "schema_version": 1,
        "overlap_metric": OVERLAP_METRIC,
        "max_overlap_ratio_exclusive": 0.60,
        "max_successful_streams": None,
        "streams": [
            {
                "name": "light-historical",
                "bucket": "light",
                "state_path": "/another/run/state.json",
                "task_ids": ["historical-light"],
            },
            {
                "name": "medium-historical",
                "bucket": "medium",
                "state_path": "/another/run/state.json",
                "task_ids": ["historical-medium"],
            },
        ],
    }))
    record = {
        "bucket": "heavy",
        "name": "candidate",
        "path": str(tmp_path / "candidate"),
        "tasks": ["candidate-heavy"],
    }
    monkeypatch.setattr(scan, "_active_run_dirs", set)

    snapshot = scan._monitor_snapshot(
        [record],
        run_root,
        registry,
        {"light": 10, "medium": 5, "heavy": 5},
    )

    assert snapshot["fully_passed"] == {
        "light": 0,
        "medium": 0,
        "heavy": 0,
    }
    assert snapshot["registered_successes"] == {
        "light": 1,
        "medium": 1,
        "heavy": 0,
    }
    assert snapshot["registered_success_count"] == 2


def test_select_registered_streams_includes_historical_entries(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "successful-streams.json"
    rows = []
    for bucket, name, task_id in (
        ("light", "light-historical", "light-task"),
        ("heavy", "heavy-current", "heavy-task"),
    ):
        stream_dir = tmp_path / name
        _write_stream(stream_dir, [task_id])
        rows.append({
            "name": name,
            "bucket": bucket,
            "state_path": str(tmp_path / f"{name}.state.json"),
            "stream_dir": str(stream_dir),
            "task_ids": [task_id],
        })
    registry.write_text(json.dumps({
        "schema_version": 1,
        "overlap_metric": OVERLAP_METRIC,
        "max_overlap_ratio_exclusive": 0.60,
        "max_successful_streams": None,
        "streams": rows,
    }))

    out_dir = tmp_path / "selected"
    scan._select_registered_streams(
        registry,
        out_dir,
        {"light": 1, "heavy": 1},
    )

    selection = json.loads((out_dir / "selection.json").read_text())
    assert selection["selected_counts"] == {"heavy": 1, "light": 1}
    assert {row["name"] for row in selection["selected"]} == {
        "light-historical",
        "heavy-current",
    }
    assert (out_dir / "selection.csv").is_file()


def test_monitor_snapshot_recovers_inactive_codex_refine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    record = {
        "bucket": "heavy",
        "name": "candidate",
        "path": str(tmp_path / "candidate"),
        "tasks": ["a"],
    }
    state_path = run_root / "results/heavy/candidate/state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"status": "codex-refine"}))
    monkeypatch.setattr(scan, "_active_run_dirs", set)

    snapshot = scan._monitor_snapshot(
        [record],
        run_root,
        run_root / "successful-streams.json",
        {"heavy": 4},
    )

    assert snapshot["resumable"] == ["heavy/candidate"]


def test_monitor_snapshot_does_not_immediately_restart_infrastructure_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    record = {
        "bucket": "heavy",
        "name": "candidate",
        "path": str(tmp_path / "candidate"),
        "tasks": ["a"],
    }
    state_path = run_root / "results/heavy/candidate/state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"status": "paused-infrastructure"}))
    monkeypatch.setattr(scan, "_active_run_dirs", set)

    snapshot = scan._monitor_snapshot(
        [record],
        run_root,
        run_root / "successful-streams.json",
        {"heavy": 5},
    )

    assert snapshot["status_counts"]["heavy"] == {"paused-infrastructure": 1}
    assert snapshot["resumable"] == []


def test_monitor_snapshot_refills_candidate_without_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    record = {
        "bucket": "light",
        "name": "candidate",
        "path": str(tmp_path / "candidate"),
        "tasks": ["a"],
    }
    monkeypatch.setattr(scan, "_active_run_dirs", set)

    snapshot = scan._monitor_snapshot(
        [record],
        run_root,
        run_root / "successful-streams.json",
        {"light": 10},
    )

    assert snapshot["status_counts"]["light"] == {"missing-state": 1}
    assert snapshot["resumable"] == ["light/candidate"]


def test_monitor_snapshot_replaces_terminal_build_overlap_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    failed = {
        "bucket": "medium",
        "name": "failed-overlap",
        "path": str(tmp_path / "failed-overlap"),
        "tasks": ["a"],
    }
    replacement = {
        "bucket": "medium",
        "name": "replacement",
        "path": str(tmp_path / "replacement"),
        "tasks": ["b"],
    }
    state_path = run_root / "results/medium/failed-overlap/state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "status": "failed-overlap-build",
        "codex_turn": 0,
    }))
    monkeypatch.setattr(scan, "_active_run_dirs", set)

    snapshot = scan._monitor_snapshot(
        [failed, replacement],
        run_root,
        run_root / "build-valid-streams.json",
        {"medium": 20},
        stage="build",
    )

    assert snapshot["status_counts"]["medium"] == {
        "failed-overlap-build": 1,
        "missing-state": 1,
    }
    assert snapshot["resumable"] == ["medium/replacement"]


def test_monitor_snapshot_recovers_exhausted_below_new_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    record = {
        "bucket": "light",
        "name": "candidate",
        "path": str(tmp_path / "candidate"),
        "tasks": ["a"],
    }
    state_path = run_root / "results/light/candidate/state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "status": "failed-max-rounds",
        "codex_turn": 32,
    }))
    monkeypatch.setattr(scan, "_active_run_dirs", set)

    snapshot = scan._monitor_snapshot(
        [record],
        run_root,
        run_root / "successful-streams.json",
        {"light": 10},
        resume_exhausted=True,
        max_rounds=64,
    )

    assert snapshot["resumable"] == ["light/candidate"]


def test_monitor_snapshot_does_not_recover_exhausted_at_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    record = {
        "bucket": "light",
        "name": "candidate",
        "path": str(tmp_path / "candidate"),
        "tasks": ["a"],
    }
    state_path = run_root / "results/light/candidate/state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "status": "failed-max-rounds",
        "codex_turn": 64,
    }))
    monkeypatch.setattr(scan, "_active_run_dirs", set)

    snapshot = scan._monitor_snapshot(
        [record],
        run_root,
        run_root / "successful-streams.json",
        {"light": 10},
        resume_exhausted=True,
        max_rounds=64,
    )

    assert snapshot["resumable"] == []


def test_target_override_is_scoped_to_manifest_buckets() -> None:
    records = [{"bucket": "heavy", "name": "candidate"}]
    args = type("Args", (), {
        "target_light": 10,
        "target_medium": 5,
        "target_heavy": 4,
    })()

    assert scan._targets_for_records(records, args) == {"heavy": 4}


def test_select_named_records_preserves_manifest_order() -> None:
    records = [
        {"bucket": "light", "name": "one"},
        {"bucket": "medium", "name": "two"},
        {"bucket": "heavy", "name": "three"},
    ]

    selected = scan._select_named_records(
        records,
        ["heavy/three", "light/one"],
    )

    assert selected == [records[0], records[2]]


def test_select_bucket_records_preserves_manifest_order() -> None:
    records = [
        {"bucket": "light", "name": "one"},
        {"bucket": "medium", "name": "two"},
        {"bucket": "heavy", "name": "three"},
        {"bucket": "medium", "name": "four"},
    ]

    selected = scan._select_bucket_records(records, ["medium"])

    assert selected == [records[1], records[3]]


def test_resume_batch_respects_available_worker_capacity() -> None:
    snapshot = {
        "active_workers": 38,
        "resumable": ["light/one", "medium/two", "heavy/three"],
    }

    assert scan._resume_batch(snapshot, 40) == ["light/one", "medium/two"]
    assert scan._resume_batch(snapshot, 38) == []
    assert scan._resume_batch(snapshot, None) == snapshot["resumable"]
