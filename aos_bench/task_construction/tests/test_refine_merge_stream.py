from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from aos_bench.task_construction.assemble.refine_merge_stream import orchestrator

from aos_bench.task_construction.assemble.refine_merge_stream.orchestrator import (
    CodexNoOpError,
    LoopConfig,
    LoopState,
    StreamRefineLoop,
    VerifierResult,
    _accepted_stream_usage,
    _allowed_task_workspace_path,
    _audit_codex_commands,
    _dockerfile_repair_chunks,
    _editable_workspace_roots,
    _error_signals,
    _infrastructure_failure_class,
    _is_transient_build_failure,
    _load_success_registry,
    _prune_sensitive_workspace,
    _redact_log_text,
    _task_overlap,
    _task_oracle_reputation,
    _tree_hashes,
    _validate_dockerfile_security,
)
from aos_bench.task_construction.scripts.generate_v1_0 import load_task_blacklist


def _entry(task_id: str, tier: str) -> SimpleNamespace:
    return SimpleNamespace(task_id=task_id, tier=tier, pool="TB2", task_dir=Path("/unused"))


def _bare_loop(tmp_path: Path) -> StreamRefineLoop:
    loop = object.__new__(StreamRefineLoop)
    loop.config = LoopConfig(stream_dir=tmp_path / "stream", run_dir=tmp_path / "run")
    loop.stream_dir = loop.config.stream_dir
    loop.run_dir = loop.config.run_dir
    loop.workspace = loop.run_dir / "workspace"
    loop.logs_dir = loop.run_dir / "logs"
    loop.jobs_dir = loop.run_dir / "jobs"
    loop.state_path = loop.run_dir / "state.json"
    loop.success_registry_path = tmp_path / "successful-streams.json"
    loop.build_registry_path = tmp_path / "build-valid-streams.json"
    loop.pool = [_entry("a", "L2"), _entry("b", "L2"), _entry("c", "L3")]
    loop.all_pool = loop.pool
    loop.pool_by_id = {entry.task_id: entry for entry in loop.pool}
    loop.all_pool_by_id = loop.pool_by_id
    loop.explicit_blacklist = set()
    loop.state = LoopState(
        stream_dir=str(loop.stream_dir),
        original_task_count=2,
        original_tier_counts={"L2": 1, "L3": 1},
    )
    loop.logs_dir.mkdir(parents=True)
    loop.jobs_dir.mkdir(parents=True)
    return loop


def _write_stream(path: Path, task_ids: list[str]) -> None:
    path.mkdir(parents=True)
    arrivals = "\n".join(
        f'[[arrivals]]\narrive_wall_time = {i}\ntask_id = "{task_id}"\n'
        for i, task_id in enumerate(task_ids)
    )
    (path / "stream.toml").write_text(
        'version = "1.0"\n[metadata]\nname = "test"\n'
        '[environment]\ncpus = 4\nmemory_mb = 8192\nallow_internet = true\n'
        '[session]\nmax_steps = 20\n' + arrivals
    )
    (path / "environment").mkdir()
    (path / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")


def _write_source(path: Path) -> None:
    (path / "environment").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "solution").mkdir()
    (path / "instruction.md").write_text("Do the task.\n")
    (path / "environment" / "Dockerfile").write_text(
        "FROM ubuntu:24.04\nWORKDIR /app\n"
    )
    (path / "tests" / "test.sh").write_text(
        "echo 1 > /logs/verifier/reward.txt\n"
    )
    (path / "solution" / "solve.sh").write_text("#!/bin/bash\ntrue\n")


def test_error_signals_are_bounded_and_redact_secrets() -> None:
    text = "\n".join([
        "ordinary output",
        "ModuleNotFoundError: missing thing",
        "Authorization=secret-token failed",
    ])
    signals = _error_signals(text)
    assert signals[0] == "ModuleNotFoundError: missing thing"
    assert "secret-token" not in signals[1]


def test_transient_build_failure_classifier_is_conservative() -> None:
    assert _is_transient_build_failure(
        "Connection broken: InvalidChunkLength(got length b'', 0 bytes read)"
    )
    assert _is_transient_build_failure(
        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE"
    )
    assert not _is_transient_build_failure(
        "No matching distribution found for scipy==1.10.1"
    )


def test_infrastructure_failure_classifier_handles_persisted_verdicts() -> None:
    result = VerifierResult(
        "oracle",
        False,
        "Oracle produced no reward.json",
        {
            "error_signals": [
                "RuntimeError: dispatcher demo did not pass /healthz within 60s"
            ],
        },
    )

    assert _infrastructure_failure_class(result) == "oracle-infrastructure"


@pytest.mark.parametrize(
    ("codex_job_queue", "expected_label"),
    [
        (None, "harbor.job-queue=stream-queue"),
        ("", None),
        ("codex-queue", "harbor.job-queue=codex-queue"),
    ],
)
def test_codex_container_queue_can_use_default_proxy_placement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    codex_job_queue: str | None,
    expected_label: str | None,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.codex_container = "refine-test-container"
    loop.config.codex_image = "refine-test-image"
    loop.config.job_queue = "stream-queue"
    loop.config.codex_job_queue = codex_job_queue
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="container-id\n", stderr="")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    monkeypatch.setattr(orchestrator.fcntl, "flock", lambda *_: None)
    loop._create_codex_container()

    command = commands[0]
    labels = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--label"
    ]
    queue_labels = [
        label for label in labels
        if label.startswith("harbor.job-queue=")
    ]
    assert queue_labels == ([] if expected_label is None else [expected_label])
    assert ("aos.refine.codex-placement=default" in labels) == (
        codex_job_queue == ""
    )


def test_log_redaction_handles_header_and_assignment_credentials() -> None:
    text = "Authorization: Bearer secret-one\nAPI_KEY=secret-two\nordinary\n"
    redacted = _redact_log_text(text)
    assert "secret-one" not in redacted
    assert "secret-two" not in redacted
    assert redacted.endswith("ordinary\n")


def test_replacement_preserves_count_tiers_and_uniqueness(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])
    assert loop._validate_plan({"task_ids": ["b", "c"]}) == ["b", "c"]
    with pytest.raises(ValueError, match="duplicate"):
        loop._validate_plan({"task_ids": ["a", "a"]})
    with pytest.raises(ValueError, match="tier mix"):
        loop._validate_plan({"task_ids": ["a", "b"]})
    with pytest.raises(ValueError, match="outside candidate pool"):
        loop._validate_plan({"task_ids": ["a", "unknown"]})


