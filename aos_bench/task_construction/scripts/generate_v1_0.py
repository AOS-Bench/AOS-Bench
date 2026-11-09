"""Generate V1-0 streams from the audited two-trial task pool.

V1-0 tier-v2 composition (updated 2026-07-27):

  light  × 20  = 5 L2
  medium × 10  = 10 L2
  heavy  × 10  = 10 L2 + 10 L3

Tier scheme = tradeoff of wall_time × cpu_util:

  score = min(wall_seconds / 3600, 1) × min(cpu_utilization_ratio, 1)

  L3:  score ≥ 0.10
  L2:  0.02 ≤ score < 0.10
  L1:  < 0.02   (excluded from V1-0)

Sampling — deterministic per stream seed:

  1. load the two-trial mean score and tier → L2 / L3 / skip
  2. per bucket (light/medium/heavy), assemble N streams via
       stream_i = sample-without-replacement(L2, k_L2) + sample(L3, k_L3)
     with base_seed=42 and per-stream seed offset.
  3. arrival timestamps: cumulative `max(0, round(rng.gauss(60, 30)))`
     (identical policy to V0-3 migrate; ADR-002 removed deadlines so
      only arrive_wall_time is emitted).
  4. env: cpus=4, memory_mb=8192, allow_internet=True, max_steps=360.
  5. call `assemble()` from aos_bench.task_construction.assemble.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path

from aos_bench.task_construction.assemble.assemble import (
    StreamSpec,
    TaskRef,
    assemble,
)
from aos_bench.task_construction.v1_spec import (
    CANONICAL_GENERATION_COUNTS,
    STREAM_TIER_MIX,
    V1_TIER_SCHEMA_VERSION,
    tier_v2_root,
    validate_agent_pack_lock,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LEGACY_SUMMARY = Path(
    str(Path.home() / "aos_logs/exp_0712-compute-density/dataset_summary.json")
)
DEFAULT_SUMMARY = (
    _REPO_ROOT
    / "aos_bench"
    / "task_construction"
    / "manifests"
    / "v1_0_final_task_pool.json"
)
DEFAULT_BLACKLIST = (
    _REPO_ROOT
    / "aos_bench"
    / "task_construction"
    / "manifests"
    / "v1_0_merged_stream_blacklist.txt"
)
DEFAULT_SUPPLEMENTAL_SUMMARY = (
    _REPO_ROOT
    / "aos_bench"
    / "task_construction"
    / "manifests"
    / "v1_0_external_task_scores.json"
)
DEFAULT_OUT_DIR = tier_v2_root(_REPO_ROOT) / "candidates" / "canonical"
DEFAULT_HARBOR_DIR = _REPO_ROOT / "data" / "harbor"
DEFAULT_AGENT_PACK_LOCK = _REPO_ROOT / "images" / "agents" / "agents.lock.toml"

# Non-repo pools live under tmp/datasets/
POOL_SOURCE_ROOTS = {
    "TB2":            _REPO_ROOT / "data" / "harbor" / "terminal-bench-2",
    "TB3":            _REPO_ROOT / "tmp" / "datasets" / "agent_benchmarks" / "tb3" / "tasks",
    "FSWE":           _REPO_ROOT / "tmp" / "datasets" / "agent_benchmarks" / "frontier-swe" / "tasks",
    "aos-synth-v1":   _REPO_ROOT / "data" / "harbor" / "aos-synth-v1",
    "CompileAgent":   _REPO_ROOT / "data" / "harbor" / "compileagent",
    "CompileBench":   _REPO_ROOT / "data" / "harbor" / "compilebench",
    "LLVMFix":        _REPO_ROOT / "data" / "harbor" / "llvmfix",
    "OneBRC":         _REPO_ROOT / "data" / "harbor" / "onebrc",
    "ParEval":        _REPO_ROOT / "data" / "harbor" / "pareval",
    "TerminalWorld":  _REPO_ROOT / "data" / "harbor" / "terminalworld-converted-no-mirrors",
    "skillsbench":    _REPO_ROOT / "data" / "harbor" / "skillsbench" / "tasks",
    "wildclawbench":  _REPO_ROOT / "data" / "harbor" / "wildclawbench-converted",
}

# Bucket composition — 3x overproduction so we can filter by oracle-pass later
BUCKETS = {
    bucket: {
        "n_streams": CANONICAL_GENERATION_COUNTS[bucket],
        "k_L2": mix["L2"],
        "k_L3": mix["L3"],
    }
    for bucket, mix in STREAM_TIER_MIX.items()
}

_ARRIVAL_MEAN_SEC = 60
_ARRIVAL_STD_SEC = 30

# Whole task families excluded from V1-0 stream sampling, enforced at generation
# time so the tasks never reach a Docker build.  This is a policy exclusion, not
# a per-task defect list -- unlike v1_0_merged_stream_blacklist.txt, which names
# individual tasks that failed.
#
# 2026-09-15: the WildClawBench family exclusion was LIFTED.  It existed because
# the LLM-judge graders scored 0 without a key, which is indistinguishable from
# a wrong answer.  A judge is now provisioned and verified end to end:
#
#     text   : deepseek-v4-flash   (OpenAI-compatible Chat Completions)
#     vision : gemini-3.5-flash    (deepseek-v4-flash rejects image_url)
#     endpoint and key: supplied through OPENROUTER_BASE_URL / OPENROUTER_API_KEY
#
# The oracle-sweep launcher defaults to exactly these and supplies
# OPENROUTER_API_KEY / OPENROUTER_BASE_URL / JUDGE_MODEL /
# AOS_JUDGE_VISION_MODEL to both the verifier and the Oracle agent under
# --enable-wildclaw-judge.  9 of the 10 L2/L3 WildClaw graders send image_url,
# so the vision model is required, not optional.
#
# The security pool exclusion also no longer applies: all 8 tasks are L1
# (max mean_v1_score 0.0067 vs the L2 floor of 0.02), so they never enter
# medium/heavy sampling anyway, and excluding them only removed a task family
# from light-tier diversity.
#
# Keeping this predicate (now empty by default) rather than deleting it: the
# call sites in load_pool() and v1_low_overlap_scan.py record WHY a task was
# dropped, and a future family exclusion should reuse that reporting path.
_SECURITY_TASK_IDS: frozenset[str] = frozenset()
_EXCLUDED_POOL_PREFIXES: tuple[str, ...] = ()

# Retained for provenance and for tests that assert the lift is deliberate.
_LIFTED_SECURITY_TASK_IDS = frozenset({
    "break-filter-js-from-html",
    "crack-7z-hash",
    "filter-js-from-html",
    "fix-code-vulnerability",
    "openssl-selfsigned-cert",
    "password-recovery",
    "sanitize-git-repo",
    "vulnerable-secret",
})


def is_policy_excluded(task_id: str) -> bool:
    """True when a task belongs to a family barred from V1-0 streams.

    Pool IDs carry a ``tb2-`` prefix for terminal-bench-2 tasks while
    tb2-sec.toml lists the bare names, so both spellings are matched. Numbered
    assembly variants (``<id>-2``) are covered too, since a duplicate arrival is
    renamed rather than given a new source.

    Both former exclusions (WildClawBench, security) are lifted, so this
    currently returns False for every task.  The logic is kept intact so that
    re-adding a family is a one-line change to the constants above.
    """
    if _EXCLUDED_POOL_PREFIXES and task_id.startswith(_EXCLUDED_POOL_PREFIXES):
        return True
    bare = task_id.removeprefix("tb2-")
    if bare in _SECURITY_TASK_IDS:
        return True
    base, _, suffix = bare.rpartition("-")
    return bool(suffix.isdigit() and base in _SECURITY_TASK_IDS)


def load_task_blacklist(path: Path | None) -> set[str]:
    """Read exact task IDs, allowing blank lines and inline comments."""
    if path is None or not path.is_file():
        return set()
    return {
        line.split("#", 1)[0].strip()
        for line in path.read_text().splitlines()
        if line.split("#", 1)[0].strip()
    }


# --------------------------------------------------------------------------- #
# Tier classification
# --------------------------------------------------------------------------- #

@dataclass
class TaskEntry:
    task_id: str
    pool: str
    task_dir: Path
    wall_s: float
    cpu_util_ratio: float
    cpu_time_s: float
    reward: float | None
    score: float
    tier: str


def _score(wall, util):
    if wall is None or util is None:
        return 0.0
    wn = min(max(wall, 0) / 3600.0, 1.0)
    un = min(max(util, 0), 1.0)
    return round(wn * un, 4)


# Tier boundaries. ADR-006 set the L2 floor at 0.02, which admitted only 59
# buildable tasks on this host -- not enough to synthesise a diverse corpus
# (heavy needs 10 L3 per stream, and every stream pair must stay under the 0.60
# overlap gate, so pool size is the binding constraint).
#
# 2026-09-15: the L2 floor is lowered 0.02 -> 0.009, taking the usable pool from
# 59 to 85. This is a deliberate, measured relaxation:
#
#   floor   usable tasks
#   0.020   59   (ADR-006 original)
#   0.012   75
#   0.010   80   (exactly at target, no margin)
#   0.009   85   (chosen: clears 80 with headroom for build failures)
#   0.008   87
#
# What the newly admitted band actually looks like, versus the original set:
#
#                      count  median wall  median cpu_util
#   [0.009, 0.020)       26        ~700s        ~0.07
#   >= 0.020 (original)  59       ~1921s        ~0.21
#
# So these are genuinely lighter tasks -- roughly a third of the wall time and a
# third of the CPU utilisation. They are NOT equivalent to the original L2s, and
# the L3 floor is deliberately left at 0.10 so the heavy tier keeps its
# compute-intensive character. The justification is that AOS-Bench's primary
# axis is arrival density and interruption response, not CPU saturation
# (ADR-005): a shorter task still occupies a scheduling slot, still arrives on a
# clock, and still has to be sequenced against its peers. Pool diversity buys
# more signal on that axis than per-task CPU weight does.
#
# Override with --l2-floor to re-measure at a different cut.
L2_SCORE_FLOOR_DEFAULT = 0.009
L3_SCORE_FLOOR = 0.10


def _tier(score, l2_floor: float = L2_SCORE_FLOOR_DEFAULT):
    if score >= L3_SCORE_FLOOR: return "L3"
    if score >= l2_floor: return "L2"
    return "L1"


import re as _re

# A Dockerfile instruction must begin at column 0 (leading whitespace is not
# valid for an instruction keyword).  Matching a stripped line instead would
# also count Python `from x import y` lines that appear INDENTED inside a
# heredoc -- e.g. terminal-bench-3/vpp-loss-divergence embeds
# "            from megatron.core import parallel_state" in a RUN heredoc, and a
# stripped match misreads that single-stage task as multi-stage and drops it
# from the pool entirely.
_FROM_INSTRUCTION_RE = _re.compile(r"(?im)^FROM[ \t]+\S")


def _from_instruction_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if _FROM_INSTRUCTION_RE.match(line)
    ]


def _dockerfile_from(task_dir: Path) -> str:
    df = task_dir / "environment" / "Dockerfile"
    if not df.exists():
        return ""
    for line in _from_instruction_lines(df.read_text()):
        return line.split(None, 2)[1].split(" AS")[0].strip()
    return ""


def _dockerfile_is_multistage(task_dir: Path) -> bool:
    """assemble()'s dockerfile_merge rejects multi-stage Dockerfiles.
    Pre-scan and skip those tasks so we don't fail mid-loop."""
    df = task_dir / "environment" / "Dockerfile"
    if not df.exists():
        return True  # no Dockerfile → can't assemble anyway
    return len(_from_instruction_lines(df.read_text())) >= 2


