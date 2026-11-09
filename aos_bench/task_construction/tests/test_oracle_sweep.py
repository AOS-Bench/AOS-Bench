"""Tests for the stream oracle sweep's run-time contract.

The bug these guard against: `oracle_sweep.py` used to run
`docker run <image> bash /oracle/solve.sh`. Nothing in the repository ever
creates `/oracle/`, and the assembler's anti-leak gate deliberately keeps
`solution/` out of the image, so every task was reported `no_solver` — a
false negative indistinguishable from a real solver gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aos_bench.task_construction.scripts import oracle_sweep


def _make_stream(root: Path, name: str, *, solution: bool = True, tag: str = "") -> Path:
    stream = root / name
    (stream / "environment").mkdir(parents=True)
    if solution:
        sol = stream / "solution"
        (sol / "tasks" / "task-a").mkdir(parents=True)
        (sol / "solve.sh").write_text("#!/usr/bin/env bash\necho stub\n")
        (sol / "tasks" / "task-a" / "solve.sh").write_text("#!/usr/bin/env bash\n")
    if tag:
        logs = stream / "verification-logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "build-result.tsv").write_text(
            "verdict\trc\tlayers\tfailing_step\ttag\tdigest\n"
            f"build-success\t0\t56\t\t{tag}\tsha256:abc\n"
        )
    return stream


def _captured_cmd(monkeypatch, stream: Path, log: Path):
    """Run _docker_run_oracle with docker stubbed out, returning the argv."""
    seen: dict = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        # _docker_run_oracle writes the command line into the log itself and
        # then reads the log back, so the stub must not clobber that handle.
        return _Result()

    monkeypatch.setattr(oracle_sweep.subprocess, "run", fake_run)
    rc, _ = oracle_sweep._docker_run_oracle(stream, log)
    return rc, seen.get("cmd", [])


def test_solution_is_mounted_not_assumed_baked_in(monkeypatch, tmp_path):
    stream = _make_stream(tmp_path, "light-5-scan-001")
    rc, cmd = _captured_cmd(monkeypatch, stream, tmp_path / "run.log")

    assert rc == 0
    # The entrypoint must be the harbor stream contract, not the single-task one.
    assert cmd[-3:] == [cmd[-3], "bash", "/solution/solve.sh"]
    assert "/oracle/solve.sh" not in cmd

    # And the solution tree must actually be supplied at run time.
    mount = f"{(stream / 'solution').resolve()}:/solution"
    assert "-v" in cmd
    assert mount in cmd, f"solution not mounted; argv was {cmd}"


def test_missing_solution_refuses_instead_of_reporting_no_solver(monkeypatch, tmp_path):
    """A stream with no solve.sh must fail loudly, not silently as no_solver."""
    stream = _make_stream(tmp_path, "light-5-scan-002", solution=False)
    log = tmp_path / "run.log"

    called = False

    def fake_run(cmd, **kwargs):  # pragma: no cover - must not be reached
        nonlocal called
        called = True
        raise AssertionError("docker must not run without a solution tree")

    monkeypatch.setattr(oracle_sweep.subprocess, "run", fake_run)
    rc, text = oracle_sweep._docker_run_oracle(stream, log)

    assert not called
    assert rc == -2, "a missing solution must be distinguishable from a pass"
    assert "solution missing" in text
    assert "no_solver" in text, "the log should explain why this is not no_solver"


def test_pushed_tag_is_reused_when_registry_is_set(monkeypatch, tmp_path):
    tag = "registry.example.com/aosbench/aosbench:v1-0-light-5-scan-001-a3"
    stream = _make_stream(tmp_path, "light-5-scan-001", tag=tag)
    monkeypatch.setattr(
        oracle_sweep, "_ORACLE_REGISTRY", "registry.example.com/aosbench"
    )
    assert oracle_sweep._sweep_image(stream) == tag


def test_local_only_tag_is_not_reused_as_a_registry_image(monkeypatch, tmp_path):
    """`localhost/...` was never pushed, so it cannot satisfy a registry run."""
    stream = _make_stream(
        tmp_path, "light-5-scan-010", tag="localhost/aos-stream/light-5-scan-010:cn"
    )
    monkeypatch.setattr(
        oracle_sweep, "_ORACLE_REGISTRY", "registry.example.com/aosbench"
    )
    assert oracle_sweep._sweep_image(stream) == "aos-bench/light-5-scan-010:sweep"


def test_image_falls_back_to_local_tag_without_registry(tmp_path):
    stream = _make_stream(tmp_path, "light-5-scan-001", tag="whatever:1")
    assert oracle_sweep._sweep_image(stream) == "aos-bench/light-5-scan-001:sweep"


def test_nested_container_isolation_flags_are_passed(monkeypatch, tmp_path):
    """Running a container from inside this container needs all three flags.

    `runc` rejects `--cgroups=disabled`, a private netns makes crun write to a
    read-only /proc, and /sys/fs/cgroup is read-only — each flag maps to one of
    those failures, so dropping any of them breaks the sweep on this host.
    """
    stream = _make_stream(tmp_path, "light-5-scan-001")
    _, cmd = _captured_cmd(monkeypatch, stream, tmp_path / "run.log")

    assert "--runtime=crun" in cmd
    assert "--cgroups=disabled" in cmd
    assert "--network=host" in cmd
    # `--isolation` is build-only; podman run exits non-zero on it.
    assert not any(c.startswith("--isolation") for c in cmd)


def test_parses_the_header_the_generator_actually_emits():
    """assemble.py writes "oracle solver summary", not "oracle summary".

    A fully-solved stream used to parse as empty, which is indistinguishable
    from a stream that produced no results at all.
    """
    log = (
        "=== oracle solve: a ===\n"
        "=== Solution complete ===\n"
        "\n"
        "=== oracle solver summary ===\n"
        "solved (2):    task-a task-b\n"
        "no_solver (0): —\n"
        "failed (1):    task-c (exit 1)\n"
    )
    parsed = oracle_sweep._parse_oracle_log(log)
    assert parsed["solved"] == ["task-a", "task-b"]
    assert parsed["no_solver"] == []
    assert parsed["failed"] == ["task-c (exit 1)"]


def test_still_parses_the_legacy_header():
    log = (
        "=== oracle summary ===\n"
        "solved (1):    task-a\n"
        "no_solver (0): —\n"
        "failed (0):    —\n"
    )
    assert oracle_sweep._parse_oracle_log(log)["solved"] == ["task-a"]