def test_replacement_rejects_globally_blocklisted_new_task(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])
    loop._replacement_reputation = lambda: {
        "b": {"replacement_status": "avoid"},
    }
    with pytest.raises(ValueError, match="blocklisted"):
        loop._validate_plan({"task_ids": ["b", "c"]})


def test_explicit_blacklist_is_parsed_and_enforced(tmp_path: Path) -> None:
    blacklist = tmp_path / "blacklist.txt"
    blacklist.write_text("# blocked\nb  # repeated failure\n\n")
    assert load_task_blacklist(blacklist) == {"b"}

    loop = _bare_loop(tmp_path)
    loop.explicit_blacklist = {"b"}
    loop.pool = [entry for entry in loop.pool if entry.task_id != "b"]
    loop.pool_by_id = {entry.task_id: entry for entry in loop.pool}
    _write_stream(loop.stream_dir, ["a", "c"])

    with pytest.raises(ValueError, match="explicitly blacklisted"):
        loop._validate_plan({"task_ids": ["b", "c"]})


def test_existing_explicit_blacklist_blocks_build_and_requires_replacement(
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.explicit_blacklist = {"a"}
    loop.pool = [entry for entry in loop.pool if entry.task_id != "a"]
    loop.pool_by_id = {entry.task_id: entry for entry in loop.pool}
    _write_stream(loop.stream_dir, ["a", "c"])

    result = loop.verify_build()
    payload = loop._plan_payload()

    assert result.layer == "task-blacklist"
    assert result.details["failed_tasks"] == ["a"]
    assert payload["mandatory_replacements"] == ["a"]
    assert "a" in payload["replacement_blocklist"]
    assert "a" not in {row["task_id"] for row in payload["candidate_pool"]}


def test_task_reputation_blocklists_repeated_zero_reward(tmp_path: Path) -> None:
    state = tmp_path / "results" / "medium" / "candidate" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "rounds": [
            {"oracle": {"details": {"rewards": {"bad": 0.0, "good": 1.0}}}},
            {"oracle": {"details": {"rewards": {"bad": 0.0, "good": 1.0}}}},
            {"oracle": {"details": {"rewards": {"bad": 0.0, "good": 0.0}}}},
        ]
    }))

    reputation = _task_oracle_reputation(tmp_path / "results")

    assert reputation["bad"]["replacement_status"] == "avoid"
    assert reputation["good"]["replacement_status"] == "eligible"
    assert reputation["good"]["exact_pass_rate"] == pytest.approx(2 / 3, abs=0.0001)


def test_accepted_stream_usage_reads_only_fully_passed_states(tmp_path: Path) -> None:
    accepted = tmp_path / "streams" / "accepted"
    rejected = tmp_path / "streams" / "rejected"
    _write_stream(accepted, ["a", "c"])
    _write_stream(rejected, ["b", "c"])
    results = tmp_path / "results"
    accepted_state = results / "heavy" / "accepted" / "state.json"
    rejected_state = results / "heavy" / "rejected" / "state.json"
    accepted_state.parent.mkdir(parents=True)
    rejected_state.parent.mkdir(parents=True)
    accepted_state.write_text(json.dumps({
        "status": "passed", "stream_dir": str(accepted), "rounds": [],
    }))
    rejected_state.write_text(json.dumps({
        "status": "failed", "stream_dir": str(rejected), "rounds": [],
    }))

    diversity = _accepted_stream_usage(results)

    assert diversity["accepted_stream_count"] == 1
    assert diversity["accepted_task_usage"] == {"a": 1, "c": 1}


def test_configured_empty_registry_does_not_fall_back_to_historical_states(
    tmp_path: Path,
) -> None:
    accepted = tmp_path / "streams" / "accepted"
    _write_stream(accepted, ["a", "c"])
    state = tmp_path / "results" / "heavy" / "accepted" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "status": "passed",
        "stream_dir": str(accepted),
        "rounds": [],
    }))

    diversity = _accepted_stream_usage(
        tmp_path / "results",
        tmp_path / "successful-streams.json",
    )

    assert diversity == {
        "accepted_stream_count": 0,
        "accepted_task_usage": {},
        "accepted_streams": [],
    }


def test_plan_exposes_accepted_usage_for_diverse_replacement(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])
    loop._accepted_stream_usage = lambda: {
        "accepted_stream_count": 2,
        "accepted_task_usage": {"a": 2, "b": 0, "c": 2},
        "accepted_streams": [{"name": "old", "task_ids": ["a", "c"]}],
    }
    loop._replacement_reputation = lambda: {}

    payload = loop._plan_payload()
    candidates = {row["task_id"]: row for row in payload["candidate_pool"]}

    assert candidates["a"]["accepted_usage"] == 2
    assert candidates["b"]["accepted_usage"] == 0
    assert payload["diversity"]["current_overlap_by_accepted_stream"] == {"old": 2}


def test_codex_command_audit_rejects_solution_reads(tmp_path: Path) -> None:
    safe = json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "sed -n '1,80p' environment/Dockerfile"},
    })
    unsafe = json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "cat /cloud/run/solution/solve.sh"},
    })
    assert _audit_codex_commands(safe, tmp_path) == []
    assert _audit_codex_commands(unsafe, tmp_path)


def test_task_overlay_allowlist_is_scoped_to_current_tasks() -> None:
    task_ids = {"a", "c"}
    assert _allowed_task_workspace_path("tasks/a/instruction.md", task_ids)
    assert _allowed_task_workspace_path("tasks/a/tests/test.sh", task_ids)
    assert _allowed_task_workspace_path("tasks/a/verifier/check.py", task_ids)
    assert _allowed_task_workspace_path("tasks/c/solution/solve.sh", task_ids)
    assert not _allowed_task_workspace_path(
        "tasks/c/solution/solve.sh", task_ids, stage="build"
    )
    assert not _allowed_task_workspace_path("tasks/b/tests/test.sh", task_ids)
    assert not _allowed_task_workspace_path("tasks/a/environment/Dockerfile", task_ids)
    assert not _allowed_task_workspace_path("solution/tasks/a/solve.sh", task_ids)


