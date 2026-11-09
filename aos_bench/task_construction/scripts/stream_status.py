"""Aggregate oracle sweep CSV outputs into a per-stream status markdown.

Reads any number of `notes/oracle_sweep_*_parts/part_*.csv` files (later
sweeps win when the same stream appears in multiple), groups results by
pool, and writes a markdown report to ``notes/stream_status.md``.

Usage::

    # Default: scan all known sweep CSV dirs, write notes/stream_status.md
    python -m aos_bench.task_construction.scripts.stream_status

    # Custom sources (later overrides earlier)
    python -m aos_bench.task_construction.scripts.stream_status \\
        --csv-glob "notes/oracle_sweep_tb2_v4_parts/part_*.csv" \\
        --csv-glob "notes/oracle_sweep_tb2_v5_parts/part_*.csv" \\
        --output notes/stream_status.md
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_STREAM_DIR = _REPO_ROOT / "data" / "stream"
_DEFAULT_OUTPUT = _REPO_ROOT / "notes" / "stream_status.md"
# Globs are merged in-order; later wins for same (stream, task_id) pair.
_DEFAULT_CSV_GLOBS = [
    "notes/oracle_sweep.csv",
    "notes/oracle_sweep_v3.csv",
    "notes/oracle_sweep_tb2_full.csv",
    "notes/oracle_sweep_tb2_parts/part_*.csv",
    "notes/oracle_sweep_tb2_v2_parts/part_*.csv",
    "notes/oracle_sweep_tb2_v3_parts/part_*.csv",
    "notes/oracle_sweep_tb2_v4_parts/part_*.csv",
    "notes/oracle_sweep_tb2_v5_parts/part_*.csv",
    "notes/oracle_sweep_tb2_v6_parts/part_*.csv",
    "notes/oracle_sweep_tb2_v7_parts/part_*.csv",
    "notes/oracle_sweep_skills_wild_v1_parts/part_*.csv",
    "notes/oracle_sweep_all_v8_parts/part_*.csv",
    "notes/oracle_sweep_all_v9_parts/part_*.csv",
    "notes/oracle_sweep_all_v10_parts/part_*.csv",
    "notes/oracle_sweep_all_v10b_parts/part_*.csv",
    "notes/oracle_sweep_all_v11_parts/part_*.csv",
    "notes/oracle_sweep_all_v11b_parts/part_*.csv",
    "notes/oracle_sweep_all_v12_parts/part_*.csv",
    "notes/oracle_sweep_all_v12b_parts/part_*.csv",
    "notes/oracle_sweep_mlds_v13_parts/part_*.csv",
    "notes/oracle_sweep_mlds_v14_parts/part_*.csv",
]


def _load_rows_grouped(csv_globs: list[str]) -> list[list[dict]]:
    """Each CSV file becomes one group; group order = glob order, lexical within."""
    groups: list[list[dict]] = []
    for pat in csv_globs:
        full = _REPO_ROOT / pat if not os.path.isabs(pat) else Path(pat)
        for f in sorted(glob.glob(str(full))):
            with open(f) as fh:
                groups.append(list(csv.DictReader(fh)))
    return groups


def _aggregate(groups: list[list[dict]]) -> dict[str, dict[str, list[str]]]:
    """For each stream, pick the LATEST CSV that mentions it.

    Earlier sweeps (e.g. all-build-fail v3) must NOT bleed into later sweeps
    where the same stream succeeded. Aggregation is stream-scoped: for each
    stream, find the last CSV file that has any row for it, and only use rows
    from that file.
    """
    # For each stream, remember the last group index it appears in.
    last_group_for_stream: dict[str, int] = {}
    for idx, group in enumerate(groups):
        for r in group:
            last_group_for_stream[r["stream"]] = idx

    out: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"solved": [], "failed": [], "no_solver": [], "build_fail": []}
    )
    for idx, group in enumerate(groups):
        for r in group:
            if last_group_for_stream[r["stream"]] != idx:
                continue
            st = r["status"]
            if st in out[r["stream"]]:
                out[r["stream"]][st].append(r["task_id"])
    return out


def _stream_tasks(stream_dir: Path) -> list[str]:
    toml = stream_dir / "stream.toml"
    if not toml.exists():
        return []
    tasks: list[str] = []
    for line in toml.read_text().splitlines():
        s = line.strip()
        if s.startswith("task_id"):
            tasks.append(s.split("=", 1)[1].strip().strip('"'))
    return tasks


def _pool_of(stream: str) -> str:
    """tb2-{pool}-{N}-{seed} → pool (handles 'ml-ds')."""
    parts = stream.split("-")
    if len(parts) < 4:
        return "?"
    pool = parts[1]
    if pool == "ml" and len(parts) >= 5:
        return "ml-ds"
    return pool


def render(stream_dir: Path, csv_globs: list[str]) -> str:
    groups = _load_rows_grouped(csv_globs)
    agg = _aggregate(groups)
    all_streams = sorted(d.name for d in stream_dir.iterdir() if d.is_dir())

    lines: list[str] = []
    add = lines.append

    add("# Task Stream 合成进度")
    add("")
    add(f"_最后更新：{datetime.now().strftime('%Y-%m-%d %H:%M')}_")
    add("")
    add(f"扫描的 stream 数：**{len(all_streams)}**")
    add("")
    add("## 状态图例")
    add("")
    add("- ✅ build OK + oracle 全通过")
    add("- 🟡 build OK + oracle 部分通过")
    add("- ❌ build OK + oracle 全失败")
    add("- 💥 build 失败")
    add("- ⏳ 还未跑")
    add("")

    fully = partial = none_ok = bf = pending = 0
    for s in all_streams:
        d = agg.get(s)
        if d is None:
            pending += 1
        elif d["build_fail"]:
            bf += 1
        elif len(d["failed"]) == 0 and len(d["solved"]) > 0:
            fully += 1
        elif len(d["solved"]) == 0:
            none_ok += 1
        else:
            partial += 1

    add("## 统计")
    add("")
    add("| 状态 | 数量 |")
    add("|---|---|")
    add(f"| ✅ Fully solved | **{fully}** |")
    add(f"| 🟡 Partial | {partial} |")
    add(f"| ❌ All failed | {none_ok} |")
    add(f"| 💥 Build fail | {bf} |")
    add(f"| ⏳ Pending | {pending} |")
    add(f"| **Total** | **{len(all_streams)}** |")
    add("")

    add("## 完全通过的 stream（推荐用于评测框架测试）")
    add("")
    add("| Stream | Tasks | 任务列表 |")
    add("|---|---|---|")
    for s in all_streams:
        d = agg.get(s)
        if not d or d["build_fail"]:
            continue
        if len(d["failed"]) == 0 and len(d["solved"]) > 0:
            add(
                f"| `{s}` | {len(d['solved'])} | {', '.join(d['solved'])} |"
            )
    add("")

    add("## 详细状态（按 pool 分组）")
    add("")

    pools = sorted(set(_pool_of(s) for s in all_streams))
    for pool in pools:
        add(f"### {pool}")
        add("")
        add("| Stream | Build | Oracle | Solved/Total | Failed tasks |")
        add("|---|---|---|---|---|")
        for s in [s for s in all_streams if _pool_of(s) == pool]:
            d = agg.get(s)
            if d is None:
                add(f"| `{s}` | ⏳ | ⏳ | - | - |")
                continue
            if d["build_fail"]:
                add(f"| `{s}` | 💥 | - | - | - |")
                continue
            ok = len(d["solved"])
            fail = len(d["failed"])
            total = ok + fail + len(d["no_solver"])
            if fail == 0 and ok > 0:
                icon = "✅"
            elif ok == 0:
                icon = "❌"
            else:
                icon = "🟡"
            failed_str = ", ".join(d["failed"][:3]) if d["failed"] else "—"
            if len(d["failed"]) > 3:
                failed_str += f", +{len(d['failed']) - 3}"
            add(f"| `{s}` | ✓ | {icon} | {ok}/{total} | {failed_str} |")
        add("")

    add("## Never-solved task（任务在出现的所有 stream 都失败）")
    add("")
    add("可以考虑加进 `_has_unbuildable_pattern` 过滤。")
    add("")
    task_status: dict[str, dict[str, int]] = defaultdict(
        lambda: {"solved": 0, "failed": 0}
    )
    for s, d in agg.items():
        if d["build_fail"]:
            continue
        for t in d["solved"]:
            task_status[t]["solved"] += 1
        for t in d["failed"]:
            task_status[t]["failed"] += 1
    never = sorted(
        ((t, c) for t, c in task_status.items() if c["solved"] == 0 and c["failed"] > 0),
        key=lambda x: -x[1]["failed"],
    )
    add("| Task | Failed in N streams |")
    add("|---|---|")
    for t, c in never:
        add(f"| {t} | {c['failed']} |")
    add("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream-dir", type=Path, default=_DEFAULT_STREAM_DIR)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument(
        "--csv-glob",
        action="append",
        default=None,
        help="One or more CSV glob patterns (relative to repo root or absolute). "
        "Later patterns override earlier ones for the same (stream, task_id). "
        f"Default: {_DEFAULT_CSV_GLOBS}",
    )
    args = parser.parse_args(argv)
    csv_globs = args.csv_glob or _DEFAULT_CSV_GLOBS
    md = render(args.stream_dir, csv_globs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md)
    print(f"wrote {args.output} ({len(md.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
