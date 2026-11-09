"""Tests for the V1-0 build-time task-family exclusion policy.

History: WildClawBench and the security pool were both excluded at pool-load
time.  Both exclusions are now LIFTED (2026-09-15):

* WildClawBench needed an LLM judge that did not exist; one is now provisioned
  and verified (deepseek-v4-flash for text, gemini-3.5-flash for the 9 of 10
  L2/L3 graders that send ``image_url``).
* The 8 security tasks are all L1, so they never reached medium/heavy sampling
  and excluding them only cost light-tier diversity.

These tests pin the lift so it cannot silently regress, and keep covering the
ID-matching logic so that re-adding a family stays a one-line change.
"""

from __future__ import annotations

import json
from pathlib import Path

from aos_bench.task_construction.scripts.generate_v1_0 import (
    _EXCLUDED_POOL_PREFIXES,
    _LIFTED_SECURITY_TASK_IDS,
    _SECURITY_TASK_IDS,
    is_policy_excluded,
    load_pool,
)


_SECURITY_MANIFEST = (
    Path(__file__).resolve().parents[1] / "manifests" / "tb2-sec.toml"
)


def test_no_family_is_currently_excluded() -> None:
    """Both historical exclusions are lifted."""
    assert _SECURITY_TASK_IDS == frozenset()
    assert _EXCLUDED_POOL_PREFIXES == ()


def test_wildclawbench_is_admitted() -> None:
    """Requires the provisioned judge; see module docstring."""
    assert not is_policy_excluded(
        "wildclawbench-02_Code_Intelligence_task_3_jigsaw_puzzle_zh"
    )
    assert not is_policy_excluded("wildclawbench-01_Productivity_Flow_task_3_bibtex")


def test_security_tasks_are_admitted_in_both_spellings() -> None:
    """Pool IDs carry a ``tb2-`` prefix; tb2-sec.toml lists the bare names."""
    assert not is_policy_excluded("crack-7z-hash")
    assert not is_policy_excluded("tb2-crack-7z-hash")
    assert not is_policy_excluded("tb2-fix-code-vulnerability-2")


def test_ordinary_tasks_are_kept() -> None:
    for task_id in (
        "tb2-regex-chess",
        "tb2-regex-chess-2",
        "terminalworld-tw_528959",
        "skillsbench-seismic-phase-picking",
        "compilebench-curl-ssl-arm64-static",
    ):
        assert not is_policy_excluded(task_id), task_id


def test_lifted_security_list_still_matches_the_manifest() -> None:
    """The recorded lift must stay faithful to tb2-sec.toml.

    If the manifest gains a task, this fails -- which is the signal to decide
    explicitly whether the new task is admitted, rather than letting the
    provenance record drift.
    """
    task_ids = {
        line.split('"')[1]
        for line in _SECURITY_MANIFEST.read_text().splitlines()
        if line.startswith("task_id")
    }
    assert task_ids, "no task_id rows parsed from tb2-sec.toml"
    assert task_ids == set(_LIFTED_SECURITY_TASK_IDS)


def test_exclusion_logic_still_works_when_a_family_is_re_added(monkeypatch) -> None:
    """Guard the matching logic itself, not just the empty default."""
    module = "aos_bench.task_construction.scripts.generate_v1_0"
    monkeypatch.setattr(module + "._SECURITY_TASK_IDS", frozenset({"crack-7z-hash"}))
    monkeypatch.setattr(module + "._EXCLUDED_POOL_PREFIXES", ("wildclawbench-",))
    assert is_policy_excluded("crack-7z-hash")
    assert is_policy_excluded("tb2-crack-7z-hash")
    assert is_policy_excluded("tb2-crack-7z-hash-2")
    assert is_policy_excluded("wildclawbench-anything")
    assert not is_policy_excluded("tb2-regex-chess")


def _write_pool(tmp_path: Path, task_ids: list[str]) -> Path:
    """Build a pool whose entries all pass load_pool's structural checks."""
    tasks = []
    for index, task_id in enumerate(task_ids):
        task_dir = tmp_path / "tasks" / task_id
        (task_dir / "environment").mkdir(parents=True)
        (task_dir / "task.toml").write_text("[task]\n")
        (task_dir / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
        tasks.append(
            {
                "task_id": task_id,
                "benchmark": "TB2",
                "task_dir": str(task_dir),
                "mean_wall_seconds": 1800.0 + index,
                "mean_cpu_utilization_ratio": 0.5,
                "mean_cpu_time_cumulative_s": 900.0,
                "mean_reward": 1.0,
                "mean_v1_score": 0.25,
                "v1_tier": "L3",
            }
        )
    summary = tmp_path / "pool.json"
    summary.write_text(json.dumps({"tasks": tasks}))
    return summary


def test_load_pool_admits_all_families(tmp_path) -> None:
    summary = _write_pool(
        tmp_path,
        [
            "tb2-regex-chess",
            "tb2-crack-7z-hash",
            "wildclawbench-01_Productivity_Flow_task_3_bibtex",
        ],
    )

    loaded = {entry.task_id for entry in load_pool(summary)}

    assert loaded == {
        "tb2-regex-chess",
        "tb2-crack-7z-hash",
        "wildclawbench-01_Productivity_Flow_task_3_bibtex",
    }