def _has_compose(task_dir: Path) -> bool:
    """Multi-service compose tasks aren't stream-mergeable either."""
    return (task_dir / "environment" / "docker-compose.yaml").exists()


def load_pool(summary_path: Path) -> list[TaskEntry]:
    """Read the final pool or a legacy dataset summary.

    The default final pool already contains the mean of two successful 4C/8G
    GLM-5.2 trials. Legacy ``dataset_summary.json`` inputs remain supported for
    reproducibility. Tasks whose Dockerfile is multi-stage or uses Compose are
    filtered here because the current stream assembler cannot merge them.
    """
    data = json.loads(summary_path.read_text())
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        out = []
        skipped = {"multistage": 0, "compose": 0, "missing": 0, "policy_excluded": 0}
        for item in data["tasks"]:
            if is_policy_excluded(item["task_id"]):
                skipped["policy_excluded"] += 1
                continue
            task_dir = Path(item["task_dir"])
            if not (task_dir / "task.toml").is_file():
                skipped["missing"] += 1
                continue
            if _has_compose(task_dir):
                skipped["compose"] += 1
                continue
            if _dockerfile_is_multistage(task_dir):
                skipped["multistage"] += 1
                continue
            out.append(TaskEntry(
                task_id=item["task_id"],
                pool=item["benchmark"],
                task_dir=task_dir,
                wall_s=item["mean_wall_seconds"],
                cpu_util_ratio=item["mean_cpu_utilization_ratio"],
                cpu_time_s=item["mean_cpu_time_cumulative_s"],
                reward=item.get("mean_reward"),
                score=item["mean_v1_score"],
                tier=item["v1_tier"],
            ))
        print(f"skipped final-pool tasks unsupported by assembler: {skipped}")
        return out

    if (
        summary_path.resolve() == DEFAULT_LEGACY_SUMMARY.resolve()
        and DEFAULT_SUPPLEMENTAL_SUMMARY.is_file()
    ):
        supplemental = json.loads(DEFAULT_SUPPLEMENTAL_SUMMARY.read_text())
        # Prefer a freshly measured entry from the primary summary if a later
        # sweep has already promoted one of these tasks into that dataset.
        by_task_id = {item["task_id"]: item for item in supplemental}
        by_task_id.update({item["task_id"]: item for item in data})
        data = list(by_task_id.values())
        print(
            f"merged supplemental summary {DEFAULT_SUPPLEMENTAL_SUMMARY}: "
            f"{len(supplemental)} external tasks"
        )
    out = []
    skipped = {"not_admitted": 0, "no_util": 0, "no_root": 0,
               "no_toml": 0, "multistage": 0, "compose": 0,
               "policy_excluded": 0}
    for it in data:
        if is_policy_excluded(it["task_id"]):
            skipped["policy_excluded"] += 1
            continue
        if it.get("admission_status", "ready") != "ready":
            skipped["not_admitted"] += 1
            continue
        r = it["runs"].get("unified_4c8g")
        if not r: continue
        wall = r.get("wall_seconds")
        util = r.get("cpu_utilization_ratio")
        cpu_t = r.get("cpu_time_cumulative_s")
        if wall is None or util is None:
            skipped["no_util"] += 1; continue
        s = _score(wall, util)
        pool = it["pool"]
        troot = POOL_SOURCE_ROOTS.get(pool)
        if troot is None:
            skipped["no_root"] += 1; continue
        raw = it["raw_task_name"]
        tdir = troot / raw
        if not (tdir / "task.toml").exists():
            skipped["no_toml"] += 1; continue
        if _has_compose(tdir):
            skipped["compose"] += 1; continue
        if _dockerfile_is_multistage(tdir):
            skipped["multistage"] += 1; continue
        out.append(TaskEntry(
            task_id=it["task_id"],
            pool=pool,
            task_dir=tdir,
            wall_s=wall,
            cpu_util_ratio=util,
            cpu_time_s=cpu_t or 0,
            reward=r.get("reward"),
            score=s,
            tier=_tier(s),
        ))
    print(f"skipped: {skipped}")
    return out


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #

def _arrival_schedule(n: int, rng: random.Random) -> list[int]:
    """N arrival timestamps; first = 0, rest cumulative N(60,30) clip 0."""
    arr = [0]
    for _ in range(n - 1):
        arr.append(arr[-1] + max(0, round(rng.gauss(_ARRIVAL_MEAN_SEC, _ARRIVAL_STD_SEC))))
    return arr


def _sample_stream(l2_pool: list[TaskEntry], l3_pool: list[TaskEntry],
                    k_l2: int, k_l3: int, rng: random.Random) -> list[TaskEntry]:
    """Sample k_L2 from L2 + k_L3 from L3 WITHOUT replacement, then shuffle.
    Promote an `ubuntu:24.04` task to slot 0 for base consistency.

    2026-07-22 policy change: dupes disallowed. If k > pool size, raises
    ValueError so the caller can either expand the whitelist or lower the
    bucket target — silently duplicating a task with `-2`/`-3` renames
    inflates the image (repeated COPY of the same source tree) and can
    leak intra-stream information from a task's `-1` solve into `-2`."""
    if k_l2 > len(l2_pool):
        raise ValueError(
            f"L2 bucket asks for {k_l2} tasks but pool only has {len(l2_pool)}. "
            f"Options: (a) shrink the bucket, (b) lower the L2 tier threshold "
            f"in ADR-006 to admit more tasks, (c) enlarge the whitelist."
        )
    if k_l3 > len(l3_pool):
        raise ValueError(
            f"L3 bucket asks for {k_l3} tasks but pool only has {len(l3_pool)}. "
            f"Same options apply as for L2."
        )
    picks = []
    if k_l2:
        picks.extend(rng.sample(l2_pool, k_l2))
    if k_l3:
        picks.extend(rng.sample(l3_pool, k_l3))
    rng.shuffle(picks)
    for i, e in enumerate(picks):
        if _dockerfile_from(e.task_dir).startswith("ubuntu:24.04"):
            if i != 0:
                picks[0], picks[i] = picks[i], picks[0]
            break
    return picks


