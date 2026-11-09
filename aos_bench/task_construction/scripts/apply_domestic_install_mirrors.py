"""Apply domestic dependency mirrors to V1 candidates and REFINE workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from aos_bench.task_construction.assemble.install_mirrors import (
    normalize_domestic_install_sources,
)

PASSED_STATUSES = {"passed", "build-passed"}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def apply(
    scan_dir: Path,
    run_root: Path,
    *,
    invalidate_passed: bool = False,
) -> dict[str, object]:
    passed_streams: set[tuple[str, str]] = set()
    for state_path in sorted((run_root / "results").glob("*/*/state.json")):
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError, TypeError):
            continue
        if state.get("status") in PASSED_STATUSES:
            passed_streams.add((state_path.parents[1].name, state_path.parent.name))

    files = sorted(scan_dir.glob("*/*/environment/Dockerfile"))
    files += sorted((run_root / "results").glob("*/*/workspace/environment/Dockerfile"))
    records: list[dict[str, object]] = []
    for path in files:
        before = path.read_text(errors="replace")
        if path.is_relative_to(scan_dir):
            relative = path.relative_to(scan_dir)
        else:
            relative = path.relative_to(run_root / "results")
        stream_key = (relative.parts[0], relative.parts[1])
        if stream_key in passed_streams and not invalidate_passed:
            records.append({
                "path": str(path.resolve()),
                "changed": False,
                "skipped": "passed-stream",
                "before_sha256": _digest(before),
                "after_sha256": _digest(before),
            })
            continue
        after = normalize_domestic_install_sources(before)
        changed = before != after
        if changed:
            temporary = path.with_suffix(".Dockerfile.tmp")
            temporary.write_text(after)
            temporary.replace(path)
        records.append({
            "path": str(path.resolve()),
            "changed": changed,
            "before_sha256": _digest(before),
            "after_sha256": _digest(after),
        })
    invalidated: list[str] = []
    if invalidate_passed:
        for state_path in sorted((run_root / "results").glob("*/*/state.json")):
            state = json.loads(state_path.read_text())
            if state.get("status") not in PASSED_STATUSES:
                continue
            state["status"] = "verifying-build"
            state["final_reason"] = (
                "prior pass invalidated: dependency sources changed to the "
                "domestic mirror policy and require build+Oracle re-verification"
            )
            temporary = state_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
            temporary.replace(state_path)
            invalidated.append(str(state_path.parent.resolve()))

    created_at = time.time()
    result: dict[str, object] = {
        "schema_version": 1,
        "created_at": created_at,
        "scan_dir": str(scan_dir.resolve()),
        "run_root": str(run_root.resolve()),
        "files": len(records),
        "changed": sum(bool(row["changed"]) for row in records),
        "skipped_passed": sum(row.get("skipped") == "passed-stream" for row in records),
        "invalidated_passed": invalidated,
        "records": records,
    }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    history_dir = run_root / "domestic-mirror-rewrites"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"rewrite-{int(created_at * 1000)}.json"
    history_path.write_text(payload)
    manifest = run_root / "domestic-mirror-rewrite.json"
    temporary = manifest.with_suffix(".json.tmp")
    temporary.write_text(payload)
    temporary.replace(manifest)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-dir", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--invalidate-passed", action="store_true")
    args = parser.parse_args()
    print(json.dumps(
        apply(
            args.scan_dir,
            args.run_root,
            invalidate_passed=args.invalidate_passed,
        ),
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
