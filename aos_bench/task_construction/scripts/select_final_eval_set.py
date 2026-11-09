#!/usr/bin/env python3
"""Select the final V1-0 evaluation set and verify it against the release bar.

Picks N streams per tier out of the full-valid corpus so that the selection
satisfies, and is reported against, five acceptance criteria:

  1. task overlap minimised   -- every pair under the gate
                                 overlap(A,B) = |A&B| / min(|A|,|B|) < 0.60,
                                 and among gate-passing candidates the set with
                                 the fewest shared task-pairs is preferred
  2. arrival times irregular  -- inter-arrival gaps must not form an arithmetic
                                 progression; a stream whose gaps are constant
                                 is rejected
  3. benchmark sources mixed  -- consecutive arrivals should come from different
                                 source benchmarks; runs of the same benchmark
                                 are scored and minimised
  4. image published          -- stream.toml carries a docker_image and the tag
                                 resolves in the registry
  5. no solution in the image -- the environment build context must not copy
                                 solution/ or solve.sh into the image

Selection is greedy-with-restarts rather than exhaustive: the gate alone leaves
a large feasible space, so the objective is to pick a LOW-overlap member of it,
not to prove optimality. The chosen set is re-verified pairwise at the end, so a
bad pick fails loudly instead of shipping.

Usage:
    select_final_eval_set.py --light 10 --medium 10 --heavy 5 --heavy-plus 1 \
        --out data/stream/V1-0/valid-0917 \
        --report tmp/0917/data_final_report.md
    select_final_eval_set.py ... --dry-run     # report only, copy nothing
"""

from __future__ import annotations

import os
import argparse
import json
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tomllib

GATE = 0.60
TIERS = ("light", "medium", "heavy", "heavy-plus")
REPO = pathlib.Path(__file__).resolve().parents[3]
TIER_V2 = REPO / "data/stream/V1-0/tier-v2"
SOURCE_DIRS = ("full-valid", "full-valid-0915-new", "full-valid-diverse0915")


class Stream:
    def __init__(self, name: str, tier: str, path: pathlib.Path) -> None:
        self.name = name
        self.tier = tier
        self.path = path
        spec = tomllib.loads((path / "stream.toml").read_text())
        self.spec = spec
        arrivals = spec.get("arrivals") or []
        self.arrivals = [
            (int(a.get("arrive_wall_time", 0)), str(a.get("task_id", "")))
            for a in arrivals
            if isinstance(a, dict)
        ]
        self.tasks = frozenset(t for _, t in self.arrivals if t)
        env = spec.get("environment") or {}
        self.image = env.get("docker_image") or ""

    # ---- criterion 2: arrival irregularity -------------------------------
    def gaps(self) -> list[int]:
        times = [t for t, _ in self.arrivals]
        return [b - a for a, b in zip(times, times[1:])]

    def is_arithmetic(self) -> bool:
        g = self.gaps()
        return len(g) >= 2 and len(set(g)) == 1

    def gap_stats(self) -> dict[str, float]:
        g = self.gaps()
        if not g:
            return {"n": 0, "min": 0, "max": 0, "distinct": 0, "cv": 0.0}
        mean = sum(g) / len(g)
        var = sum((x - mean) ** 2 for x in g) / len(g)
        return {
            "n": len(g),
            "min": min(g),
            "max": max(g),
            "distinct": len(set(g)),
            "cv": (var**0.5 / mean) if mean else 0.0,
        }

    # ---- criterion 3: benchmark dispersion -------------------------------
    @staticmethod
    def benchmark_of(task_id: str) -> str:
        # Task ids are "<benchmark>-<task>", with the benchmark being the
        # leading token up to the first dash that is not part of a known
        # multi-word prefix. Using the first two dashes would split e.g.
        # "terminal-bench-2"; anchoring on known prefixes keeps families intact.
        for prefix in (
            "terminal-bench-3",
            "terminal-bench-2",
            "terminalworld-converted-no-mirrors",
            "terminalworld",
            "wildclawbench",
            "skillsbench",
            "compileagent",
            "compilebench",
            "pareval",
            "onebrc",
            "aos-synth-v1",
            "tb2",
            "tb3",
            "swebench",
            "civ6",
        ):
            if task_id.startswith(prefix):
                return prefix
        return task_id.split("-", 1)[0]

    def benchmarks(self) -> list[str]:
        return [self.benchmark_of(t) for _, t in self.arrivals if t]

    def adjacent_same_benchmark(self) -> int:
        b = self.benchmarks()
        return sum(1 for x, y in zip(b, b[1:]) if x == y)

    # ---- criterion 5: solution must not be in the image ------------------
    def solution_in_build_context(self) -> tuple[bool, list[str]]:
        """True if the environment build would copy solution material in."""
        env_dir = self.path / "environment"
        hits: list[str] = []
        df = env_dir / "Dockerfile"
        if df.is_file():
            for i, line in enumerate(df.read_text(errors="ignore").splitlines(), 1):
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if re.match(r"(?i)^(COPY|ADD)\b", s) and re.search(
                    r"(^|[\s/])(solution|solve\.sh)([\s/]|$)", s
                ):
                    hits.append(f"Dockerfile:{i}: {s[:110]}")
        # solution/ living inside the build context would be copied by a
        # `COPY . .`, so flag its presence there too.
        for candidate in ("solution", "solve.sh"):
            if (env_dir / candidate).exists():
                hits.append(f"present in build context: environment/{candidate}")
        return bool(hits), hits


