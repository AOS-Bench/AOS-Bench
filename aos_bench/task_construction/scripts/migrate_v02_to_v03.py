"""V0-2 → V0-3 in-place migration.

For each V0-2 stream:

1. Read its `[[arrivals]]` task list from stream.toml.
2. Replace every `06_Safety_Alignment_*` task with a deterministically-
   chosen TB-2 task from the **per-stream unused pool** (valid TB-2 not
   in this stream and not on the global blacklist).
3. Re-sample arrival timestamps from cumulative `N(60s, 30s)` inter-
   arrivals, seeded by the stream seed, freeze into stream.toml.
4. Set `cpus=4, memory_mb=8192` (V0-2 was 8 / 16384).
5. Call `assemble.assemble(spec, V0-3/<bucket>/<name>)` to rebuild the
   Dockerfile, fixtures, solve.sh, and per-task verifier dirs.

The non-safety task IDs and their **order** are preserved from V0-2.

Usage::

    python -m aos_bench.task_construction.scripts.migrate_v02_to_v03
    # or per-bucket:
    python -m aos_bench.task_construction.scripts.migrate_v02_to_v03 \\
        --buckets light medium
    # or single stream:
    python -m aos_bench.task_construction.scripts.migrate_v02_to_v03 \\
        --streams light/mixed-5-20
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tomllib
from pathlib import Path

from aos_bench.task_construction.assemble.assemble import (
    StreamSpec,
    TaskRef,
    assemble,
)
from aos_bench.task_construction.scripts.generate_streams import (
    _POOLS,
    DEFAULT_BLACKLIST,
    DEFAULT_HARBOR_DIR,
    _load_blacklist,
    discover_pool,
)
from aos_bench.task_construction.scripts.build_valid_harbor_tasks import (
    load as load_valid_tasks,
    _DEFAULT_OUT as DEFAULT_VALID_TASKS_PATH,
)


_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_V02_DIR = _REPO_ROOT / "data" / "stream" / "V0-2"
DEFAULT_V03_DIR = _REPO_ROOT / "data" / "stream" / "V0-3"
DEFAULT_PROGRESS_DOC = _REPO_ROOT / "docs" / "tasks" / "V0-3_migration_progress.md"


# Inter-arrival distribution. Cumulative N(60, 30) clipped at 0 — left-tail
# truncation pulls the realized std a little below 30 but the schedule is
# fully deterministic per seed.
_ARRIVAL_MEAN_SEC = 60
_ARRIVAL_STD_SEC = 30


def _build_task_index() -> dict[str, tuple[Path, str]]:
    """Return task_id → (source_dir, pool_label).

    Walks every PoolDef in `_POOLS` once; if a task_id appears in more
    than one pool (shouldn't happen), the first match wins. `pool_label`
    is the human-readable pool name used in `TaskRef.pool`.
    """
    index: dict[str, tuple[Path, str]] = {}
    for pd in _POOLS:
        for row in discover_pool(DEFAULT_HARBOR_DIR, pd):
            tid = row["task_id"]
            if tid in index:
                continue
            index[tid] = (Path(row["source_dir"]), pd.pool)
    return index


def _gaussian_arrival_schedule(n: int, rng: random.Random) -> list[int]:
    """N arrival timestamps; first is 0, rest are cumulative
    `max(0, round(rng.gauss(60, 30)))` inter-arrivals."""
    arrivals = [0]
    for _ in range(n - 1):
        arrivals.append(
            arrivals[-1] + max(0, round(rng.gauss(_ARRIVAL_MEAN_SEC, _ARRIVAL_STD_SEC)))
        )
    return arrivals


def _replacement_pool(
    stream_task_ids: set[str],
    task_index: dict[str, tuple[Path, str]],
    valid_allowlist: set[str],
    blacklist: set[str],
) -> list[str]:
    """TB-2 tasks that are valid AND not on blacklist AND not already in
    this stream. Returned sorted for deterministic sampling."""
    tb2_pool_names = {"swe", "ml-ds", "sci", "security"}  # all TB-2 pool labels
    tb2_all = {
        tid for tid, (_, pool) in task_index.items() if pool in tb2_pool_names
    }
    pool = (tb2_all & valid_allowlist) - blacklist - stream_task_ids
    return sorted(pool)


def _parse_seed(stream_name: str) -> int:
    """`mixed-5-20` → 20. `mixed-30-10` → 10."""
    m = re.fullmatch(r"mixed-\d+-(\d+)", stream_name)
    if not m:
        raise ValueError(f"unexpected stream name format: {stream_name!r}")
    return int(m.group(1))


def _enumerate_v02_streams(v02_root: Path, only: list[str] | None) -> list[Path]:
    if only:
        return [v02_root / s for s in only]
    out: list[Path] = []
    for bucket in ("light", "medium", "heavy"):
        bdir = v02_root / bucket
        if not bdir.is_dir():
            continue
        for d in sorted(bdir.iterdir()):
            if (d / "stream.toml").is_file():
                out.append(d)
    return out


def _enumerate_v02_streams_multi(roots: list[Path], only: list[str] | None) -> list[Path]:
    """Enumerate streams from multiple V0-2 root dirs (e.g. V0-2 + V0-2-failed),
    deduping by `<bucket>/<name>` — later roots override earlier ones."""
    if only:
        # `only` paths apply to the first root that contains them
        out: list[Path] = []
        for s in only:
            for root in roots:
                if (root / s / "stream.toml").is_file():
                    out.append(root / s)
                    break
            else:
                # not found in any root — append against first root so error is clear
                out.append(roots[0] / s)
        return out
    seen: dict[str, Path] = {}
    for root in roots:
        for s in _enumerate_v02_streams(root, None):
            key = f"{s.parent.name}/{s.name}"
            seen[key] = s  # later roots override
    return [seen[k] for k in sorted(seen)]


def migrate_one(
    v02_stream_dir: Path,
    v03_root: Path,
    task_index: dict[str, tuple[Path, str]],
    valid_allowlist: set[str],
    blacklist: set[str],
    cpus: int = 4,
    memory_mb: int = 8192,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """Migrate one V0-2 stream. Returns a dict describing what was changed.

    `dict` schema (also used by the progress doc):
        {
          "stream": "light/mixed-5-20",
          "seed": 20,
          "n": 5,
          "replacements": [(old_safety_id, new_tb2_id), ...],
          "arrivals": [0, 47, 89, ...],
          "out_dir": "data/stream/V0-3/light/mixed-5-20",
          "status": "assembled" | "dry-run" | "skipped (exists)" | "missing source for X",
        }
    """
    bucket = v02_stream_dir.parent.name
    name = v02_stream_dir.name
    seed = _parse_seed(name)
    rel = f"{bucket}/{name}"

    raw = tomllib.loads((v02_stream_dir / "stream.toml").read_text())
    arrivals_in = raw.get("arrivals", [])
    task_ids_in_order = [a["task_id"] for a in arrivals_in]
    n = len(task_ids_in_order)

    # Determine replacement candidates for this stream
    repl_pool = _replacement_pool(
        set(task_ids_in_order), task_index, valid_allowlist, blacklist
    )
    # Decoupled rng — large prime offset so it doesn't replay V0-2's
    # `random.Random(seed).sample(...)` sequence and we get a fresh draw.
    rng = random.Random(seed * 1000003 + 42)

    replacements: list[tuple[str, str]] = []
    new_task_ids: list[str] = []
    # Tasks we've already picked as replacement in THIS stream so we don't
    # double-pick when multiple safety slots appear.
    picked_replacements: set[str] = set()
    for tid in task_ids_in_order:
        # Replace if (a) it's a safety task, or (b) it's blacklisted —
        # V0-2 may contain tasks that have since been deemed broken
        # (e.g. path-tracing's torch install, fix-druid-loophole-cve's
        # kkgithub clone). Without (b) blacklisted tasks survive the
        # migration and break V0-3 builds.
        if tid.startswith("06_Safety_Alignment_") or tid in blacklist:
            avail = [t for t in repl_pool if t not in picked_replacements]
            if not avail:
                raise RuntimeError(
                    f"{rel}: replacement pool exhausted for {tid}"
                )
            new = rng.choice(avail)
            picked_replacements.add(new)
            replacements.append((tid, new))
            new_task_ids.append(new)
        else:
            new_task_ids.append(tid)

    # Re-sample arrivals
    arrivals = _gaussian_arrival_schedule(n, rng)

    # Build TaskRef list
    refs: list[TaskRef] = []
    missing: list[str] = []
    for tid, t_arr in zip(new_task_ids, arrivals, strict=True):
        if tid not in task_index:
            missing.append(tid)
            continue
        src, pool = task_index[tid]
        refs.append(TaskRef(source=src, pool=pool, task_id=tid, arrive_wall_time=t_arr))

    if missing:
        return {
            "stream": rel, "seed": seed, "n": n,
            "replacements": replacements, "arrivals": arrivals,
            "out_dir": None,
            "status": f"missing source for {missing}",
        }

    out_dir = v03_root / bucket / name
    if out_dir.exists() and not force and not dry_run:
        return {
            "stream": rel, "seed": seed, "n": n,
            "replacements": replacements, "arrivals": arrivals,
            "out_dir": str(out_dir.relative_to(_REPO_ROOT)),
            "status": "skipped (exists, pass --force)",
        }

    spec = StreamSpec(
        name=name,
        description=(
            f"V0-3 in-place migration of V0-2 {name}: "
            f"{len(replacements)} safety task(s) replaced, "
            f"cpus={cpus}, memory_mb={memory_mb}, "
            f"arrivals ~ N({_ARRIVAL_MEAN_SEC}s, {_ARRIVAL_STD_SEC}s) cumulative."
        ),
        author="aos-bench task_construction (V0-3 in-place)",
        cpus=cpus, memory_mb=memory_mb, allow_internet=True,
        max_steps=max(60, 12 * n),
        base_image="ubuntu:24.04",
        accept_mixed_from=True,
        extra_apt_packages=[
            "ca-certificates", "curl", "git",
            "python3", "python3-pip", "build-essential",
        ],
        shared_prefixes=[],
        tasks=refs,
    )

    if dry_run:
        return {
            "stream": rel, "seed": seed, "n": n,
            "replacements": replacements, "arrivals": arrivals,
            "out_dir": str(out_dir.relative_to(_REPO_ROOT)),
            "status": "dry-run",
        }

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    assemble(spec, out_dir, force=True)
    return {
        "stream": rel, "seed": seed, "n": n,
        "replacements": replacements, "arrivals": arrivals,
        "out_dir": str(out_dir.relative_to(_REPO_ROOT)),
        "status": "assembled",
    }


def write_progress_doc(out_doc: Path, results: list[dict]) -> None:
    """Render results into a markdown progress doc."""
    out_doc.parent.mkdir(parents=True, exist_ok=True)

    by_bucket: dict[str, list[dict]] = {"light": [], "medium": [], "heavy": []}
    for r in results:
        bucket = r["stream"].split("/")[0]
        by_bucket.setdefault(bucket, []).append(r)

    lines: list[str] = []
    lines.append("# V0-3 Migration Progress\n")
    lines.append("> In-place migration of V0-2 streams to V0-3.")
    lines.append("> Source: `data/stream/V0-2/` → `data/stream/V0-3/`.")
    lines.append("> Each safety task replaced by a deterministically-chosen TB-2 task.")
    lines.append("> Resources: cpus=4, memory_mb=8192. Arrivals: cumulative N(60s, 30s),")
    lines.append("> frozen at generation time.\n")
    lines.append("## Status summary\n")
    lines.append("| bucket | total | gen ✓ | gen ✗ | build ✓ | build ✗ | oracle ✓ | oracle ✗ | accepted |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for bucket in ("light", "medium", "heavy"):
        rs = by_bucket.get(bucket, [])
        gen_ok = sum(1 for r in rs if r["status"] == "assembled")
        gen_fail = sum(1 for r in rs if r["status"].startswith("missing"))
        lines.append(
            f"| {bucket} | {len(rs)} | {gen_ok} | {gen_fail} | 0 | 0 | 0 | 0 | 0 |"
        )
    lines.append("")
    lines.append("(build / oracle columns populated by `oracle_sweep_v0_3.sh` + downstream analysis.)\n")

    lines.append("## Per-stream replacements\n")
    lines.append("Only streams that contained at least one safety task are listed.\n")
    lines.append("| stream | seed | n | safety → replacement |")
    lines.append("|---|---:|---:|---|")
    for r in results:
        if not r["replacements"]:
            continue
        repls = "; ".join(f"{old} → **{new}**" for old, new in r["replacements"])
        lines.append(f"| `{r['stream']}` | {r['seed']} | {r['n']} | {repls} |")
    lines.append("")

    lines.append("## Per-stream status\n")
    lines.append("| stream | seed | n | replacements | gen | build | oracle | reward | notes |")
    lines.append("|---|---:|---:|---:|---|---|---|---|---|")
    for r in results:
        gen = "✓" if r["status"] == "assembled" else (
            "·" if r["status"].startswith("dry-run") else
            "·" if r["status"].startswith("skipped") else "✗"
        )
        note = "" if r["status"] in ("assembled", "dry-run") else r["status"]
        lines.append(
            f"| `{r['stream']}` | {r['seed']} | {r['n']} | "
            f"{len(r['replacements'])} | {gen} | — | — | — | {note} |"
        )
    lines.append("")

    out_doc.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--v02-dir", type=Path, nargs="+",
        default=[DEFAULT_V02_DIR, _REPO_ROOT / "data" / "stream" / "V0-2-failed"],
        help="One or more V0-2 source directories. Default: both V0-2/ and "
             "V0-2-failed/ (re-include failed streams as V0-3 candidates).",
    )
    parser.add_argument("--v03-dir", type=Path, default=DEFAULT_V03_DIR)
    parser.add_argument(
        "--buckets", nargs="+",
        choices=["light", "medium", "heavy"],
        default=None,
        help="Restrict to these buckets (default: all).",
    )
    parser.add_argument(
        "--streams", nargs="+", default=None,
        help='Explicit stream list, e.g. "light/mixed-5-20".',
    )
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory-mb", type=int, default=8192)
    parser.add_argument("--valid-tasks", type=Path, default=DEFAULT_VALID_TASKS_PATH)
    parser.add_argument(
        "--no-allowlist", action="store_true",
        help="Skip valid_harbor_tasks.json filter for replacement candidates.",
    )
    parser.add_argument("--blacklist", type=Path, default=DEFAULT_BLACKLIST)
    parser.add_argument(
        "--progress-doc", type=Path, default=DEFAULT_PROGRESS_DOC,
        help="Where to write the per-stream progress table.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    # Default mirror — tencent has been intermittently unreachable on this
    # host since 2026-05-19 (see docs/tasks/task_design.md §8.8).
    os.environ.setdefault("AOS_DOCKERFILE_MIRROR", "tuna")

    print(f"[index] building task → source-dir index from {len(_POOLS)} pools")
    task_index = _build_task_index()
    print(f"[index] {len(task_index)} unique tasks indexed")

    valid_allowlist: set[str] | None = None
    if not args.no_allowlist:
        try:
            valid_allowlist = load_valid_tasks(args.valid_tasks)
            print(f"[allowlist] {len(valid_allowlist)} valid task(s)")
        except FileNotFoundError as e:
            print(f"[warn] {e}; running without allowlist", file=sys.stderr)
            valid_allowlist = set(task_index)
    else:
        valid_allowlist = set(task_index)

    blacklist = _load_blacklist(args.blacklist)
    print(f"[blacklist] {len(blacklist)} task(s) excluded from replacement pool")

    streams = _enumerate_v02_streams_multi(args.v02_dir, args.streams)
    if args.buckets:
        streams = [s for s in streams if s.parent.name in args.buckets]
    print(f"[plan] {len(streams)} V0-2 stream(s) to migrate (from {len(args.v02_dir)} source dir(s))")

    results: list[dict] = []
    n_assembled = n_skipped = n_failed = 0
    for s in streams:
        try:
            r = migrate_one(
                s, args.v03_dir, task_index,
                valid_allowlist, blacklist,
                cpus=args.cpus, memory_mb=args.memory_mb,
                force=args.force, dry_run=args.dry_run,
            )
        except Exception as e:
            r = {
                "stream": f"{s.parent.name}/{s.name}",
                "seed": _parse_seed(s.name),
                "n": 0, "replacements": [], "arrivals": [],
                "out_dir": None,
                "status": f"error: {type(e).__name__}: {e}",
            }
        results.append(r)
        tag = (
            "[assemble]" if r["status"] == "assembled" else
            "[dry-run] " if r["status"] == "dry-run" else
            "[skip]    " if r["status"].startswith("skipped") else
            "[FAIL]    "
        )
        nrepl = len(r["replacements"])
        print(f"  {tag} {r['stream']:<28s} repl={nrepl}  {r['status']}")
        if r["status"] == "assembled":
            n_assembled += 1
        elif r["status"].startswith("skipped"):
            n_skipped += 1
        elif r["status"] != "dry-run":
            n_failed += 1

    print(f"\n[done] {n_assembled} assembled, {n_skipped} skipped, {n_failed} failed")

    if not args.dry_run:
        write_progress_doc(args.progress_doc, results)
        print(f"[doc] wrote {args.progress_doc.relative_to(_REPO_ROOT)}")

    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