def test_container_collection_roots_include_current_task_overlays() -> None:
    roots = _editable_workspace_roots(["c", "a"])
    assert roots[:2] == ["environment/Dockerfile", "stream_plan.json"]
    assert roots[2:] == [
        "tasks/a/instruction.md",
        "tasks/a/tests",
        "tasks/a/verifier",
        "tasks/a/solution",
        "tasks/c/instruction.md",
        "tasks/c/tests",
        "tasks/c/verifier",
        "tasks/c/solution",
    ]


def test_container_collection_skips_task_root_absent_from_baseline(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "environment").mkdir(parents=True)
    (workspace / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    (workspace / "stream_plan.json").write_text("{}\n")
    (workspace / "tasks" / "a" / "tests").mkdir(parents=True)
    (workspace / "tasks" / "a" / "instruction.md").write_text("Do it.\n")

    roots = _editable_workspace_roots(["a"], workspace)

    assert "tasks/a/instruction.md" in roots
    assert "tasks/a/tests" in roots
    assert "tasks/a/solution" not in roots


def test_build_container_collection_exposes_contract_but_not_solution(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "environment").mkdir(parents=True)
    (workspace / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    (workspace / "stream_plan.json").write_text("{}\n")
    for root in ("tests", "verifier", "solution"):
        (workspace / "tasks" / "a" / root).mkdir(parents=True)
        (workspace / "tasks" / "a" / root / "file.txt").write_text(root)
    (workspace / "tasks" / "a" / "instruction.md").write_text("Do it.\n")

    roots = _editable_workspace_roots(["a"], workspace, stage="build")

    assert "tasks/a/instruction.md" in roots
    assert "tasks/a/tests" in roots
    assert "tasks/a/verifier" in roots
    assert "tasks/a/solution" not in roots


def test_sensitive_source_material_is_pruned(tmp_path: Path) -> None:
    env = tmp_path / "environment"
    (env / "_sources" / "task" / "tests").mkdir(parents=True)
    (env / "_sources" / "task" / "tests" / "hidden.py").write_text("secret")
    (env / "_sources" / "task" / "data").mkdir()
    (env / "_sources" / "task" / "data" / "input.csv").write_text("public")
    (env / "_sources" / "task" / "solve.sh").write_text("oracle")
    _prune_sensitive_workspace(env)
    assert not (env / "_sources" / "task" / "tests").exists()
    assert not (env / "_sources" / "task" / "solve.sh").exists()
    assert (env / "_sources" / "task" / "data" / "input.csv").is_file()


def test_dockerfile_cannot_reintroduce_tests() -> None:
    _validate_dockerfile_security("FROM ubuntu:24.04\nCOPY data/ /workspace/data/\n")
    with pytest.raises(ValueError, match="tests/solution"):
        _validate_dockerfile_security("FROM ubuntu:24.04\nCOPY tests/interp.py /app/\n")


def test_dockerfile_allows_path_rewrite_without_new_sensitive_copy() -> None:
    baseline = "FROM ubuntu:24.04\nCOPY tests/old.py /opt/check.py\n"
    rewritten = "FROM ubuntu:24.04\nCOPY tests/new.py /opt/check.py\n"
    _validate_dockerfile_security(rewritten, baseline)
    with pytest.raises(ValueError, match="tests/solution"):
        _validate_dockerfile_security(
            rewritten + "COPY solution/solve.sh /opt/solve.sh\n", baseline
        )


def test_apply_rejects_codex_no_op(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])
    loop._prepare_workspace()

    with pytest.raises(CodexNoOpError, match="without changing"):
        loop.apply_codex_changes()


def test_stage_edit_contracts_are_enforced(tmp_path: Path) -> None:
    build_loop = _bare_loop(tmp_path / "build")
    build_loop.config.stage = "build"
    _write_stream(build_loop.stream_dir, ["a", "c"])
    for task_id in ("a", "c"):
        _write_source(build_loop.stream_dir / "tasks" / task_id)
    build_loop._prepare_workspace()
    (build_loop.workspace / "tasks" / "a" / "instruction.md").write_text(
        "Do the task at /task/a instead.\n"
    )
    assert "tasks/a/instruction.md" in build_loop._validate_workspace_edits()
    (build_loop.workspace / "tasks" / "a" / "solution").mkdir(parents=True)
    (build_loop.workspace / "tasks" / "a" / "solution" / "solve.sh").write_text(
        "#!/bin/bash\nfalse\n"
    )
    with pytest.raises(ValueError, match="forbidden in build stage"):
        build_loop._validate_workspace_edits()

    solution_loop = _bare_loop(tmp_path / "solution")
    solution_loop.config.stage = "solution"
    _write_stream(solution_loop.stream_dir, ["a", "c"])
    for task_id in ("a", "c"):
        _write_source(solution_loop.stream_dir / "tasks" / task_id)
    solution_loop._prepare_workspace()
    (solution_loop.workspace / "environment" / "Dockerfile").write_text(
        "FROM debian:bookworm\n"
    )
    with pytest.raises(ValueError, match="forbidden in solution stage"):
        solution_loop._validate_workspace_edits()


def test_build_overlay_applies_contract_edits_without_touching_solution(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.stage = "build"
    _write_stream(loop.stream_dir, ["a", "c"])
    for task_id in ("a", "c"):
        _write_source(loop.stream_dir / "tasks" / task_id)
    verifier = loop.stream_dir / "tasks" / "a" / "verifier"
    verifier.mkdir()
    (verifier / "check.py").write_text("assert True\n")
    original_solution = (
        loop.stream_dir / "tasks" / "a" / "solution" / "solve.sh"
    ).read_bytes()
    loop._prepare_workspace()

    (loop.workspace / "tasks" / "a" / "instruction.md").write_text(
        "Do the task using the aligned /task/a path.\n"
    )
    (loop.workspace / "tasks" / "a" / "tests" / "test.sh").write_text(
        "cd /task/a\necho 1 > /logs/verifier/reward.txt\n"
    )
    (loop.workspace / "tasks" / "a" / "verifier" / "check.py").write_text(
        "import os\nassert os.path.isdir('/task/a')\n"
    )
    changed = loop._validate_workspace_edits()
    loop._validate_task_overlay_equivalence(changed)
    loop._apply_task_overlay(changed)

    assert "/task/a" in (
        loop.stream_dir / "tasks" / "a" / "instruction.md"
    ).read_text()
    assert "/task/a" in (
        loop.stream_dir / "tasks" / "a" / "tests" / "test.sh"
    ).read_text()
    assert "/task/a" in (
        loop.stream_dir / "tasks" / "a" / "verifier" / "check.py"
    ).read_text()
    assert (
        loop.stream_dir / "tasks" / "a" / "solution" / "solve.sh"
    ).read_bytes() == original_solution


def test_build_stage_stops_before_oracle_and_registers_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.stage = "build"
    _write_stream(loop.stream_dir, ["a", "c"])
    image = "registry.example/aos@sha256:" + "a" * 64
    monkeypatch.setattr(
        loop,
        "verify_build",
        lambda: VerifierResult("build", True, "build passed", {"image": image}),
    )
    monkeypatch.setattr(
        loop,
        "_register_build_valid",
        lambda _: VerifierResult(
            "overlap",
            True,
            "overlap passed",
            {"registered_entry": {"image": image}},
        ),
    )
    monkeypatch.setattr(
        loop,
        "verify_oracle",
        lambda: pytest.fail("build stage must not invoke Oracle"),
    )

    assert loop.run() == 0
    assert loop.state.status == "build-passed"
    assert loop.state.rounds[-1]["overlap"]["passed"] is True


def _passed_build(image_suffix: str = "a") -> VerifierResult:
    image = "registry.example/aos@sha256:" + image_suffix * 64
    return VerifierResult(
        "build",
        True,
        "build passed",
        {
            "image": image,
            "image_tag": "registry.example/aos:test",
            "image_digest": "sha256:" + image_suffix * 64,
        },
    )


def _write_success_registry(path: Path, task_ids: list[str]) -> None:
    path.write_text(json.dumps({
        "schema_version": 1,
        "overlap_metric": "intersection_over_min_task_count",
        "max_overlap_ratio_exclusive": 0.60,
        "max_successful_streams": None,
        "streams": [{
            "name": "historical",
            "state_path": "/historical/state.json",
            "task_ids": task_ids,
        }],
    }))


def test_build_registration_rejects_historical_overlap_at_equality(
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.max_overlap_ratio = 0.60
    historical = [f"task-{index:02d}" for index in range(20)]
    candidate = historical[:12] + [f"new-{index:02d}" for index in range(8)]
    _write_success_registry(loop.success_registry_path, historical)
    _write_stream(loop.stream_dir, candidate)

    result = loop._register_build_valid(_passed_build())

    assert not result.passed
    assert result.details["conflicts"][0]["ratio"] == pytest.approx(0.60)
    assert result.details["conflicts"][0]["source"] == "historical-valid"
    assert not loop.build_registry_path.exists()
    assert (loop.run_dir / "build-evidence.json").is_file()
    assert (loop.logs_dir / "attempt-00" / "overlap.json").is_file()


def test_build_registration_accepts_overlap_below_threshold(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.max_overlap_ratio = 0.60
    historical = [f"task-{index:02d}" for index in range(20)]
    candidate = historical[:11] + [f"new-{index:02d}" for index in range(9)]
    _write_success_registry(loop.success_registry_path, historical)
    _write_stream(loop.stream_dir, candidate)

    result = loop._register_build_valid(_passed_build())

    assert result.passed
    payload = json.loads(loop.build_registry_path.read_text())
    assert len(payload["streams"]) == 1
    assert payload["max_overlap_ratio_exclusive"] == pytest.approx(0.60)


def test_build_registration_serializes_current_sweep_overlap(tmp_path: Path) -> None:
    left = _bare_loop(tmp_path / "left")
    right = _bare_loop(tmp_path / "right")
    registry = tmp_path / "build-valid-streams.json"
    historical = tmp_path / "successful-streams.json"
    for loop in (left, right):
        loop.build_registry_path = registry
        loop.success_registry_path = historical
        loop.config.max_overlap_ratio = 0.60
    shared = [f"shared-{index:02d}" for index in range(12)]
    _write_stream(left.stream_dir, shared + [f"left-{index:02d}" for index in range(8)])
    _write_stream(right.stream_dir, shared + [f"right-{index:02d}" for index in range(8)])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda item: item[0]._register_build_valid(_passed_build(item[1])),
            ((left, "a"), (right, "b")),
        ))

    assert sum(result.passed for result in results) == 1
    assert len(json.loads(registry.read_text())["streams"]) == 1


def test_build_overlap_failure_is_terminal_without_codex(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.stage = "build"
    _write_stream(loop.stream_dir, ["a", "c"])
    monkeypatch.setattr(loop, "verify_build", lambda: _passed_build())
    monkeypatch.setattr(
        loop,
        "_register_build_valid",
        lambda _: VerifierResult("overlap", False, "overlap equality failed"),
    )
    monkeypatch.setattr(
        loop,
        "run_codex",
        lambda _: pytest.fail("build overlap failure must release the worker slot"),
    )

    assert loop.run() == 1
    assert loop.state.status == "failed-overlap-build"
    assert loop.state.rounds[-1]["overlap"]["passed"] is False


def test_solution_stage_skips_build_and_uses_fixed_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.stage = "solution"
    _write_stream(loop.stream_dir, ["a", "c"])
    image = "registry.example/aos@sha256:" + "b" * 64
    loop._set_docker_image(image)
    monkeypatch.setattr(
        loop,
        "verify_build",
        lambda: pytest.fail("solution stage must not rebuild the image"),
    )
    monkeypatch.setattr(
        loop,
        "verify_oracle",
        lambda: VerifierResult("oracle", True, "oracle passed"),
    )
    monkeypatch.setattr(
        loop,
        "verify_overlap",
        lambda _: VerifierResult("overlap", True, "overlap passed"),
    )

    assert loop.run() == 0
    assert loop.state.rounds[-1]["build"]["details"]["build_skipped"] is True


def test_solution_runtime_tag_must_resolve_to_registered_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])
    digest = "sha256:" + "b" * 64
    identity = f"registry.example/aos@{digest}"
    runtime_tag = "registry.example/aos:a7-content"
    loop.build_registry_path.write_text(json.dumps({
        "streams": [{
            "name": "stream",
            "image": identity,
            "image_tag": runtime_tag,
            "image_digest": digest,
        }],
    }))
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"digest": digest}),
            stderr="",
        ),
    )

    assert loop._solution_image_references(identity) == (identity, runtime_tag)
    assert loop._solution_image_references(runtime_tag) == (identity, runtime_tag)


