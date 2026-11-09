"""Generate V0-2 mixed mega-streams across multiple n + seed values.

Aggregates ALL filtered tb2 + skillsbench + wildclaw candidates into a single
bag (post VLM-judge filter + valid_harbor_tasks allowlist), then for each
(n, seed) deterministically samples N tasks and assembles a stream.

Outputs are organized by intensity bucket. Default sweep: 70 streams
(40 light + 20 medium + 10 heavy):

::

    <out_dir>/
    ├── light/    mixed-5-{seed}/         (n=5, 40 seeds)
    ├── medium/   mixed-10-{seed}/        (n=10, 20 seeds)
    └── heavy/    mixed-30-{seed}/        (n=30, 10 seeds)

Usage::

    # Default sweep: 70 streams (40+20+10)
    python -m aos_bench.task_construction.scripts.generate_v0_2

    # Override per-bucket seed counts
    python -m aos_bench.task_construction.scripts.generate_v0_2 \\
        --bucket light:40 medium:20 heavy:10

    # Single stream
    python -m aos_bench.task_construction.scripts.generate_v0_2 \\
        --n 30 --seed 42 --force
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from aos_bench.task_construction.assemble.assemble import (
    StreamSpec,
    TaskRef,
    assemble,
)
from aos_bench.task_construction.scripts.build_valid_harbor_tasks import (
    load as load_valid_tasks,
    _DEFAULT_OUT as DEFAULT_VALID_TASKS_PATH,
)
from aos_bench.task_construction.scripts.generate_streams import (
    _POOLS,
    DEFAULT_BLACKLIST,
    DEFAULT_HARBOR_DIR,
    PoolDef,
    _DIFFICULTY_ORDER,
    _load_blacklist,
    discover_pool,
)


_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT_DIR = _REPO_ROOT / "data" / "stream" / "V0-2"


# Intensity buckets: (name, n, default seed count). Override via --bucket.
_DEFAULT_BUCKETS: list[tuple[str, int, int]] = [
    ("light",  5, 40),
    ("medium", 10, 20),
    ("heavy",  30, 10),
]


def _arrival_schedule(n: int, cadence: int, window: int) -> list[tuple[int, int]]:
    return [(i * cadence, i * cadence + window) for i in range(n)]


def _collect_all_candidates(
    harbor_dir: Path,
    pools: list[PoolDef],
    blacklist: set[str],
    valid_allowlist: set[str] | None,
    exclude_difficulties: set[str],
) -> list[dict]:
    seen: set[str] = set()
    bag: list[dict] = []
    skipped_not_valid: list[str] = []
    skipped_difficulty: list[str] = []
    for pd in pools:
        rows = discover_pool(harbor_dir, pd)
        keep_count = 0
        not_valid = 0
        too_easy = 0
        for r in rows:
            tid = r["task_id"]
            if tid in blacklist or tid in seen:
                continue
            if valid_allowlist is not None and tid not in valid_allowlist:
                not_valid += 1
                skipped_not_valid.append(tid)
                continue
            # Filter by difficulty (e.g. drop "easy" from tb2/skb so the
            # stream pressure stays calibrated to medium/hard). Wildclaw
            # tasks have difficulty="medium" by converter default — they
            # never trip this filter.
            diff = (r.get("difficulty") or "").lower()
            if diff in exclude_difficulties:
                too_easy += 1
                skipped_difficulty.append(f"{tid}({diff})")
                continue
            seen.add(tid)
            r = dict(r)
            r["pool"] = pd.pool
            r["dataset_key"] = pd.dataset_key
            bag.append(r)
            keep_count += 1
        msg = f"  - {pd.dataset_key}-{pd.pool}: {keep_count} candidate(s)"
        extras = []
        if not_valid:
            extras.append(f"{not_valid} not-in-allowlist")
        if too_easy:
            extras.append(f"{too_easy} excluded-difficulty")
        if extras:
            msg += "  (" + ", ".join(extras) + ")"
        print(msg)
    if skipped_not_valid:
        print(f"  [allowlist] {len(skipped_not_valid)} task(s) excluded (no oracle history / no solver)")
    if skipped_difficulty:
        print(f"  [difficulty] {len(skipped_difficulty)} task(s) excluded ({sorted(exclude_difficulties)}): {skipped_difficulty[:5]}{'...' if len(skipped_difficulty)>5 else ''}")
    return bag


def _build_one(
    *,
    bag: list[dict],
    n: int,
    seed: int,
    cadence: int,
    window: int,
    base_image: str,
    extra_apt: list[str],
    out_dir: Path,
    bucket_name: str,
    force: bool,
    dry_run: bool,
) -> tuple[Path, bool, str]:
    if len(bag) < n:
        return out_dir, False, f"need n={n} but only {len(bag)} candidates"

    rng = random.Random(seed)
    sorted_bag = sorted(bag, key=lambda r: r["task_id"])
    chosen = rng.sample(sorted_bag, n)

    # Shuffle the arrival order: previously sorted by (difficulty, task_id)
    # which leaked benchmark-source correlation (wcb tasks always first, etc.).
    # The shuffle uses the same rng (seeded) so the order remains deterministic.
    rng.shuffle(chosen)

    arrivals = _arrival_schedule(n, cadence, window)
    refs: list[TaskRef] = []
    for (arr, _dl), row in zip(arrivals, chosen, strict=True):
        # 2026-05-20: deadline removed from stream protocol.
        refs.append(
            TaskRef(
                source=row["source_dir"],
                pool=row["pool"],
                task_id=row["task_id"],
                arrive_wall_time=arr,
            )
        )

    name = f"mixed-{n}-{seed}"
    spec = StreamSpec(
        name=name,
        description=(
            f"V0-2 mixed mega-stream: {n} tasks sampled across "
            f"tb2+skillsbench+wildclaw (seed={seed}, intensity={bucket_name})."
        ),
        author="aos-bench task_construction",
        cpus=8,
        memory_mb=16384,
        allow_internet=True,
        max_steps=max(60, 12 * n),
        base_image=base_image,
        accept_mixed_from=True,
        extra_apt_packages=list(extra_apt),
        shared_prefixes=[],
        tasks=refs,
    )

    bucket_dir = out_dir / bucket_name
    bucket_dir.mkdir(parents=True, exist_ok=True)
    out_path = bucket_dir / spec.name
    if dry_run:
        return out_path, True, "dry-run"
    if out_path.exists() and not force:
        return out_path, False, "exists (pass --force)"
    assemble(spec, out_path, force=True)
    return out_path, True, "assembled"


def _parse_buckets(spec_strs: list[str] | None) -> list[tuple[str, int, int]]:
    """`--bucket light:40 medium:20 heavy:10` → list of (name, n, num_seeds)."""
    if not spec_strs:
        return list(_DEFAULT_BUCKETS)
    name_to_n = {name: n for name, n, _ in _DEFAULT_BUCKETS}
    out: list[tuple[str, int, int]] = []
    for s in spec_strs:
        parts = s.split(":")
        if len(parts) == 2:
            name, count = parts
            if name not in name_to_n:
                raise SystemExit(f"unknown bucket {name!r}; use name:n:count")
            out.append((name, name_to_n[name], int(count)))
        elif len(parts) == 3:
            name, n, count = parts
            out.append((name, int(n), int(count)))
        else:
            raise SystemExit(f"bad --bucket spec {s!r}; use name:count or name:n:count")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harbor-dir", type=Path, default=DEFAULT_HARBOR_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--valid-tasks",
        type=Path,
        default=DEFAULT_VALID_TASKS_PATH,
        help="Path to valid_harbor_tasks.json allowlist",
    )
    parser.add_argument(
        "--no-allowlist", action="store_true",
        help="Disable valid_harbor_tasks filter",
    )
    parser.add_argument(
        "--bucket",
        nargs="+",
        default=None,
        help='Override default buckets. Format: "name:count" or "name:n:count". '
             'Default: light:40 medium:20 heavy:10.',
    )
    parser.add_argument(
        "--n", type=int, nargs="+", default=None,
        help="Legacy single-mode: override --bucket with explicit n list",
    )
    parser.add_argument(
        "--seed", type=int, nargs="+", default=None,
        help="Legacy single-mode: explicit seed list (only used with --n)",
    )
    parser.add_argument("--cadence", type=int, default=45)
    parser.add_argument("--window", type=int, default=600)
    parser.add_argument("--base-image", default="ubuntu:24.04")
    parser.add_argument(
        "--extra-apt",
        nargs="+",
        default=[
            "ca-certificates", "curl", "git",
            "python3", "python3-pip", "build-essential",
        ],
    )
    parser.add_argument("--blacklist", type=Path, default=DEFAULT_BLACKLIST)
    parser.add_argument(
        "--exclude-difficulty",
        nargs="+",
        default=["easy"],
        help='Difficulty levels to drop from the pool (default: ["easy"]). '
             'Pass "" to keep all. Wildclaw tasks are difficulty="medium" by '
             'converter default — never affected.',
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    # Resolve bucket plan.
    if args.n:
        seeds = args.seed if args.seed else list(range(1, 21))
        plan: list[tuple[str, int, int]] = []
        for n in sorted(args.n):
            name = next((b for b, n_, _ in _DEFAULT_BUCKETS if n_ == n), f"n{n}")
            for s in seeds:
                plan.append((name, n, s))
    else:
        buckets = _parse_buckets(args.bucket)
        plan = []
        for name, n, num_seeds in buckets:
            for seed in range(1, num_seeds + 1):
                plan.append((name, n, seed))
        print(f"[plan] buckets: {[(b, n, s) for b, n, s in buckets]}")

    print(f"[plan] {len(plan)} stream(s) total")

    valid_allowlist: set[str] | None = None
    if not args.no_allowlist:
        try:
            valid_allowlist = load_valid_tasks(args.valid_tasks)
            print(f"[allowlist] {len(valid_allowlist)} valid task(s) from {args.valid_tasks}")
        except FileNotFoundError as e:
            print(f"[warn] {e}", file=sys.stderr)
            print("[warn] proceeding without allowlist (use --no-allowlist to silence)", file=sys.stderr)

    blacklist = _load_blacklist(args.blacklist)
    if blacklist:
        print(f"[blacklist] {len(blacklist)} task(s) excluded")

    print(f"[discover] aggregating candidates from {len(_POOLS)} pools")
    exclude_diffs = {d.lower() for d in args.exclude_difficulty if d}
    if exclude_diffs:
        print(f"[difficulty filter] dropping: {sorted(exclude_diffs)}")
    bag = _collect_all_candidates(args.harbor_dir, _POOLS, blacklist, valid_allowlist, exclude_diffs)
    print(f"[discover] {len(bag)} unique candidate(s) total")

    max_n = max(n for _, n, _ in plan)
    if len(bag) < max_n:
        raise SystemExit(f"max(n)={max_n} but only {len(bag)} candidates available")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    ok_count = 0
    skip_count = 0
    failures: list[tuple[str, int, int, str]] = []
    for bucket_name, n, seed in plan:
        out_path, ok, reason = _build_one(
            bag=bag, n=n, seed=seed,
            cadence=args.cadence, window=args.window,
            base_image=args.base_image, extra_apt=args.extra_apt,
            out_dir=args.out_dir, bucket_name=bucket_name,
            force=args.force, dry_run=args.dry_run,
        )
        if ok:
            tag = "[dry-run]" if args.dry_run else "[assemble]"
            print(f"  {tag} {out_path.relative_to(args.out_dir)}  ({reason})")
            ok_count += 1
        elif reason.startswith("exists"):
            print(f"  [skip]      {out_path.relative_to(args.out_dir)}  ({reason})")
            skip_count += 1
        else:
            print(f"  [fail]      {bucket_name} n={n} seed={seed}  ({reason})")
            failures.append((bucket_name, n, seed, reason))

    print(f"\n[done] {ok_count} ok, {skip_count} skipped, {len(failures)} failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
