"""Materialize the complete V1-0 task score catalog.

This merges the original exp_0712 compute-density summary with the tracked
external-task supplement. Classification is deliberately independent of the
ready whitelist and historical blacklists: every task with a usable 4c/8G
measurement receives an L1/L2/L3 tier, and missing measurements are recorded
as ``unclassified``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BASE_SUMMARY = Path(
    str(Path.home() / "aos_logs/exp_0712-compute-density/dataset_summary.json")
)
DEFAULT_EXTERNAL_SUMMARY = (
    REPO_ROOT
    / "aos_bench"
    / "task_construction"
    / "manifests"
    / "v1_0_external_task_scores.json"
)
DEFAULT_READY_WHITELIST = (
    REPO_ROOT
    / "aos_bench"
    / "task_construction"
    / "manifests"
    / "v1_0_ready_task_ids.txt"
)
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "aos_bench"
    / "task_construction"
    / "manifests"
    / "v1_0_task_scores.json"
)


def score(wall_seconds: float, cpu_utilization_ratio: float) -> float:
    return min(max(wall_seconds, 0.0) / 3600.0, 1.0) * min(
        max(cpu_utilization_ratio, 0.0), 1.0
    )


def v1_tier(value: float) -> str:
    if value >= 0.10:
        return "L3"
    if value >= 0.02:
        return "L2"
    return "L1"


def cpu_tier(cpu_time: float | None) -> str | None:
    if cpu_time is None:
        return None
    if cpu_time >= 600:
        return "L3"
    if cpu_time >= 60:
        return "L2"
    return "L1"


def build_catalog(
    base_summary: Path,
    external_summary: Path,
    ready_whitelist: Path,
) -> dict:
    base = json.loads(base_summary.read_text())
    external = json.loads(external_summary.read_text())
    ready_ids = set(ready_whitelist.read_text().split())

    # A later primary sweep wins over the supplement if it contains the same
    # task ID, so refreshing dataset_summary naturally supersedes this import.
    merged = {item["task_id"]: (item, "external_supplement") for item in external}
    merged.update({item["task_id"]: (item, "exp_0712") for item in base})

    tasks = []
    for task_id, (item, source_summary) in sorted(merged.items()):
        run = item.get("runs", {}).get("unified_4c8g") or {}
        wall = run.get("wall_seconds")
        util = run.get("cpu_utilization_ratio")
        cpu_time = run.get("cpu_time_cumulative_s")
        if wall is None or util is None:
            value = None
            tier = "unclassified"
        else:
            value = score(float(wall), float(util))
            tier = v1_tier(value)

        admission = item.get("admission_status")
        if admission is None:
            admission = "ready" if task_id in ready_ids else "unfiltered"

        tasks.append(
            {
                "task_id": task_id,
                "pool": item.get("pool"),
                "raw_task_name": item.get("raw_task_name"),
                "task_dir": item.get("task_dir"),
                "wall_seconds": wall,
                "cgroup_sample_span_s": run.get("cgroup_sample_span_s"),
                "cpu_time_cumulative_s": cpu_time,
                "cpu_utilization_ratio": util,
                "v1_score": round(value, 6) if value is not None else None,
                "v1_tier": tier,
                "cpu_tier": cpu_tier(cpu_time),
                "reward": run.get("reward"),
                "run_status": run.get("status"),
                "admission_status": admission,
                "eligible_by_v1_tier": tier in {"L2", "L3"},
                "source_summary": source_summary,
                "source_run": run.get("source_run"),
            }
        )

    counts = Counter(task["v1_tier"] for task in tasks)
    return {
        "schema_version": 1,
        "generated_by": "aos_bench.task_construction.scripts.build_v1_task_scores",
        "classification_ignores_blacklist": True,
        "reference_config": {"cpus": 4, "memory_mb": 8192},
        "score_formula": (
            "min(wall_seconds / 3600, 1) * "
            "min(cpu_utilization_ratio, 1)"
        ),
        "tier_thresholds": {"L1": "score < 0.02", "L2": "0.02 <= score < 0.10", "L3": "score >= 0.10"},
        "counts": dict(sorted(counts.items())),
        "tasks": tasks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-summary", type=Path, default=DEFAULT_BASE_SUMMARY)
    parser.add_argument("--external-summary", type=Path, default=DEFAULT_EXTERNAL_SUMMARY)
    parser.add_argument("--ready-whitelist", type=Path, default=DEFAULT_READY_WHITELIST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    catalog = build_catalog(
        args.base_summary,
        args.external_summary,
        args.ready_whitelist,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalog, indent=2) + "\n")
    print(f"wrote {len(catalog['tasks'])} tasks -> {args.output}")
    print(f"counts: {catalog['counts']}")


if __name__ == "__main__":
    main()