def test_solution_runtime_tag_rejects_digest_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])
    digest = "sha256:" + "b" * 64
    identity = f"registry.example/aos@{digest}"
    runtime_tag = "registry.example/aos:a7-content"
    loop.build_registry_path.write_text(json.dumps({
        "streams": [{
            "name": "stream",
            "image": identity,
            "image_tag": runtime_tag,
            "image_digest": digest,
        }],
    }))
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"digest": "sha256:" + "c" * 64}),
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="tag digest changed"):
        loop._solution_image_references(identity)


def test_feedback_recommends_repeated_task_replacement(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    result = VerifierResult("oracle", False, "failed", {"failed_tasks": ["a"]})
    first = loop._feedback_prompt(result)
    second = loop._feedback_prompt(result)
    assert '"replacement_recommended": []' in first
    assert '"replacement_recommended": [\n    "a"' in second


def test_feedback_does_not_poison_task_reputation_for_infrastructure(
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    result = VerifierResult(
        "oracle",
        False,
        "Oracle produced no reward.json",
        {
            "failed_tasks": ["a", "c"],
            "infrastructure_failure": True,
            "failure_class": "dispatcher-health",
        },
    )

    prompt = loop._feedback_prompt(result)

    assert loop.state.failure_counts == {}
    assert '"infrastructure_failure": "dispatcher-health"' in prompt
    assert '"replacement_recommended": []' in prompt


def test_feedback_encodes_observed_oracle_failure_playbook(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    result = VerifierResult(
        "oracle",
        False,
        "failed",
        {
            "failed_tasks": ["a"],
            "task_error_signals": {"a": ["FileNotFoundError: output missing"]},
        },
    )

    prompt = loop._feedback_prompt(result)

    assert "more common than true\n   cross-task version conflicts" in prompt
    assert "A missing expected output is usually a downstream symptom" in prompt
    assert "from __future__ imports" in prompt
    assert "Fully passed requires an exact reward of 1.0" in prompt
    assert "do not rewrite or replace a task based only on timeout" in prompt
    assert "search all four\n  surfaces" in prompt
    assert "Full redacted diagnostics are available read-only under logs/" in prompt
    assert "HTTP 401/402/429" in prompt
    assert "same repair class has already failed twice" in prompt
    assert "replacement_status=avoid" in prompt
    assert "reassembly-repair-replay.json" in prompt
    assert "arm64 package indexes must come from ubuntu-ports" in prompt
    assert "binary-arm64 from a regular Ubuntu mirror" in prompt
    assert "never Jammy arm64 with Noble amd64" in prompt
    assert "constrain every active native legacy and deb822\n  Ubuntu source to amd64" in prompt
    assert "relocate, move, or symlink vllm" in prompt
    assert "Do not\n  blindly run every workspace's build script" in prompt
    assert "not permission\n  to fabricate it" in prompt
    assert "Oracle run is broadly partial" in prompt
    assert "Replace exactly one failed avoid-rated task" in prompt


def test_feedback_hot_loads_verified_repair_playbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playbook = tmp_path / "refine_prompt_playbook.md"
    monkeypatch.setattr(orchestrator, "REFINE_PROMPT_PLAYBOOK", playbook)
    loop = _bare_loop(tmp_path)
    result = VerifierResult("build", False, "failed", {})

    playbook.write_text("first verified repair lesson")
    assert "first verified repair lesson" in loop._feedback_prompt(result)

    playbook.write_text("second verified repair lesson")
    prompt = loop._feedback_prompt(result)
    assert "second verified repair lesson" in prompt
    assert "first verified repair lesson" not in prompt


def test_build_feedback_allows_path_alignment_and_includes_playbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playbook = tmp_path / "refine_prompt_playbook.md"
    playbook.write_text("verified domestic mirror lesson")
    monkeypatch.setattr(orchestrator, "REFINE_PROMPT_PLAYBOOK", playbook)
    loop = _bare_loop(tmp_path)
    loop.config.stage = "build"

    prompt = loop._feedback_prompt(VerifierResult("build", False, "failed", {}))

    assert "instruction.md, tests/**, and verifier/**" in prompt
    assert "update every logically related reference in the SAME turn" in prompt
    assert "Solution files are hidden and" in prompt
    assert "Never delete,\nskip, relax, or bypass" in prompt
    assert "verified domestic mirror lesson" in prompt


def test_overlap_metric_uses_exclusive_intersection_over_smaller_stream() -> None:
    left = [f"task-{index:02d}" for index in range(20)]
    eleven_shared = left[:11] + [f"other-{index:02d}" for index in range(9)]
    twelve_shared = left[:12] + [f"extra-{index:02d}" for index in range(8)]

    assert _task_overlap(left, eleven_shared)["ratio"] == pytest.approx(0.55)
    assert _task_overlap(left, twelve_shared)["ratio"] == pytest.approx(0.60)


def _passed_oracle() -> VerifierResult:
    return VerifierResult(
        "oracle",
        True,
        "Oracle fully passed",
        {"reward_path": "/jobs/reward.json"},
    )


def test_overlap_registry_accepts_below_threshold_and_rejects_equality(
    tmp_path: Path,
) -> None:
    first = _bare_loop(tmp_path / "first")
    below = _bare_loop(tmp_path / "below")
    equal = _bare_loop(tmp_path / "equal")
    registry = tmp_path / "successful-streams.json"
    for loop in (first, below, equal):
        loop.success_registry_path = registry
        loop.config.max_overlap_ratio = 0.60

    base = [f"task-{index:02d}" for index in range(20)]
    _write_stream(first.stream_dir, base)
    _write_stream(
        below.stream_dir,
        base[:11] + [f"below-{index:02d}" for index in range(9)],
    )
    _write_stream(
        equal.stream_dir,
        base[:12] + [f"equal-{index:02d}" for index in range(8)],
    )

    assert first.verify_overlap(_passed_oracle()).passed
    assert below.verify_overlap(_passed_oracle()).passed
    result = equal.verify_overlap(_passed_oracle())

    assert not result.passed
    assert result.details["conflicts"][0]["ratio"] == pytest.approx(0.60)
    assert len(_load_success_registry(registry)["streams"]) == 2


def test_overlap_registry_registration_is_idempotent_by_state_path(
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, [f"task-{index:02d}" for index in range(20)])

    assert loop.verify_overlap(_passed_oracle()).passed
    assert loop.verify_overlap(_passed_oracle()).passed

    registry = _load_success_registry(loop.success_registry_path)
    assert len(registry["streams"]) == 1
    assert registry["streams"][0]["state_path"] == str(loop.state_path.resolve())


def test_success_registry_rejects_internal_overlap_violation(tmp_path: Path) -> None:
    registry = tmp_path / "successful-streams.json"
    base = [f"task-{index:02d}" for index in range(20)]
    registry.write_text(json.dumps({
        "schema_version": 1,
        "overlap_metric": "intersection_over_min_task_count",
        "max_overlap_ratio_exclusive": 0.60,
        "streams": [
            {"name": "first", "task_ids": base},
            {"name": "second", "task_ids": base[:12] + [f"new-{i}" for i in range(8)]},
        ],
    }))

    with pytest.raises(ValueError, match="violates its exclusive overlap"):
        _load_success_registry(registry)


def test_overlap_feedback_requires_plan_only_replacement(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    result = VerifierResult(
        "overlap",
        False,
        "overlap failed",
        {"conflicts": [{"name": "accepted", "ratio": 0.60}]},
    )

    prompt = loop._feedback_prompt(result)

    assert "Edit only stream_plan.json" in prompt
    assert "Equality is a failure" in prompt
    assert "Do not edit the Dockerfile, instruction" in prompt


def test_overlap_registry_serializes_conflicting_candidates(tmp_path: Path) -> None:
    left = _bare_loop(tmp_path / "left")
    right = _bare_loop(tmp_path / "right")
    registry = tmp_path / "successful-streams.json"
    for loop in (left, right):
        loop.success_registry_path = registry
        loop.config.max_overlap_ratio = 0.60
    base = [f"shared-{index:02d}" for index in range(12)]
    _write_stream(left.stream_dir, base + [f"left-{index:02d}" for index in range(8)])
    _write_stream(right.stream_dir, base + [f"right-{index:02d}" for index in range(8)])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda loop: loop.verify_overlap(_passed_oracle()),
            (left, right),
        ))

    assert sum(result.passed for result in results) == 1
    assert len(_load_success_registry(registry)["streams"]) == 1


def test_success_registry_capacity_is_enforced_under_lock(tmp_path: Path) -> None:
    registry = tmp_path / "successful-streams.json"
    loops = [_bare_loop(tmp_path / f"candidate-{index}") for index in range(5)]
    for index, loop in enumerate(loops):
        loop.success_registry_path = registry
        loop.config.max_successful_streams = 4
        _write_stream(
            loop.stream_dir,
            [f"candidate-{index}-task-{task:02d}" for task in range(20)],
        )

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(
            lambda loop: loop.verify_overlap(_passed_oracle()),
            loops,
        ))

    assert sum(result.passed for result in results) == 4
    assert [result.layer for result in results].count("success-target") == 1
    payload = _load_success_registry(registry, max_successful_streams=4)
    assert payload["max_successful_streams"] == 4
    assert len(payload["streams"]) == 4


def test_container_readiness_reports_terminal_proxy_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.codex_container = "stale-codex"

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        if command[:2] == ["docker", "exec"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="container not running")
        assert command[:3] == ["docker", "container", "inspect"]
        return SimpleNamespace(returncode=0, stdout="status=exited exit=0 error=\n", stderr="")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="stayed terminal") as exc_info:
        loop._wait_for_codex_container(timeout_sec=60)
    assert "status=exited exit=0" in str(exc_info.value)
    assert "container not running" in str(exc_info.value)