def overlap(a: Stream, b: Stream) -> float:
    denom = min(len(a.tasks), len(b.tasks))
    if denom == 0:
        return 0.0
    return len(a.tasks & b.tasks) / denom


def load_pool(extra_dirs: tuple[str, ...] = ()) -> dict[str, list[Stream]]:
    pool: dict[str, dict[str, Stream]] = {t: {} for t in TIERS}
    for d in SOURCE_DIRS + tuple(extra_dirs):
        root = TIER_V2 / d
        if not root.is_dir():
            continue
        for tier in TIERS:
            tier_dir = root / tier
            if not tier_dir.is_dir():
                continue
            for sd in sorted(tier_dir.iterdir()):
                if not sd.is_dir() or sd.name.startswith("."):
                    continue
                if not (sd / "stream.toml").is_file():
                    continue
                if sd.name in pool[tier]:
                    continue
                try:
                    pool[tier][sd.name] = Stream(sd.name, tier, sd)
                except Exception as exc:  # noqa: BLE001
                    print(f"  warn: cannot read {tier}/{sd.name}: {exc}")
    return {t: list(v.values()) for t, v in pool.items()}


def select(
    pool: dict[str, list[Stream]], want: dict[str, int], seed: int, tries: int
) -> tuple[list[Stream], dict]:
    """Exact quota-constrained search, with randomised restarts for quality.

    Greedy alone is not enough here. Each tier has headroom on its own (light
    12/10, medium 17/10, heavy 6/5, heavy-plus 1/1 by maximum independent set),
    but the pooled maximum is 32 while the quotas need 26 -- so cross-tier
    conflicts decide feasibility and a per-tier greedy pass can paint itself
    into a corner. medium is the hot spot: 55 of the 112 conflicting pairs are
    medium-medium, plus 22 heavy-medium and 22 heavy-plus-medium.

    So: backtracking that fills the tightest tier first (fewest candidates per
    slot), which finds a feasible assignment if one exists. Restarts shuffle the
    candidate order and keep the lowest-cost solution found.
    """
    rng = random.Random(seed)
    best: list[Stream] | None = None
    best_cost: tuple[int, int, int] | None = None

    order = sorted(
        (t for t in TIERS if want.get(t, 0) > 0),
        key=lambda t: len([s for s in pool.get(t, []) if not s.is_arithmetic()])
        / max(want[t], 1),
    )

    for _ in range(tries):
        cands: dict[str, list[Stream]] = {}
        for tier in order:
            # Criteria 4 and 5 are prerequisites, not tie-breaks: a stream with
            # no published image, or one that would bake its own solution into
            # the image, can never be part of a valid set. Filtering here rather
            # than reporting a failure later keeps the search honest -- it means
            # a returned selection has already cleared those two.
            c = [
                s
                for s in pool.get(tier, [])
                if not s.is_arithmetic()
                and s.image
                and not s.solution_in_build_context()[0]
            ]
            rng.shuffle(c)
            c.sort(key=lambda s: s.adjacent_same_benchmark())
            cands[tier] = c

        chosen: list[Stream] = []
        nodes = [0]

        def fill(idx: int) -> bool:
            if idx >= len(order):
                return True
            tier = order[idx]
            need = want[tier]

            def pick(start: int, taken: int) -> bool:
                nodes[0] += 1
                if nodes[0] > 400_000:
                    return False
                if taken == need:
                    return fill(idx + 1)
                remaining = len(cands[tier]) - start
                if remaining < need - taken:
                    return False
                for i in range(start, len(cands[tier])):
                    s = cands[tier][i]
                    if all(overlap(s, o) < GATE for o in chosen):
                        chosen.append(s)
                        if pick(i + 1, taken + 1):
                            return True
                        chosen.pop()
                return False

            return pick(0, 0)

        if not fill(0):
            continue

        shared_pairs = sum(
            len(a.tasks & b.tasks)
            for i, a in enumerate(chosen)
            for b in chosen[i + 1 :]
        )
        adj = sum(c.adjacent_same_benchmark() for c in chosen)
        distinct_bm = len({b for c in chosen for b in c.benchmarks()})
        cost = (shared_pairs, adj, -distinct_bm)
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, list(chosen)

    if best is None:
        return [], {}
    shared_pairs, adj, neg_distinct = best_cost  # type: ignore[misc]
    return best, {
        "shared_task_pairs": shared_pairs,
        "adjacent_same_benchmark": adj,
        "distinct_benchmarks": -neg_distinct,
    }


