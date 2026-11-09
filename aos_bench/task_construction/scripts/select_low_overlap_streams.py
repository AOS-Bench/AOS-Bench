#!/usr/bin/env python3
"""Select medium/heavy/heavy-plus task sets under the corpus-wide overlap gate.

Gate:  overlap(A,B) = |A & B| / min(|A|,|B|) < 0.60   (strict; equality fails)

Generalises scripts/local/heavy-synth-0910/select_heavy.py from heavy-only to
any tier, because the same two properties hold for all of them:

  * the generator's pair-penalty sampler (v1_low_overlap_scan.py) only
    de-duplicates among candidates of its own run and never reads build-valid/,
    so it cannot honour a gate defined against the existing corpus;
  * the binding peer is whichever stream has the SMALLEST task count, since
    min(|A|,|B|) sits in the denominator.  A 20-task heavy stream sharing 3
    tasks with a 5-task light stream is already at 0.60 and fails.

Per-peer cap is derived, not guessed: the largest k with k/min(|A|,|B|) < 0.6.

Feasibility is checked before searching.  With a usable pool of P tasks, two
streams of size |A| and |B| must share at least |A|+|B|-P tasks (pigeonhole);
if that floor exceeds the cap, NO selection exists and search would just burn
CPU before returning nothing.  That is a proof about the pool, not a timeout.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import random
import re
import sys

# parents[3] because this file lives at
# aos_bench/task_construction/scripts/, matching generate_v1_0.py and
# v1_low_overlap_scan.py alongside it. It was parents[2] while the script sat in
# scripts/local/; moving it without changing this would have silently resolved
# REPO to aos_bench/ and made every data path below miss.
REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from aos_bench.task_construction.scripts.generate_v1_0 import (  # noqa: E402
    DEFAULT_BLACKLIST,
    _dockerfile_is_multistage,
    is_policy_excluded,
    load_task_blacklist,
)

BV = REPO / "data/stream/V1-0/tier-v2/build-valid"
GATE = 0.60

# Tier composition. heavy-plus is intentionally absent from v1_spec's
# STREAM_TIER_MIX -- the 4 existing heavy-plus streams were assembled before the
# no-duplicates policy and contain repeated tasks (`tb2-tune-mjcf-2..5`), which
# the current generator refuses to emit.  Its mix here is therefore a request,
# and --allow-l1 decides whether it can be met at all (see feasibility()).
TIER_MIX = {
    "medium": {"L2": 10, "L3": 0},
    "heavy": {"L2": 10, "L3": 10},
    "heavy-plus": {"L2": 20, "L3": 10},
}

# Documented defects, not overlap failures.  Keeping these in the pool spends a
# 45-90 minute build to re-learn a known-bad bit.
HARD_EXCLUDE = {
    # coursier GraalVM binary cannot reach maven from this build host.
    "skillsbench-python-scala-translation",
    # setup_challenge.py hardcodes /app; builds fine, can never reach reward 1.0.
    "tb3-shadow-relay",
}
PROBE_FAILED = REPO / "scripts/local/heavy-synth-0910/excluded-tasks.txt"

# Two tasks vendor github.com/ozkl/doomgeneric into the SAME path; the merged
# block only builds when the merger happens to emit them in one specific order.
FORBIDDEN_PAIRS = [("tb2-make-doom-for-mips", "tb2-make-mips-interpreter")]

# Tasks recovered or newly admitted on 2026-09-15: WildClaw (judge provisioned),
# security (all L1, so light-tier only), the five tb3 sources pulled from
# upstream at pin 10082e59, the six repaired Dockerfiles, and the band admitted
# by lowering the L2 floor to 0.009. None of these has any build-valid coverage,
# so they are drafted ahead of already-proven tasks -- a stream containing only
# long-established tasks teaches us nothing new about buildability.
#
# Implemented as a usage offset rather than a hard requirement: the overlap gate
# still has absolute priority, so if a priority task cannot fit a compliant set
# it is simply not used.
PRIORITY_BONUS = 3
PRIORITY_TASK_IDS: frozenset[str] = frozenset({
    # repaired Dockerfiles
    "skillsbench-gravitational-wave-detection",
    "skillsbench-mars-clouds-clustering",
    "skillsbench-video-tutorial-indexer",
    "tb3-react-lead-form",
    "tb3-pretrain-shard-corruption",
    "frontier-swe-notebook-compression",
    # pulled from upstream at the recorded pin
    "tb3-vpp-loss-divergence",
    # un-blacklisted once a vision judge existed
    "wildclawbench-02_Code_Intelligence_task_3_jigsaw_puzzle_zh",
    "wildclawbench-02_Code_Intelligence_task_5_jigsaw_puzzle_hard_zh",
})


def cap_for(peer_size: int, own_size: int) -> int:
    """Largest k with k / min(own, peer) < 0.60."""
    m = min(peer_size, own_size)
    k = m
    while m and k / m >= GATE:
        k -= 1
    return max(k, 0)


def stream_tasks(stream_dir: pathlib.Path) -> set[str]:
    text = (stream_dir / "stream.toml").read_text()
    return set(re.findall(r'^task_id\s*=\s*"([^"]+)"', text, re.M))


def load_corpus(extra_roots: list[pathlib.Path]) -> dict[str, set[str]]:
    """Every stream already on disk that a new stream must stay compliant with.

    Hidden/backup directories (`.name.pre-strict.<nanos>`) are snapshots left by
    earlier repair passes, not corpus members; counting them would impose
    constraints from streams that no longer exist.
    """
    out: dict[str, set[str]] = {}
    roots = [BV / t for t in ("light", "medium", "heavy", "heavy-plus")]
    roots += list(extra_roots)
    for root in roots:
        if not root.is_dir():
            continue
        for stream_dir in sorted(root.iterdir()):
            if stream_dir.name.startswith("."):
                continue
            if (stream_dir / "stream.toml").is_file():
                out[stream_dir.name] = stream_tasks(stream_dir)
    return out


def usable_source(task_dir: pathlib.Path) -> bool:
    needed = ("task.toml", "instruction.md", "environment/Dockerfile")
    if not all((task_dir / name).is_file() for name in needed):
        return False
    if (task_dir / "environment/docker-compose.yaml").exists():
        return False
    # Reuse the generator's multi-stage test rather than re-implementing it. A
    # local `line.strip().startswith("from ")` copy also matched INDENTED Python
    # `from x import y` inside a RUN heredoc, which misread the single-stage
    # tb3-vpp-loss-divergence as multi-stage and dropped that L3 from the pool.
    # Same bug was already fixed in generate_v1_0.py; duplicating the logic is
    # what let it come back.
    return not _dockerfile_is_multistage(task_dir)


def load_pool(summary: pathlib.Path, allow_l1: bool) -> dict[str, list[str]]:
    rows = json.loads(summary.read_text())["tasks"]
    blacklist = load_task_blacklist(DEFAULT_BLACKLIST)
    # Parse line-by-line and strip comments. A bare .split() would tokenise the
    # explanatory prose in that file into "task IDs": the 2026-09-15 revision
    # documents each removal by name, so .split() silently re-excluded the very
    # tasks that had just been repaired (measured: 4 of them, including 2 L3).
    probe_failed: set[str] = set()
    if PROBE_FAILED.is_file():
        for line in PROBE_FAILED.read_text().splitlines():
            entry = line.split("#", 1)[0].strip()
            if entry:
                probe_failed.add(entry)
    wanted = {"L2", "L3"} | ({"L1"} if allow_l1 else set())
    pool: dict[str, list[str]] = collections.defaultdict(list)
    dropped: collections.Counter[str] = collections.Counter()
    for row in rows:
        tier, task_id = row.get("v1_tier"), row["task_id"]
        if tier not in wanted:
            continue
        if is_policy_excluded(task_id):
            dropped["policy"] += 1
        elif task_id in blacklist:
            dropped["blacklist"] += 1
        elif task_id in probe_failed:
            dropped["probe-failed"] += 1
        elif task_id in HARD_EXCLUDE:
            dropped["hard-defect"] += 1
        elif not usable_source(pathlib.Path(row["task_dir"])):
            dropped["unbuildable-source"] += 1
        else:
            pool[tier].append(task_id)
            continue
    for tier in pool:
        pool[tier].sort()
    print(f"pool dropped: {dict(dropped)}", flush=True)
    return pool


def feasibility(
    bucket: str, usable: set[str], corpus: dict[str, set[str]], want: int
) -> list[str]:
    """Pigeonhole check: report peers this bucket provably cannot comply with.

    A new stream draws its ``size`` tasks from ``usable``.  A peer B forces
    sharing only through the part of B that lives in that same pool, so the
    floor is ``size + |B & usable| - |usable|`` -- NOT ``size + |B| - |usable|``.
    The difference is not academic here: the four heavy-plus streams each carry
    16 tasks that are not in the pool at all (duplicate ``-2``/``-3`` variants
    from before the no-duplicates policy), and the cruder formula reports them
    as blocking when they are not.
    """
    size = sum(TIER_MIX[bucket].values())
    pool_size = len(usable)
    blocking = []
    checks = [
        (name, len(tasks), len(tasks & usable)) for name, tasks in corpus.items()
    ]
    # New streams must also be mutually compliant, and they are drawn wholly
    # from the pool -- so for want>1 this binds even against an empty corpus.
    if want > 1:
        checks.append((f"<another {bucket}>", size, size))
    for name, peer_size, shared_pool in checks:
        floor = size + shared_pool - pool_size
        cap = cap_for(peer_size, size)
        if floor > cap:
            blocking.append(
                f"{name}: |A|={size} |B|={peer_size} (|B∩pool|={shared_pool}) "
                f"pool={pool_size} -> must share >={floor}, cap={cap}"
            )
    return blocking


def violations(candidate: set[str], peers: dict[str, tuple[set[str], int]]) -> int:
    total = 0
    for tasks, cap in peers.values():
        shared = len(candidate & tasks)
        if shared > cap:
            total += shared - cap
    for left, right in FORBIDDEN_PAIRS:
        if left in candidate and right in candidate:
            total += 5
    return total


def make_one(
    mix: dict[str, int],
    pool: dict[str, list[str]],
    peers: dict[str, tuple[set[str], int]],
    rng: random.Random,
    usage: collections.Counter[str],
    tries: int,
    iters: int,
    required: dict[str, list[str]] | None = None,
) -> dict[str, set[str]] | None:
    """Random restart + swap-repair, keeping each tier's count exact.

    ``required`` pins specific task IDs into the result, per tier.  A plain
    usage bonus is NOT enough for this: measured on the 2026-09-14 wave, 7 of 9
    prioritised tasks were never selected at all, because the bonus only tilts
    the choice among already-feasible sets and the overlap gate always wins.
    Pinning seeds them and excludes them from the swap-repair candidates, so a
    stream either contains them or the search reports infeasible -- which is the
    honest outcome, rather than silently dropping them.
    """
    required = required or {}
    best_violations = None
    for _ in range(tries):
        sides: dict[str, set[str]] = {}
        infeasible = False
        for tier, want in mix.items():
            if not want:
                continue
            pinned = [t for t in required.get(tier, []) if t in pool[tier]][:want]
            if len(pinned) > want:
                infeasible = True
                break
            # Bias the rest toward under-used tasks so the corpus stays
            # balanced rather than exhausting a few tasks on early streams.
            rest = [t for t in pool[tier] if t not in pinned]
            ordered = sorted(
                rest,
                key=lambda t: (usage[t] - (PRIORITY_BONUS if t in PRIORITY_TASK_IDS else 0), rng.random()),
            )
            need = want - len(pinned)
            window = max(need + 6, len(rest) // 2)
            if need > len(rest):
                infeasible = True
                break
            sides[tier] = set(pinned) | set(rng.sample(ordered[:window], need))
        if infeasible:
            return None
        current = set().union(*sides.values())
        score = violations(current, peers)
        for _ in range(iters):
            if score == 0:
                return sides
            tier = rng.choice([t for t, w in mix.items() if w])
            side = sides[tier]
            # Never swap out a pinned task.
            pinned_here = set(required.get(tier, []))
            droppable = sorted(side - pinned_here)
            if not droppable:
                continue
            drop = rng.choice(droppable)
            options = [t for t in pool[tier] if t not in side]
            if not options:
                continue
            add = rng.choice(options)
            trial = (current - {drop}) | {add}
            trial_score = violations(trial, peers)
            if trial_score <= score:
                current, score = trial, trial_score
                side.discard(drop)
                side.add(add)
        if score == 0:
            return sides
        if best_violations is None or score < best_violations:
            best_violations = score
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True, choices=sorted(TIER_MIX))
    ap.add_argument("--want", type=int, required=True)
    ap.add_argument("--summary", type=pathlib.Path,
                    default=REPO / "tmp/v1_0_heavy_pool.pathfixed.json")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--seed", type=int, default=20260913)
    ap.add_argument("--tries", type=int, default=400)
    ap.add_argument("--iters", type=int, default=6000)
    ap.add_argument("--allow-l1", action="store_true",
                    help="admit L1 tasks (needed only where L2+L3 cannot fill the tier)")
    ap.add_argument("--extra-corpus", type=pathlib.Path, nargs="*", default=[],
                    help="additional roots of streams new candidates must not clash with. "
                         "REQUIRED when another tier's candidates were selected in a "
                         "separate run: two runs that cannot see each other will each be "
                         "internally clean and still violate the gate against each other.")
    ap.add_argument("--name-prefix", default=None)
    ap.add_argument("--require", nargs="*", default=[],
                    help="Task IDs that must be placed, one per stream per tier. "
                         "Use for newly repaired tasks with no build coverage yet: "
                         "a usage bonus alone does not get them selected.")
    ap.add_argument("--start-index", type=int, default=301)
    args = ap.parse_args()

    corpus = load_corpus(list(args.extra_corpus))
    pool = load_pool(args.summary, args.allow_l1)
    mix = TIER_MIX[args.bucket]
    size = sum(mix.values())

    print(f"corpus streams={len(corpus)}  "
          f"pool={ {t: len(v) for t, v in sorted(pool.items())} }", flush=True)

    for tier, want in mix.items():
        if want and len(pool.get(tier, [])) < want:
            print(f"FATAL: {args.bucket} needs {want} {tier} tasks, pool has "
                  f"{len(pool.get(tier, []))}", file=sys.stderr)
            return 2

    usable = {t for tier, w in mix.items() if w for t in pool[tier]}
    blocking = feasibility(args.bucket, usable, corpus, args.want)
    if blocking:
        print(f"INFEASIBLE: {args.bucket} cannot satisfy the gate from this pool "
              f"({len(usable)} usable tasks). Blocking peers:", file=sys.stderr)
        for line in blocking[:10]:
            print(f"  {line}", file=sys.stderr)
        if len(blocking) > 10:
            print(f"  ... and {len(blocking) - 10} more", file=sys.stderr)
        return 3

    peers = {
        name: (tasks, cap_for(len(tasks), size)) for name, tasks in corpus.items()
    }
    rng = random.Random(args.seed)
    usage: collections.Counter[str] = collections.Counter()
    prefix = args.name_prefix or f"{args.bucket}-{size}-scan-"

    # Tasks that must appear in the FIRST streams produced, distributed one
    # cohort per stream. A usage bonus alone proved insufficient (see make_one).
    required_queue: list[str] = list(args.require or [])
    by_tier: dict[str, list[str]] = collections.defaultdict(list)
    for task_id in required_queue:
        for tier in ("L2", "L3"):
            if task_id in pool.get(tier, []):
                by_tier[tier].append(task_id)
                break
        else:
            print(f"  warning: --require {task_id} is not in the usable pool; ignored",
                  file=sys.stderr)

    selection: dict[str, list[str]] = {}
    for index in range(args.want):
        # Hand each stream at most one required task per tier, so the cohort is
        # spread across streams rather than crammed into the first one (which
        # would almost certainly violate the overlap gate).
        this_round = {
            tier: [ids.pop(0)] for tier, ids in by_tier.items() if ids
        }
        sides = make_one(mix, pool, peers, rng, usage, args.tries, args.iters,
                         required=this_round)
        if sides is None and this_round:
            # A pinned task that cannot fit is worth reporting, not hiding.
            print(f"  note: could not place required {sum(this_round.values(), [])} "
                  f"in stream #{index + 1}; retrying without the pin", flush=True)
            sides = make_one(mix, pool, peers, rng, usage, args.tries, args.iters)
        if sides is None:
            print(f"  stopped after {len(selection)}: no feasible set remains",
                  flush=True)
            break
        tasks = set().union(*sides.values())
        name = f"{prefix}{args.start_index + index:03d}"
        peers[name] = (tasks, cap_for(size, size))
        selection[name] = sorted(tasks)
        usage.update(tasks)
        worst, worst_peer = max(
            (len(tasks & other) / min(len(tasks), len(other)), other_name)
            for other_name, (other, _) in peers.items()
            if other_name != name
        )
        print(f"  {name}: worst overlap {worst:.3f} vs {worst_peer}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)

    # Re-verify the finished selection from scratch rather than trusting the
    # incremental search.  The search only ever sees the peers present in
    # `peers`, so anything it could not see -- most importantly a sibling tier
    # selected in a separate run without --extra-corpus -- is invisible to it
    # and would otherwise be discovered only after assembly.
    final_peers = {name: tasks for name, tasks in corpus.items()}
    final_peers.update({name: set(tasks) for name, tasks in selection.items()})

    # Auto-discover sibling candidate directories under tier-v2 and fold them in
    # even when --extra-corpus omitted them.  Forgetting that flag produced an
    # overlap=1.000 subset pair TWICE (2026-09-14 and -09-16): a medium selected
    # in one run became a strict subset of a heavy selected in another, and the
    # search could not see it because each run only knew its own output. Relying
    # on the operator to pass the right --extra-corpus is evidently not enough.
    tier_root = BV.parent
    for cand_dir in sorted(tier_root.glob("candidates-*/*")):
        if not cand_dir.is_dir() or cand_dir.name.startswith("."):
            continue
        for stream_dir in sorted(cand_dir.iterdir()):
            if stream_dir.name.startswith(".") or stream_dir.name in selection:
                continue
            toml = stream_dir / "stream.toml"
            if toml.is_file():
                final_peers.setdefault(stream_dir.name, stream_tasks(stream_dir))

    offences = []
    for name, tasks in selection.items():
        chosen = set(tasks)
        for peer, peer_tasks in final_peers.items():
            if peer == name:
                continue
            shared = len(chosen & peer_tasks)
            if shared > cap_for(len(peer_tasks), len(chosen)):
                ratio = shared / min(len(chosen), len(peer_tasks))
                offences.append(f"{name} x {peer} = {ratio:.3f} ({shared} shared)")
    if offences:
        print(f"SELF-CHECK FAILED: {len(offences)} gate violations in the "
              f"selection; refusing to write {args.out}", file=sys.stderr)
        for line in offences[:15]:
            print(f"  {line}", file=sys.stderr)
        return 4

    args.out.write_text(json.dumps(selection, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(selection)} selections -> {args.out}")
    return 0 if selection else 1


if __name__ == "__main__":
    raise SystemExit(main())