def test_promptless_interrupted_codex_boundary_reruns_verifier(
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.state.status = "codex-refine"
    loop.state.codex_turn = 0
    loop.state.final_reason = "stale failure"

    recovered = loop._recover_pending_codex()

    assert recovered is None
    assert loop.state.status == "candidate-applied"
    assert loop.state.codex_turn == 0
    assert loop.state.final_reason == ""
    saved = json.loads(loop.state_path.read_text())
    assert saved["status"] == "candidate-applied"


def test_missing_rollout_discards_stale_session_and_reruns_verifier(
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.state.status = "codex-refine"
    loop.state.codex_turn = 2
    loop.state.session_id = "stale-thread"
    (loop.logs_dir / "turn-02-prompt.txt").write_text("repair this")
    (loop.logs_dir / "turn-02-codex.jsonl").write_text(
        "Error: thread/resume failed: no rollout found for thread id stale-thread\n"
    )

    recovered = loop._recover_pending_codex()

    assert recovered is None
    assert loop.state.status == "candidate-applied"
    assert loop.state.session_id is None
    assert loop.state.codex_turn == 2


def test_run_codex_retries_expired_session_as_fresh_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.codex_container = "codex-test"
    loop.codex_home = loop.run_dir / "codex_home"
    loop.state.session_id = "expired-thread"
    loop._sync_diagnostic_logs = lambda: None
    loop._stage_container_workspace = lambda: None
    loop._collect_container_state = lambda: None
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        if command[:3] == ["docker", "exec", "-u"]:
            return SimpleNamespace(returncode=0)
        calls.append(command)
        output = kwargs["stdout"]
        if len(calls) == 1:
            output.write(
                "Error: thread/resume failed: no rollout found for thread id expired-thread\n"
            )
        else:
            output.write('{"thread_id":"fresh-thread"}\n')
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0)

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    rc, _ = loop.run_codex("repair this")

    assert rc == 0
    assert len(calls) == 2
    assert "resume" in calls[0]
    assert "resume" not in calls[1]
    assert loop.state.session_id == "fresh-thread"


def test_diagnostic_logs_are_synced_with_oracle_artifacts(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    loop.workspace.mkdir(parents=True)
    attempt_logs = loop.logs_dir / "attempt-00"
    attempt_logs.mkdir()
    (attempt_logs / "build.log").write_text(
        "API_KEY=do-not-copy\nfirst causal build error\n"
    )
    verifier = loop.jobs_dir / "attempt-00" / "trial" / "verifier"
    (verifier / "a").mkdir(parents=True)
    (verifier / "reward.json").write_text(json.dumps({"a": 0.0, "c": 1.0}))
    (verifier / "a" / "stdout.txt").write_text("AssertionError: output missing\n")
    agent = verifier.parent / "agent"
    agent.mkdir()
    (agent / "oracle.txt").write_text("solution command failed\n")

    before = _tree_hashes(loop.workspace)
    loop._sync_diagnostic_logs()
    after = _tree_hashes(loop.workspace)

    copied = loop.workspace / "logs" / "attempt-00"
    assert "do-not-copy" not in (copied / "build.log").read_text()
    oracle = copied / "oracle-artifacts"
    assert json.loads((oracle / "reward.json").read_text()) == {"a": 0.0, "c": 1.0}
    assert "output missing" in (oracle / "verifier" / "a" / "stdout.txt").read_text()
    assert "solution command failed" in (oracle / "oracle.txt").read_text()
    assert before == after


def test_build_verifier_writes_prebuilt_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])

    commands = []

    def fake_popen(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(pid=12345, wait=lambda timeout=None: 0)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    result = loop.verify_build()
    assert result.passed
    assert "--push" in commands[0]
    assert "--load" not in commands[0]
    assert any(
        value.startswith("zhuerle/aosbench:aos-refine-stream-")
        for value in commands[0]
    )
    assert "docker_image" in (loop.stream_dir / "stream.toml").read_text()


def test_build_verifier_retries_transient_failure_without_codex_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.build_retries = 2
    loop.config.build_retry_backoff_sec = 0
    _write_stream(loop.stream_dir, ["a", "c"])
    calls = 0

    def fake_popen(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            kwargs["stdout"].write(
                "ERROR: Connection broken: InvalidChunkLength(got length b'', 0 bytes read)\n"
            )
            kwargs["stdout"].flush()
            return SimpleNamespace(pid=12345, wait=lambda timeout=None: 1)
        return SimpleNamespace(pid=12346, wait=lambda timeout=None: 0)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    result = loop.verify_build()

    assert result.passed
    assert calls == 2
    assert result.details["transient_retries"] == 1
    assert result.details["build_attempts"] == 2


def test_build_verifier_does_not_retry_semantic_dependency_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.build_retries = 2
    loop.config.build_retry_backoff_sec = 0
    _write_stream(loop.stream_dir, ["a", "c"])
    calls = 0

    def fake_popen(command, **kwargs):
        nonlocal calls
        calls += 1
        kwargs["stdout"].write(
            "ERROR: No matching distribution found for scipy==1.10.1\n"
        )
        kwargs["stdout"].flush()
        return SimpleNamespace(pid=12345, wait=lambda timeout=None: 1)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    result = loop.verify_build()

    assert not result.passed
    assert calls == 1
    assert result.details["transient_retries"] == 0


def test_build_verifier_classifies_each_retry_from_its_own_log_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.build_retries = 2
    loop.config.build_retry_backoff_sec = 0
    _write_stream(loop.stream_dir, ["a", "c"])
    calls = 0

    def fake_popen(command, **kwargs):
        nonlocal calls
        calls += 1
        message = (
            "ERROR: Connection broken: InvalidChunkLength\n"
            if calls == 1
            else "ERROR: No matching distribution found for scipy==1.10.1\n"
        )
        kwargs["stdout"].write(message)
        kwargs["stdout"].flush()
        return SimpleNamespace(pid=12345 + calls, wait=lambda timeout=None: 1)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    result = loop.verify_build()

    assert not result.passed
    assert calls == 2
    assert result.details["transient_retries"] == 1
    assert not result.details["transient_failure"]


def test_build_timeout_terminates_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])
    waits = 0
    signals = []

    def wait(timeout=None):
        nonlocal waits
        waits += 1
        if waits == 1:
            raise orchestrator.subprocess.TimeoutExpired("docker buildx build", timeout)
        return -15

    monkeypatch.setattr(
        "subprocess.Popen",
        lambda command, **kwargs: SimpleNamespace(pid=54321, wait=wait),
    )
    monkeypatch.setattr(orchestrator.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    result = loop.verify_build()

    assert not result.passed
    assert result.details["error_signals"] == ["BUILD_TIMEOUT"]
    assert signals == [(54321, orchestrator.signal.SIGTERM)]


def test_dockerfile_repair_ledger_replays_exact_safe_edits(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    before = "FROM ubuntu:24.04\nRUN sed -i old /etc/apt/sources.list\n"
    after = "FROM ubuntu:24.04\nRUN sed -i new /etc/apt/sources.list\n"
    dockerfile.write_text(before)
    chunks = _dockerfile_repair_chunks(before, after)
    loop._dockerfile_repair_ledger_path.write_text(json.dumps({
        "version": 1,
        "repairs": chunks,
    }))

    report = loop._replay_dockerfile_repairs(dockerfile)

    assert dockerfile.read_text() == after
    assert report["applied"] == [0]


def test_oracle_verifier_requires_every_reward(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])

    def fake_run(*args, **kwargs):
        verifier = loop.jobs_dir / "attempt-00" / "job" / "trial" / "verifier"
        (verifier / "a").mkdir(parents=True)
        (verifier / "a" / "stdout.txt").write_text("AssertionError: missing output\n")
        (verifier / "reward.json").write_text(json.dumps({"a": 0.0, "c": 1.0}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    result = loop.verify_oracle()
    assert not result.passed
    assert result.details["failed_tasks"] == ["a"]
    assert "AssertionError: missing output" in result.details["task_error_signals"]["a"]


def test_oracle_verifier_rejects_nonzero_process_with_full_rewards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])

    def fake_run(*args, **kwargs):
        verifier = loop.jobs_dir / "attempt-00" / "job" / "trial" / "verifier"
        verifier.mkdir(parents=True)
        (verifier / "reward.json").write_text(json.dumps({"a": 1.0, "c": 1.0}))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr("subprocess.run", fake_run)
    result = loop.verify_oracle()

    assert not result.passed
    assert result.details["returncode"] == 7
    assert result.details["failed_tasks"] == []


def test_oracle_without_reward_is_retryable_infrastructure_not_task_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])

    def fake_run(*args, **kwargs):
        kwargs["stdout"].write(
            "RuntimeError: dispatcher demo did not pass /healthz within 60s: "
            "container is not running\n"
        )
        kwargs["stdout"].flush()
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr("subprocess.run", fake_run)
    result = loop.verify_oracle()

    assert not result.passed
    assert result.details["failed_tasks"] == []
    assert result.details["candidate_tasks"] == ["a", "c"]
    assert result.details["failure_class"] == "dispatcher-health"
    assert result.details["infrastructure_failure"] is True