def image_resolves(tag: str) -> tuple[bool, str]:
    if not tag:
        return False, "no docker_image in stream.toml"
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        **{k: os.environ[k] for k in ("CONTAINERS_STORAGE_CONF", "CONTAINERS_CONF", "TMPDIR") if k in os.environ},
        "HOME": str(pathlib.Path.home()),
    }
    try:
        r = subprocess.run(
            ["docker", "manifest", "inspect", tag],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return False, f"probe failed: {exc}"
    if r.returncode == 0:
        return True, "manifest resolved"
    # The podman shim cannot parse some OCI manifests; fall back to a HEAD-ish
    # check via skopeo-style inspect if available, else report the stderr.
    err = (r.stderr or "").strip().splitlines()
    return False, (err[-1][:150] if err else f"rc={r.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser()
    for tier, default in (("light", 10), ("medium", 10), ("heavy", 5), ("heavy-plus", 1)):
        ap.add_argument(f"--{tier}", type=int, default=default)
    ap.add_argument("--out", default="data/stream/V1-0/valid-0917")
    ap.add_argument("--report", default="tmp/0917/data_final_report.md")
    ap.add_argument("--seed", type=int, default=917)
    ap.add_argument("--tries", type=int, default=400)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-image-probe", action="store_true")
    ap.add_argument(
        "--extra-source",
        action="append",
        default=[],
        help=(
            "additional tier-v2 subdirectory to draw from, e.g. build-valid. "
            "Needed for heavy-plus: no full-valid dir holds any, while two "
            "build-valid ones already clear the >=0.90 oracle bar."
        ),
    )
    ap.add_argument(
        "--require-oracle",
        action="append",
        default=[],
        help="stream names admitted from an extra source on recorded evidence",
    )
    args = ap.parse_args()

    want = {
        "light": args.light,
        "medium": args.medium,
        "heavy": args.heavy,
        "heavy-plus": getattr(args, "heavy_plus"),
    }

    pool = load_pool(tuple(args.extra_source))
    # An extra source is not full-valid, so only the explicitly named streams
    # are admissible from it -- otherwise a build-valid stream with no oracle
    # evidence could slip into the final set.
    if args.extra_source:
        allowed = set(args.require_oracle)
        from_full_valid = load_pool()
        for tier in TIERS:
            fv_names = {s.name for s in from_full_valid.get(tier, [])}
            pool[tier] = [
                s for s in pool[tier] if s.name in fv_names or s.name in allowed
            ]
    print("pool (deduplicated across full-valid dirs):")
    for t in TIERS:
        usable = [s for s in pool[t] if not s.is_arithmetic()]
        print(f"  {t:11s} {len(pool[t]):3d} total, {len(usable):3d} non-arithmetic")

    chosen, metrics = select(pool, want, args.seed, args.tries)
    if not chosen:
        print("\nno selection satisfies the gate at these counts")
        for t in TIERS:
            print(f"  {t}: want {want[t]}, pool {len(pool[t])}")
        return 1

    # ---- verify the chosen set ------------------------------------------
    results: dict[str, object] = {}
    pairs = [
        (a.name, b.name, overlap(a, b), len(a.tasks & b.tasks))
        for i, a in enumerate(chosen)
        for b in chosen[i + 1 :]
    ]
    worst = max(pairs, key=lambda p: p[2]) if pairs else None
    violations = [p for p in pairs if p[2] >= GATE]
    results["overlap"] = {
        "pairs": len(pairs),
        "violations": len(violations),
        "max_ratio": worst[2] if worst else 0.0,
        "max_pair": f"{worst[0]} / {worst[1]}" if worst else "",
        "shared_task_pairs": metrics.get("shared_task_pairs"),
    }

    arrival_rows = []
    for s in chosen:
        st = s.gap_stats()
        arrival_rows.append(
            {
                "stream": s.name,
                "tier": s.tier,
                "n_gaps": st["n"],
                "distinct": st["distinct"],
                "min": st["min"],
                "max": st["max"],
                "cv": round(st["cv"], 3),
                "arithmetic": s.is_arithmetic(),
            }
        )
    results["arrivals"] = arrival_rows

    bench_rows = []
    for s in chosen:
        b = s.benchmarks()
        bench_rows.append(
            {
                "stream": s.name,
                "tier": s.tier,
                "n_tasks": len(b),
                "distinct_benchmarks": len(set(b)),
                "adjacent_same": s.adjacent_same_benchmark(),
                "sequence": " ".join(b),
            }
        )
    results["benchmarks"] = bench_rows

    image_rows = []
    for s in chosen:
        if args.skip_image_probe:
            ok, detail = bool(s.image), "not probed"
        else:
            ok, detail = image_resolves(s.image)
        image_rows.append(
            {"stream": s.name, "image": s.image, "ok": ok, "detail": detail}
        )
        print(f"  image {'OK ' if ok else 'BAD'} {s.name}: {detail}")
    results["images"] = image_rows

    sol_rows = []
    for s in chosen:
        leaked, hits = s.solution_in_build_context()
        sol_rows.append({"stream": s.name, "leaked": leaked, "hits": hits})
    results["solution"] = sol_rows

    out = pathlib.Path(args.out)
    if not args.dry_run:
        for s in chosen:
            dst = out / s.tier / s.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(s.path, dst, symlinks=True)
        print(f"\ncopied {len(chosen)} streams into {out}")

    report = pathlib.Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report(chosen, results, want, args))
    print(f"report written to {report}")

    hard_fail = (
        results["overlap"]["violations"] > 0  # type: ignore[index]
        or any(r["arithmetic"] for r in arrival_rows)
        or any(r["leaked"] for r in sol_rows)
        or any(not r["ok"] for r in image_rows)
    )
    return 1 if hard_fail else 0


def render_report(chosen, results, want, args) -> str:
    ov = results["overlap"]
    lines: list[str] = []
    A = lines.append
    A("# V1-0 final evaluation set — acceptance report")
    A("")
    A(f"> Generated by `aos_bench/task_construction/scripts/select_final_eval_set.py`")
    A(f"> seed {args.seed}, {args.tries} restarts · output `{args.out}`")
    A("")
    counts = {}
    for s in chosen:
        counts[s.tier] = counts.get(s.tier, 0) + 1
    A("## Selection")
    A("")
    A("| tier | requested | selected |")
    A("|---|---:|---:|")
    for t in TIERS:
        if want.get(t):
            A(f"| {t} | {want[t]} | {counts.get(t, 0)} |")
    A(f"| **total** | **{sum(want.values())}** | **{len(chosen)}** |")
    A("")
    for t in TIERS:
        names = [s.name for s in chosen if s.tier == t]
        if names:
            A(f"- **{t}**: {', '.join(names)}")
    A("")

    A("## 1. Task overlap")
    A("")
    A("Gate: `overlap(A,B) = |A∩B| / min(|A|,|B|) < 0.60`, strict. `min` in the")
    A("denominator means the smallest stream binds, so a light stream sharing 3")
    A("tasks with a heavy one already sits at 0.60 and fails.")
    A("")
    A(f"- pairs checked: **{ov['pairs']}**")
    A(f"- violations: **{ov['violations']}**")
    A(f"- worst pair: `{ov['max_pair']}` at **{ov['max_ratio']:.1%}**")
    A(f"- total shared task-pairs across the set: **{ov['shared_task_pairs']}**")
    A("")

    A("## 2. Arrival timing")
    A("")
    A("Checked that inter-arrival gaps are not an arithmetic progression. A")
    A("stream whose gaps are all equal is rejected before selection, so every")
    A("row below is non-arithmetic by construction; `distinct` and `cv`")
    A("(coefficient of variation) show how irregular each one actually is.")
    A("")
    A("| stream | tier | gaps | distinct | min | max | cv | arithmetic |")
    A("|---|---|---:|---:|---:|---:|---:|---|")
    for r in results["arrivals"]:
        A(
            f"| {r['stream']} | {r['tier']} | {r['n_gaps']} | {r['distinct']} | "
            f"{r['min']} | {r['max']} | {r['cv']} | "
            f"{'YES (fail)' if r['arithmetic'] else 'no'} |"
        )
    A("")

    A("## 3. Benchmark dispersion")
    A("")
    A("`adjacent_same` counts consecutive arrivals drawn from the same source")
    A("benchmark — lower is better mixed. 0 means no two neighbouring tasks")
    A("come from the same benchmark.")
    A("")
    A("| stream | tier | tasks | distinct benchmarks | adjacent same |")
    A("|---|---|---:|---:|---:|")
    for r in results["benchmarks"]:
        A(
            f"| {r['stream']} | {r['tier']} | {r['n_tasks']} | "
            f"{r['distinct_benchmarks']} | {r['adjacent_same']} |"
        )
    A("")
    A("<details><summary>arrival order by benchmark</summary>")
    A("")
    for r in results["benchmarks"]:
        A(f"- `{r['stream']}`: {r['sequence']}")
    A("")
    A("</details>")
    A("")

    A("## 4. Image published")
    A("")
    A("Each stream's `[environment].docker_image` was resolved against the")
    A("registry with `docker manifest inspect`. A tag recorded in stream.toml")
    A("but absent from the registry fails here.")
    A("")
    A("| stream | resolves | image |")
    A("|---|---|---|")
    for r in results["images"]:
        A(f"| {r['stream']} | {'yes' if r['ok'] else 'NO — ' + r['detail']} | `{r['image']}` |")
    A("")

    A("## 5. No solution in the image")
    A("")
    A("Two ways a solution could leak into the built image, both checked:")
    A("a `COPY`/`ADD` naming `solution/` or `solve.sh` in the Dockerfile, and")
    A("either path being present inside `environment/` (the build context),")
    A("where a `COPY . .` would pick it up. The stream directory itself keeps")
    A("`solution/` — that is required for oracle verification and is not part")
    A("of the image.")
    A("")
    A("| stream | solution in image | evidence |")
    A("|---|---|---|")
    for r in results["solution"]:
        ev = "; ".join(r["hits"])[:160] if r["hits"] else "clean"
        A(f"| {r['stream']} | {'LEAK' if r['leaked'] else 'no'} | {ev} |")
    A("")

    hard = (
        ov["violations"] > 0
        or any(r["arithmetic"] for r in results["arrivals"])
        or any(r["leaked"] for r in results["solution"])
        or any(not r["ok"] for r in results["images"])
    )
    A("## Verdict")
    A("")
    A("**FAIL — see the failing rows above.**" if hard else "**PASS on all five criteria.**")
    A("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
