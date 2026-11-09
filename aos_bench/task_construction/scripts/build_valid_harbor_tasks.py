"""Build/refresh data/valid_harbor_tasks.json from sweep history + solver registry.

Combines two sources of truth:

1. **Historical oracle_sweep.csv** (notes/oracle_sweep.csv + per-V0-2-sweep
   summary.csv files under /cloud/.../v0-2-sweep-*/): any tb2/skillsbench
   task that was solved at least once in any prior sweep is "solve-ok".

2. **WildClawBench solver registry** (aos_bench/.../wildclaw_solvers/*.sh):
   any wildclaw task with a registered solver — assumed solve-ok by
   construction (solvers are written against grader contract; first sweep
   will validate).

Output JSON layout::

    {
      "generated_at": "2026-05-15T...",
      "sources": {...},
      "tasks": {
        "tb2": ["cobol-modernization", ...],
        "skillsbench": ["3d-scan-calc", ...],
        "wildclaw": ["01_Productivity_Flow_task_3_bibtex", ...]
      },
      "all": [...]   # flat union, sorted
    }

Usage::

    python -m aos_bench.task_construction.scripts.build_valid_harbor_tasks
    python -m aos_bench.task_construction.scripts.build_valid_harbor_tasks --print
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_OUT = _REPO_ROOT / "data" / "valid_harbor_tasks.json"
_HISTORICAL_CSV = _REPO_ROOT / "notes" / "oracle_sweep.csv"
_V02_SWEEP_GLOB = str(Path.home() / "aos-bench-logs/v0-2-sweep-*/summary.csv")

_TB2_DIR = _REPO_ROOT / "data" / "harbor" / "terminal-bench-2"
_SKB_DIR = _REPO_ROOT / "data" / "harbor" / "skillsbench" / "tasks"
_WCB_DIR = _REPO_ROOT / "data" / "harbor" / "wildclawbench-converted"
_SOLVERS_DIR = _REPO_ROOT / "aos_bench" / "task_construction" / "scripts" / "wildclaw_solvers"


def _gather_solved_from_historical() -> set[str]:
    if not _HISTORICAL_CSV.is_file():
        return set()
    solved: set[str] = set()
    with _HISTORICAL_CSV.open() as f:
        for row in csv.DictReader(f):
            if row.get("status") == "solved":
                solved.add(row["task_id"])
    return solved


def _gather_solved_from_v02_sweeps() -> set[str]:
    """V0-2 sweep summary.csv schema: intensity,stream,task_id,reward (float)."""
    import glob
    solved: set[str] = set()
    for csv_path in glob.glob(_V02_SWEEP_GLOB):
        try:
            with open(csv_path) as f:
                for row in csv.DictReader(f):
                    tid = row.get("task_id", "")
                    if tid.startswith("__"):
                        continue
                    try:
                        if float(row.get("reward", "0")) >= 1.0:
                            solved.add(tid)
                    except (TypeError, ValueError):
                        pass
        except OSError:
            pass
    return solved


def _gather_failed_in_latest_v02_sweep(
    min_pass_rate: float | None,
) -> set[str]:
    """If ``min_pass_rate`` is set, return tasks whose pass rate in the LATEST
    V0-2 sweep is below the threshold. These get removed from the allowlist.

    "Latest" = newest summary.csv by mtime under _V02_SWEEP_GLOB. Pass rate
    is computed only over the latest sweep's samples (not all-time), so
    re-running after a fix can promote a previously-failing task back into
    the allowlist.
    """
    if min_pass_rate is None:
        return set()
    import glob, os
    matches = sorted(glob.glob(_V02_SWEEP_GLOB), key=os.path.getmtime)
    if not matches:
        return set()
    latest = matches[-1]
    tally: dict[str, list[int]] = {}  # tid → [pass, total]
    try:
        for row in csv.DictReader(open(latest)):
            tid = row.get("task_id", "")
            if tid.startswith("__"):
                continue
            tally.setdefault(tid, [0, 0])
            tally[tid][1] += 1
            try:
                if float(row.get("reward", "0")) >= 1.0:
                    tally[tid][0] += 1
            except (TypeError, ValueError):
                pass
    except OSError:
        return set()
    failed = {tid for tid, (p, t) in tally.items() if t > 0 and p / t < min_pass_rate}
    print(f"[allowlist] from latest sweep {os.path.basename(os.path.dirname(latest))}: "
          f"{len(failed)} task(s) excluded for pass-rate < {min_pass_rate:.0%}")
    return failed


def _gather_wildclaw_solvers() -> set[str]:
    if not _SOLVERS_DIR.is_dir():
        return set()
    return {f.stem for f in _SOLVERS_DIR.glob("*.sh")}


def _gather_native_solvers(*dirs: Path) -> set[str]:
    """Source tasks that ship their OWN solution/solve.sh (tb2/skb authors
    typically include one). These are oracle-by-construction — the upstream
    benchmark vouches that solve.sh produces a passing reward. Inclusion here
    means "eligible to try in a sweep" even if we haven't actually run it yet.
    """
    out: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for task_dir in d.iterdir():
            if task_dir.is_dir() and (task_dir / "solution" / "solve.sh").is_file():
                out.add(task_dir.name)
    return out


def build(
    out_path: Path = _DEFAULT_OUT,
    print_only: bool = False,
    min_pass_rate: float | None = None,
) -> dict:
    historical = _gather_solved_from_historical()
    v02 = _gather_solved_from_v02_sweeps()
    solvers = _gather_wildclaw_solvers()
    native = _gather_native_solvers(_TB2_DIR, _SKB_DIR)
    recent_failed = _gather_failed_in_latest_v02_sweep(min_pass_rate)

    tb2_present = {p.name for p in _TB2_DIR.iterdir() if p.is_dir()} if _TB2_DIR.is_dir() else set()
    skb_present = {p.name for p in _SKB_DIR.iterdir() if p.is_dir()} if _SKB_DIR.is_dir() else set()
    wcb_present = {p.name for p in _WCB_DIR.iterdir() if p.is_dir() and p.name != "build_failed"} if _WCB_DIR.is_dir() else set()

    # Eligible if EITHER previously solved (proven) OR ships a native solve.sh
    # (presumed by upstream). For wildclaw, registered solver is the
    # equivalent — wildclaw upstream doesn't ship solve.sh.
    solved_any = historical | v02

    def passes_recency(tid: str) -> bool:
        # If --min-pass-rate is set, drop tasks that consistently failed in
        # the latest V0-2 sweep. (Other tasks not in `recent_failed` may be
        # because they passed, OR because they weren't sampled — both OK.)
        return tid not in recent_failed

    tb2_ok = sorted(t for t in tb2_present if (t in solved_any or t in native) and passes_recency(t))
    skb_ok = sorted(t for t in skb_present if (t in solved_any or t in native) and passes_recency(t))
    wcb_ok = sorted(t for t in wcb_present if t in solvers and passes_recency(t))

    result = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "sources": {
            "historical_csv": str(_HISTORICAL_CSV),
            "v02_sweep_glob": _V02_SWEEP_GLOB,
            "wildclaw_solvers": str(_SOLVERS_DIR),
            "native_tb2_skb_solvers": "data/harbor/{terminal-bench-2,skillsbench/tasks}/<id>/solution/solve.sh",
        },
        "rules": {
            "tb2": "in solved_any OR has native solution/solve.sh; minus tasks failing recent sweep filter",
            "skillsbench": "in solved_any OR has native solution/solve.sh; minus tasks failing recent sweep filter",
            "wildclaw": "has registered wildclaw_solvers/<id>.sh; minus tasks failing recent sweep filter",
            "min_pass_rate": min_pass_rate,
        },
        "counts": {
            "historical_solved": len(historical),
            "v02_solved": len(v02),
            "wildclaw_solvers_registered": len(solvers),
            "native_tb2_skb_solvers": len(native),
            "recent_sweep_filtered_out": len(recent_failed),
            "tb2_eligible": len(tb2_ok),
            "skb_eligible": len(skb_ok),
            "wcb_eligible": len(wcb_ok),
            "total": len(tb2_ok) + len(skb_ok) + len(wcb_ok),
        },
        "tasks": {
            "tb2": tb2_ok,
            "skillsbench": skb_ok,
            "wildclaw": wcb_ok,
        },
        "all": sorted(set(tb2_ok) | set(skb_ok) | set(wcb_ok)),
    }

    if print_only:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        print(f"[wrote] {out_path}")
        for k, v in result["counts"].items():
            print(f"  {k}: {v}")
    return result


def load(path: Path = _DEFAULT_OUT) -> set[str]:
    """Read the registry and return the flat allowlist set."""
    if not path.is_file():
        raise FileNotFoundError(
            f"valid_harbor_tasks registry not found at {path}; "
            "run build_valid_harbor_tasks first."
        )
    return set(json.loads(path.read_text())["all"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--print", action="store_true",
                        help="Print to stdout instead of writing")
    parser.add_argument(
        "--min-pass-rate", type=float, default=None,
        help="If set, drop tasks whose pass rate in the latest V0-2 sweep is "
             "below this threshold (e.g. 0.8 = keep only tasks passing ≥80%% "
             "of the time). Tasks not sampled in the latest sweep are kept.",
    )
    args = parser.parse_args(argv)
    build(out_path=args.out, print_only=args.print, min_pass_rate=args.min_pass_rate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