def _oracle_cmd(loop, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], dict]:
    """Run verify_oracle with subprocess stubbed and return (argv, env)."""
    captured: dict = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        verifier = loop.jobs_dir / "attempt-00" / "job" / "trial" / "verifier"
        verifier.mkdir(parents=True)
        (verifier / "reward.json").write_text(json.dumps({"a": 1.0, "c": 1.0}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    loop.verify_oracle()
    return captured["cmd"], captured["env"]


def test_oracle_defaults_to_docker_with_the_v03_dispatcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The historical invocation must be unchanged by the new knobs."""
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])
    cmd, env = _oracle_cmd(loop, monkeypatch)

    assert cmd[cmd.index("--env") + 1] == "docker"
    assert "--aos-pool-v03" in cmd
    assert "HOST_DISPATCHER_LOGS_PATH" in env
    assert "AOS_POOL_DIR" not in env


def test_oracle_v02_passes_the_pool_source_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """harbor locates aos_bench/pool by walking up from its own __file__, which
    fails under an editable install, so the path must be passed explicitly. The
    v0.3-only dispatcher log path must not be set."""
    loop = _bare_loop(tmp_path)
    loop.config.oracle_pool_version = "v02"
    _write_stream(loop.stream_dir, ["a", "c"])
    cmd, env = _oracle_cmd(loop, monkeypatch)

    assert cmd[cmd.index("--env") + 1] == "docker"
    assert "--aos-pool-v03" not in cmd
    assert "--aos-pool-v02" in cmd
    assert env["AOS_POOL_DIR"].endswith("aos_bench/pool")
    assert "HOST_DISPATCHER_LOGS_PATH" not in env


def test_oracle_v01_passes_no_pool_flag_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v0.1 is harbor's default and has no CLI flag; passing one would abort."""
    loop = _bare_loop(tmp_path)
    loop.config.oracle_pool_version = "v01"
    _write_stream(loop.stream_dir, ["a", "c"])
    cmd, env = _oracle_cmd(loop, monkeypatch)

    assert not [arg for arg in cmd if arg.startswith("--aos-pool-")]
    assert "AOS_POOL_DIR" in env


def test_non_docker_oracle_env_is_rejected_at_config_time(tmp_path: Path) -> None:
    """Only docker can build images and host the v0.3 dispatcher sibling."""
    with pytest.raises(ValueError, match="not supported"):
        LoopConfig(
            stream_dir=tmp_path / "stream",
            run_dir=tmp_path / "run",
            oracle_env="podman",
        )


def test_run_requires_overlap_verdict_after_build_and_oracle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.verify_only = True
    _write_stream(loop.stream_dir, ["a", "c"])
    monkeypatch.setattr(
        loop,
        "verify_build",
        lambda: VerifierResult("build", True, "build passed"),
    )
    monkeypatch.setattr(
        loop,
        "verify_oracle",
        lambda: VerifierResult("oracle", True, "oracle passed"),
    )
    monkeypatch.setattr(
        loop,
        "verify_overlap",
        lambda _: VerifierResult("overlap", False, "overlap failed"),
    )

    assert loop.run() == 1
    assert loop.state.status == "failed"
    assert loop.state.rounds[-1]["overlap"]["passed"] is False


def test_run_stops_without_codex_when_registry_target_is_full(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])
    monkeypatch.setattr(
        loop,
        "verify_build",
        lambda: VerifierResult("build", True, "build passed"),
    )
    monkeypatch.setattr(
        loop,
        "verify_oracle",
        lambda: VerifierResult("oracle", True, "oracle passed"),
    )
    monkeypatch.setattr(
        loop,
        "verify_overlap",
        lambda _: VerifierResult(
            "success-target",
            False,
            "Success target already reached: 4/4",
        ),
    )
    monkeypatch.setattr(
        loop,
        "run_codex",
        lambda _: pytest.fail("Codex must not run after the registry target"),
    )

    assert loop.run() == 0
    assert loop.state.status == "stopped-target-reached"


def test_run_retries_infrastructure_without_codex_or_task_reputation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.infrastructure_retry_backoff_sec = 0
    _write_stream(loop.stream_dir, ["a", "c"])
    build_calls = 0

    def verify_build() -> VerifierResult:
        nonlocal build_calls
        build_calls += 1
        if build_calls == 1:
            return VerifierResult(
                "build",
                False,
                "Docker build timed out",
                {
                    "infrastructure_failure": True,
                    "failure_class": "build-timeout",
                },
            )
        return VerifierResult("build", True, "build passed")

    monkeypatch.setattr(loop, "verify_build", verify_build)
    monkeypatch.setattr(
        loop,
        "verify_oracle",
        lambda: VerifierResult("oracle", True, "oracle passed"),
    )
    monkeypatch.setattr(
        loop,
        "verify_overlap",
        lambda _: VerifierResult("overlap", True, "overlap passed"),
    )
    monkeypatch.setattr(
        loop,
        "run_codex",
        lambda _: pytest.fail("Codex must not run for infrastructure retry"),
    )

    assert loop.run() == 0
    assert build_calls == 2
    assert loop.state.codex_turn == 0
    assert loop.state.failure_counts == {}
    assert loop.state.infrastructure_failure_total == 1
    assert loop.state.rounds[0]["infrastructure_retry"] == "build-timeout"


def test_run_pauses_after_bounded_infrastructure_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = _bare_loop(tmp_path)
    loop.config.max_infrastructure_retries = 1
    loop.config.infrastructure_retry_backoff_sec = 0
    _write_stream(loop.stream_dir, ["a", "c"])
    monkeypatch.setattr(
        loop,
        "verify_build",
        lambda: VerifierResult(
            "build",
            False,
            "Docker build timed out",
            {
                "infrastructure_failure": True,
                "failure_class": "build-timeout",
            },
        ),
    )
    monkeypatch.setattr(
        loop,
        "run_codex",
        lambda _: pytest.fail("Codex must not run for infrastructure retry"),
    )

    assert loop.run() == 75
    assert loop.state.status == "paused-infrastructure"
    assert loop.state.infrastructure_failure_streak == 2
    assert loop.state.infrastructure_failure_total == 2
    assert loop.state.codex_turn == 0
    assert len(loop.state.rounds) == 2


def test_reassemble_applies_valid_task_replacement(tmp_path: Path) -> None:
    loop = _bare_loop(tmp_path)
    _write_stream(loop.stream_dir, ["a", "c"])
    sources = tmp_path / "sources"
    for entry in loop.pool:
        source = sources / entry.task_id
        _write_source(source)
        entry.task_dir = source

    loop._reassemble(["b", "c"])

    raw = (loop.stream_dir / "stream.toml").read_text()
    assert 'task_id            = "b"' in raw
    assert 'task_id            = "c"' in raw
    assert 'task_id            = "a"' not in raw
