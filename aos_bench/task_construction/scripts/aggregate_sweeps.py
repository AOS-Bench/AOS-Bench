"""Aggregate per-stream oracle.log files into a unified CSV + regenerate
``data/oracle_blacklist.txt`` from it.

Walks each ``oracle-sweep-*`` directory under ``--log-root`` (default
``$HOME/aos-bench-logs``), reads each ``<stream>/oracle.log``,
parses the trailing ``=== oracle summary ===`` block, and writes one row per
``(stream, task_id, status, reason, ts)``.

Then re-derives the per-task aggregate (always-solved / sometimes-solved /
never-solved / no-solver-only) and writes the never-solved set as the new
``data/oracle_blacklist.txt``.

Usage::

    # default: aggregate all sweeps + write CSV + write blacklist
    python -m aos_bench.task_construction.scripts.aggregate_sweeps

    # only build the CSV, skip blacklist update
    python -m aos_bench.task_construction.scripts.aggregate_sweeps \\
        --no-write-blacklist

    # custom locations
    python -m aos_bench.task_construction.scripts.aggregate_sweeps \\
        --log-root /path/to/sweeps \\
        --csv /tmp/sweep.csv \\
        --blacklist /tmp/blacklist.txt
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOG_ROOT = Path(str(Path.home() / "aos-bench-logs"))
DEFAULT_CSV = _REPO_ROOT / "notes" / "oracle_sweep_aggregate.csv"
DEFAULT_BLACKLIST = _REPO_ROOT / "data" / "oracle_blacklist.txt"

# Sweep-dir priority (later sweeps win on conflict per stream name).
DEFAULT_SWEEP_PRIORITY = (
    "oracle-sweep-tb2-v7",
    "oracle-sweep-skills-wild-v1",
    "oracle-sweep-tb2-v6",
    "oracle-sweep-tb2-full",
)

_SOLVED_RE = re.compile(r"^solved \(\d+\):\s*(.*)$", re.MULTILINE)
_NOSOLVER_RE = re.compile(r"^no_solver \(\d+\):\s*(.*)$", re.MULTILINE)
_FAILED_RE = re.compile(r"^failed \(\d+\):\s*(.*)$", re.MULTILINE)


def _parse_log(path: Path) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    text = path.read_text(errors="ignore")[-4096:]  # summary lives near end
    s_m = _SOLVED_RE.search(text)
    n_m = _NOSOLVER_RE.search(text)
    f_m = _FAILED_RE.search(text)

    def _split(s: str) -> list[str]:
        s = s.strip()
        if not s or s == "—":
            return []
        return [t for t in s.split() if t and not t.startswith("(")]

    solved = _split(s_m.group(1)) if s_m else []
    no_solver = _split(n_m.group(1)) if n_m else []
    failed: list[tuple[str, str]] = []
    if f_m:
        raw = f_m.group(1).strip()
        if raw and raw != "—":
            failed = [
                (tid, reason.strip())
                for tid, reason in re.findall(r"([\w\-]+)\s*\(([^)]*)\)", raw)
            ]
    return solved, no_solver, failed


def build_csv(
    log_root: Path,
    csv_path: Path,
    sweep_priority: tuple[str, ...] = DEFAULT_SWEEP_PRIORITY,
) -> int:
    """Walk ``log_root`` for sweep dirs, parse each oracle.log, write CSV.
    Returns the number of rows written.
    """
    rows: list[tuple[str, str, str, str, int]] = []
    seen_streams: set[str] = set()

    for sweep_name in sweep_priority:
        sweep = log_root / sweep_name
        if not sweep.is_dir():
            continue
        for trial in sorted(sweep.iterdir()):
            if not trial.is_dir() or trial.name in seen_streams:
                continue
            log = trial / "oracle.log"
            if not log.exists():
                continue
            try:
                solved, no_solver, failed = _parse_log(log)
            except Exception as exc:
                print(f"  parse err {trial.name}: {exc}", file=sys.stderr)
                continue
            ts = int(log.stat().st_mtime)
            for tid in solved:
                rows.append((trial.name, tid, "solved", "", ts))
            for tid in no_solver:
                rows.append((trial.name, tid, "no_solver", "", ts))
            for tid, reason in failed:
                rows.append((trial.name, tid, "failed", reason, ts))
            if solved or no_solver or failed:
                seen_streams.add(trial.name)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as cf:
        w = csv.writer(cf)
        w.writerow(["stream", "task_id", "status", "reason", "ts"])
        for r in rows:
            w.writerow(r)

    print(
        f"[csv] {csv_path}: {len(rows)} rows from {len(seen_streams)} streams"
    )
    return len(rows)


def write_blacklist(csv_path: Path, blacklist_path: Path) -> int:
    """Re-derive the never-solved blacklist from the CSV. Flaky tasks are
    listed as comments for reference but NOT included in the active
    blacklist (users iterate seeds to find lucky draws)."""
    by_task: dict[str, dict] = defaultdict(
        lambda: {"solved": 0, "failed": 0, "no_solver": 0, "reasons": set()}
    )
    for row in csv.DictReader(csv_path.open()):
        t = row["task_id"]
        if t == "<stream>":
            continue
        by_task[t][row["status"]] += 1
        if row["status"] == "failed" and row.get("reason"):
            by_task[t]["reasons"].add(row["reason"])

    never: list[tuple[str, int, list[str]]] = []
    flaky: list[tuple[str, int, int]] = []
    for t, c in sorted(by_task.items()):
        if c["solved"] == 0 and c["failed"] > 0:
            never.append((t, c["failed"], sorted(c["reasons"])))
        elif c["solved"] > 0 and c["failed"] > 0:
            flaky.append((t, c["solved"], c["failed"]))

    never_lines = "\n".join(
        f"{t}  # failed×{n} reasons={','.join(r) or 'unknown'}"
        for t, n, r in never
    )
    flaky_lines = "\n".join(
        f"# {t}  solved×{s} failed×{f}" for t, s, f in flaky
    )
    text = f"""# Oracle blacklist — task_ids excluded from generate_streams.py sampling.
#
# Generated by aos_bench.task_construction.scripts.aggregate_sweeps from the
# unified sweep CSV ({csv_path}).
# Format: one task_id per line. Inline `# ...` comments and blank lines OK.
#
# Hard blacklist = oracle never passes (failed across all streams seen).
# Flaky tasks (oracle passes sometimes) are NOT blacklisted — they stay in
# the candidate pool, and users iterate seeds (e.g. 42..51) until they find
# one whose sample doesn't draw a flaky task.
#
# ─── Always-broken (n={len(never)}; permanently excluded) ───
{never_lines}
#
# ─── For reference: flaky (n={len(flaky)}; NOT blacklisted, may fail in some seeds) ───
{flaky_lines}
"""
    blacklist_path.parent.mkdir(parents=True, exist_ok=True)
    blacklist_path.write_text(text)
    print(
        f"[blacklist] {blacklist_path}: {len(never)} hard-excluded "
        f"({len(flaky)} flaky kept in pool)"
    )
    return len(never)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--blacklist", type=Path, default=DEFAULT_BLACKLIST)
    ap.add_argument(
        "--no-write-blacklist",
        action="store_true",
        help="Build CSV only; skip rewriting the blacklist file.",
    )
    args = ap.parse_args(argv)

    n_rows = build_csv(args.log_root, args.csv)
    if n_rows == 0:
        print("warning: 0 rows written; check --log-root", file=sys.stderr)
        return 1
    if not args.no_write_blacklist:
        write_blacklist(args.csv, args.blacklist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
