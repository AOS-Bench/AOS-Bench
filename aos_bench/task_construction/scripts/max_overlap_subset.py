#!/usr/bin/env python3
"""Largest set of streams that pairwise satisfies the overlap gate.

    overlap(A, B) = |A & B| / min(|A|, |B|) < 0.60     (strict)

This is a maximum independent set problem, not a count: build a graph whose
nodes are streams and whose edges join any pair that VIOLATES the gate, then
find the largest node set with no edge inside it. Greedy gives a lower bound
that can be far off, so this runs exact search on each connected component
(they are small) and falls back to a reported bound only if a component is too
large to finish.

Two properties of the gate drive the whole thing, and both are easy to get
wrong:

  * `min` is in the denominator, so the binding peer is whichever stream has
    the FEWEST tasks. A 20-task heavy stream sharing 3 tasks with a 5-task
    light stream is already at 0.60 and fails. Mixing tiers therefore costs far
    more than it looks.
  * strict inequality: exactly 0.60 fails.

Usage:
    python max_overlap_subset.py --root data/stream/V1-0/tier-v2 \
        --dir full-valid --dir full-valid-0915-new --dir full-valid-diverse0915
    python max_overlap_subset.py ... --per-tier    # solve each tier separately
"""

from __future__ import annotations

import argparse
import itertools
import pathlib
import sys
import tomllib

GATE = 0.60
TIERS = ("light", "medium", "heavy", "heavy-plus")


def task_ids(stream_dir: pathlib.Path) -> frozenset[str]:
    """Task ids of a stream, read from stream.toml."""
    spec = stream_dir / "stream.toml"
    if not spec.is_file():
        return frozenset()
    try:
        data = tomllib.loads(spec.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return frozenset()

    ids: set[str] = set()
    # Task ids live in `arrivals[].task_id` in the current schema. The other
    # shapes are accepted because older streams used them; returning an empty
    # set on an unrecognised shape would be the worst outcome here, since two
    # empty sets never violate the gate and the answer would silently inflate.
    for key in ("arrivals", "tasks", "task", "stream_tasks"):
        entries = data.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    tid = entry.get("task_id") or entry.get("id") or entry.get("name")
                    if tid:
                        ids.add(str(tid))
                elif isinstance(entry, str):
                    ids.add(entry)
    if not ids:
        tasks_dir = stream_dir / "tasks"
        if tasks_dir.is_dir():
            ids = {p.name for p in tasks_dir.iterdir() if p.is_dir()}
    return frozenset(ids)


def collect(root: pathlib.Path, dirs: list[str]) -> dict[str, tuple[str, frozenset[str]]]:
    """name -> (tier, task ids). Hidden dirs are skipped: refine waves leave
    .<stream>.pre-oracle90-refine-<ts>/ snapshots that carry a full stream.toml
    and would otherwise be counted as extra corpus members."""
    found: dict[str, tuple[str, frozenset[str]]] = {}
    for d in dirs:
        for tier in TIERS:
            tier_dir = root / d / tier
            if not tier_dir.is_dir():
                continue
            for stream in sorted(tier_dir.iterdir()):
                if not stream.is_dir() or stream.name.startswith("."):
                    continue
                ids = task_ids(stream)
                if not ids:
                    continue
                # Same stream promoted into several dirs: keep one copy.
                found.setdefault(stream.name, (tier, ids))
    return found


def violates(a: frozenset[str], b: frozenset[str]) -> bool:
    denom = min(len(a), len(b))
    if denom == 0:
        return False
    return len(a & b) / denom >= GATE


def components(nodes: list[str], adj: dict[str, set[str]]) -> list[list[str]]:
    seen: set[str] = set()
    out: list[list[str]] = []
    for node in nodes:
        if node in seen:
            continue
        stack = [node]
        comp: list[str] = []
        seen.add(node)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        out.append(comp)
    return out


def max_independent_set(
    comp: list[str], adj: dict[str, set[str]], budget: int
) -> tuple[list[str], bool]:
    """Exact branch-and-bound MIS on one component.

    Returns (set, exact). exact=False means the budget ran out and the result
    is only a lower bound -- reported as such rather than passed off as the
    answer.
    """
    best: list[str] = []
    calls = 0
    order = sorted(comp, key=lambda n: len(adj[n] & set(comp)), reverse=True)

    def recurse(remaining: list[str], chosen: list[str]) -> bool:
        nonlocal best, calls
        calls += 1
        if calls > budget:
            return False
        if len(chosen) > len(best):
            best = list(chosen)
        if not remaining:
            return True
        # Bound: even taking everything left cannot beat best.
        if len(chosen) + len(remaining) <= len(best):
            return True
        pivot = remaining[0]
        rest = remaining[1:]
        # Branch 1: take pivot, drop its neighbours.
        if not recurse([n for n in rest if n not in adj[pivot]], chosen + [pivot]):
            return False
        # Branch 2: skip pivot.
        return recurse(rest, chosen)

    exact = recurse(order, [])
    return best, exact


def solve(
    streams: dict[str, tuple[str, frozenset[str]]], budget: int
) -> tuple[list[str], bool, int]:
    names = sorted(streams)
    adj: dict[str, set[str]] = {n: set() for n in names}
    edges = 0
    for a, b in itertools.combinations(names, 2):
        if violates(streams[a][1], streams[b][1]):
            adj[a].add(b)
            adj[b].add(a)
            edges += 1

    selected: list[str] = []
    exact = True
    for comp in components(names, adj):
        if len(comp) == 1:
            selected.extend(comp)
            continue
        got, ok = max_independent_set(comp, adj, budget)
        selected.extend(got)
        exact = exact and ok
    return sorted(selected), exact, edges


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/stream/V1-0/tier-v2")
    ap.add_argument("--dir", action="append", required=True)
    ap.add_argument("--per-tier", action="store_true")
    ap.add_argument("--budget", type=int, default=2_000_000)
    ap.add_argument("--list", action="store_true", help="print the chosen streams")
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    streams = collect(root, args.dir)
    if not streams:
        print("no streams with readable task ids found")
        return 1

    by_tier: dict[str, int] = {}
    for tier, _ in streams.values():
        by_tier[tier] = by_tier.get(tier, 0) + 1

    print(f"streams read (deduplicated): {len(streams)}")
    for tier in TIERS:
        if tier in by_tier:
            sizes = sorted(
                len(ids) for t, ids in streams.values() if t == tier
            )
            print(
                f"  {tier:11s} {by_tier[tier]:3d}   task counts "
                f"{sizes[0]}..{sizes[-1]}"
            )

    if args.per_tier:
        print()
        for tier in TIERS:
            subset = {n: v for n, v in streams.items() if v[0] == tier}
            if not subset:
                continue
            chosen, exact, edges = solve(subset, args.budget)
            tag = "exact" if exact else "LOWER BOUND (budget hit)"
            print(
                f"{tier:11s} {len(subset):3d} streams, {edges:5d} violating "
                f"pairs -> max subset {len(chosen):3d}  [{tag}]"
            )
            if args.list:
                for name in chosen:
                    print(f"    {name}")

    print()
    chosen, exact, edges = solve(streams, args.budget)
    tag = "exact" if exact else "LOWER BOUND (budget hit)"
    total_pairs = len(streams) * (len(streams) - 1) // 2
    print(
        f"ALL TIERS POOLED: {len(streams)} streams, {edges}/{total_pairs} pairs "
        f"violate the gate"
    )
    print(f"  maximum compatible subset: {len(chosen)}  [{tag}]")
    if args.list:
        for name in chosen:
            print(f"    {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