def build_streams(entries: list[TaskEntry], out_dir: Path,
                   base_seed: int = 42, force: bool = False,
                   dry_run: bool = False) -> list[dict]:
    """Assemble every bucket's streams. Returns manifest list."""
    l2_pool = sorted([e for e in entries if e.tier == "L2"], key=lambda e: e.task_id)
    l3_pool = sorted([e for e in entries if e.tier == "L3"], key=lambda e: e.task_id)
    print(f"L2 pool size: {len(l2_pool)}   L3 pool size: {len(l3_pool)}")

    manifest = []
    for bucket, spec in BUCKETS.items():
        bucket_dir = out_dir / bucket
        bucket_dir.mkdir(parents=True, exist_ok=True)
        for s in range(spec["n_streams"]):
            stream_seed = base_seed + hash((bucket, s)) % 100003
            rng = random.Random(stream_seed)
            k_l2, k_l3 = spec["k_L2"], spec["k_L3"]
            if k_l2 > 0 and len(l2_pool) == 0:
                print(f"  ⚠  {bucket}/{s} needs L2 but pool empty; skipping")
                continue
            if k_l3 > 0 and len(l3_pool) == 0:
                print(f"  ⚠  {bucket}/{s} needs L3 but pool empty; skipping")
                continue
            picks = _sample_stream(l2_pool, l3_pool, k_l2, k_l3, rng)
            arrivals = _arrival_schedule(len(picks), rng)
            name = f"{bucket}-{len(picks)}-{s + 1:02d}"
            desc = (f"V1-0 auto-synth: {k_l2} L2 + {k_l3} L3 tasks, "
                    f"tier=wall×util product≥0.02, seed={stream_seed}")
            task_refs = [
                TaskRef(source=e.task_dir, pool=e.pool.lower(),
                        task_id=e.task_id, arrive_wall_time=a)
                for e, a in zip(picks, arrivals)
            ]
            stream_spec = StreamSpec(
                name=name, description=desc,
                author="aos-bench task_construction (V1-0 auto-synth)",
                cpus=4, memory_mb=8192, allow_internet=True, max_steps=360,
                base_image="ubuntu:24.04", accept_mixed_from=True,
                extra_apt_packages=[
                    "ca-certificates", "curl", "git",
                    "python3", "python3-pip", "python-is-python3",
                    "build-essential",
                ],
                tasks=task_refs,
                shared_prefixes=[],
                pool_v02=False,
            )
            out = bucket_dir / name
            if not dry_run:
                if out.exists() and not force:
                    print(f"  SKIP {bucket}/{name} (exists; use --force to overwrite)")
                    manifest.append({
                        "tier_schema_version": V1_TIER_SCHEMA_VERSION,
                        "tier_mix": STREAM_TIER_MIX,
                        "bucket": bucket, "name": name, "seed": stream_seed,
                        "k_L2": k_l2, "k_L3": k_l3,
                        "path": str(out),
                        "tasks": [e.task_id for e in picks],
                        "arrivals": arrivals,
                        "regenerated": False,
                    })
                    continue
                assemble(stream_spec, out, force=force, mirror_profile="tencent")
            manifest.append({
                "tier_schema_version": V1_TIER_SCHEMA_VERSION,
                "tier_mix": STREAM_TIER_MIX,
                "bucket": bucket, "name": name, "seed": stream_seed,
                "k_L2": k_l2, "k_L3": k_l3,
                "path": str(out),
                "tasks": [e.task_id for e in picks],
                "arrivals": arrivals,
            })
            print(f"  {'DRY' if dry_run else 'wrote'} {bucket}/{name}  "
                  f"tasks={len(picks)}  seed={stream_seed}")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--agent-pack-lock", type=Path, default=DEFAULT_AGENT_PACK_LOCK)
    ap.add_argument("--whitelist", type=Path,
                    help="Optional file with task_ids (one per line) to restrict pool to")
    ap.add_argument(
        "--blacklist",
        type=Path,
        default=DEFAULT_BLACKLIST,
        help="Exact task IDs to exclude; use /dev/null to disable.",
    )
    args = ap.parse_args()

    validate_agent_pack_lock(args.agent_pack_lock)

    entries = load_pool(args.summary)
    blacklist = load_task_blacklist(args.blacklist)
    if blacklist:
        before = len(entries)
        entries = [entry for entry in entries if entry.task_id not in blacklist]
        print(f"applied blacklist {args.blacklist}: {before} → {len(entries)} tasks")
    if args.whitelist and args.whitelist.exists():
        wl = set(args.whitelist.read_text().split())
        before = len(entries)
        entries = [e for e in entries if e.task_id in wl]
        print(f"applied whitelist {args.whitelist}: {before} → {len(entries)} tasks")
    print(f"loaded {len(entries)} candidate tasks from {args.summary}")
    from collections import Counter
    tc = Counter(e.tier for e in entries)
    print(f"tier dist: {dict(tc)}")

    manifest = build_streams(entries, args.out_dir, base_seed=args.seed,
                              force=args.force, dry_run=args.dry_run)

    if not args.dry_run:
        mp = args.out_dir / "manifest.json"
        mp.write_text(json.dumps(manifest, indent=2))
        print(f"\nwrote manifest → {mp}")
    print(f"total streams generated: {len(manifest)}")


if __name__ == "__main__":
    main()
