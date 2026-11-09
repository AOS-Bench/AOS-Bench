"""Generate, launch, monitor, and select the low-overlap V1-0 scan.

The command deliberately keeps candidate streams and refine state outside the
canonical V1-0 tree.  ``generate`` creates 40/20/20 tier-correct candidates,
``launch`` starts one persistent refine driver per candidate, ``watch``
recovers interrupted refine drivers and waits for the 10/5/5 gate, and
``select`` chooses the successful set with the smallest task-set overlap.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
import tomllib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aos_bench.task_construction.assemble.assemble import StreamSpec, TaskRef, assemble
from aos_bench.task_construction.assemble.refine_merge_stream.orchestrator import (
    BUILD_REGISTRY_NAME,
    DEFAULT_CODEX_IMAGE,
    DEFAULT_MAX_OVERLAP_RATIO,
    SUCCESS_REGISTRY_NAME,
    _task_overlap,
    _load_success_registry,
)
from aos_bench.task_construction.v1_spec import (
    LOW_OVERLAP_SCAN_COUNTS,
    LOW_OVERLAP_SELECTION_COUNTS,
    STREAM_TIER_MIX,
    V1_TIER_SCHEMA_VERSION,
    tier_v2_root,
    validate_agent_pack_lock,
)
from aos_bench.task_construction.scripts.generate_v1_0 import (
    DEFAULT_BLACKLIST,
    _FROM_INSTRUCTION_RE,
    is_policy_excluded,
    load_task_blacklist,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SUMMARY = REPO_ROOT / "aos_bench/task_construction/manifests/v1_0_final_task_pool.json"
DEFAULT_SCAN_DIR = tier_v2_root(REPO_ROOT) / "candidates" / "low-overlap"
DEFAULT_RUN_ROOT = Path(
    str(Path.home() / "aos-bench-logs-data-synth/refine-v1-0-low-overlap")
)
DEFAULT_QUEUE = os.environ.get("AOS_REFINE_JOB_QUEUE", "")
DEFAULT_AGENT_PACK_LOCK = REPO_ROOT / "images/agents/agents.lock.toml"
BUCKET_SPECS = {
    bucket: (LOW_OVERLAP_SCAN_COUNTS[bucket], mix["L2"], mix["L3"])
    for bucket, mix in STREAM_TIER_MIX.items()
}
FINAL_COUNTS = dict(LOW_OVERLAP_SELECTION_COUNTS)


@dataclass(frozen=True)
class Entry:
    task_id: str
    tier: str
    benchmark: str
    task_dir: Path


def _load_entries(summary: Path) -> list[Entry]:
    data = json.loads(summary.read_text())
    rows = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{summary} does not contain a tasks list")
    out: list[Entry] = []
    skipped = Counter()
    for row in rows:
        tier = str(row.get("v1_tier", ""))
        task_dir = Path(str(row.get("task_dir", "")))
        if is_policy_excluded(str(row.get("task_id", ""))):
            skipped["policy_excluded"] += 1
            continue
        if tier not in {"L2", "L3"}:
            skipped["tier"] += 1
            continue
        if not (task_dir / "task.toml").is_file() or not (task_dir / "instruction.md").is_file():
            skipped["missing_task"] += 1
            continue
        dockerfile = task_dir / "environment/Dockerfile"
        if not dockerfile.is_file():
            skipped["missing_dockerfile"] += 1
            continue
        lines = dockerfile.read_text(errors="replace").splitlines()
        # Instructions start at column 0; see _from_instruction_lines in
        # generate_v1_0.py for why a stripped match is wrong (indented Python
        # `from ... import ...` inside a RUN heredoc gets miscounted).
        from_count = sum(bool(_FROM_INSTRUCTION_RE.match(line)) for line in lines)
        if from_count >= 2:
            skipped["multistage"] += 1
            continue
        if (task_dir / "environment/docker-compose.yaml").exists():
            skipped["compose"] += 1
            continue
        out.append(Entry(
            task_id=str(row["task_id"]),
            tier=tier,
            benchmark=str(row.get("benchmark", "unknown")),
            task_dir=task_dir,
        ))
    ids = [x.task_id for x in out]
    if len(ids) != len(set(ids)):
        raise ValueError("final pool contains duplicate task IDs")
    print(f"loaded compatible pool: {len(out)}; tiers={dict(Counter(x.tier for x in out))}; skipped={dict(skipped)}")
    return out


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _choose_tier(
    pool: list[Entry],
    n: int,
    task_counts: Counter[str],
    pair_counts: Counter[tuple[str, str]],
    rng: random.Random,
) -> list[Entry]:
    if len(pool) < n:
        raise ValueError(f"tier pool has {len(pool)} tasks but needs {n}")
    selected: list[Entry] = []
    available = list(pool)
    for _ in range(n):
        scored: list[tuple[float, float, Entry]] = []
        for entry in available:
            pair_penalty = sum(pair_counts[_pair_key(entry.task_id, other.task_id)] for other in selected)
            # Task reuse is normalized by the number of slots; pair reuse gets a
            # larger coefficient because pair overlap is more expensive for the
            # final Jaccard objective.
            cost = 0.35 * task_counts[entry.task_id] + 1.0 * pair_penalty
            scored.append((cost, rng.random(), entry))
        scored.sort(key=lambda x: (x[0], x[1], x[2].task_id))
        chosen = scored[0][2]
        selected.append(chosen)
        available.remove(chosen)
    return selected


def _stream_spec(name: str, bucket: str, entries: list[Entry], rng: random.Random) -> StreamSpec:
    ordered = list(entries)
    rng.shuffle(ordered)
    arrivals = [0]
    for _ in range(len(ordered) - 1):
        arrivals.append(arrivals[-1] + max(0, round(rng.gauss(60, 30))))
    return StreamSpec(
        name=name,
        description=(
            f"V1-0 low-overlap scan candidate ({bucket}); "
            f"L2={sum(x.tier == 'L2' for x in entries)}, "
            f"L3={sum(x.tier == 'L3' for x in entries)}"
        ),
        author="aos-bench V1-0 low-overlap scan",
        cpus=4,
        memory_mb=8192,
        allow_internet=True,
        max_steps=360,
        base_image="ubuntu:24.04",
        accept_mixed_from=True,
        extra_apt_packages=["ca-certificates", "curl", "git", "python3", "python3-pip", "build-essential"],
        tasks=[
            TaskRef(source=e.task_dir, pool=e.benchmark.lower(), task_id=e.task_id, arrive_wall_time=arrival)
            for e, arrival in zip(ordered, arrivals, strict=True)
        ],
    )


def generate(args: argparse.Namespace) -> int:
    validate_agent_pack_lock(args.agent_pack_lock)
    bucket_specs = dict(BUCKET_SPECS)
    heavy_l2 = getattr(args, "heavy_l2", None)
    heavy_l3 = getattr(args, "heavy_l3", None)
    if heavy_l2 is not None or heavy_l3 is not None:
        canonical = STREAM_TIER_MIX["heavy"]
        n_l2 = canonical["L2"] if heavy_l2 is None else heavy_l2
        n_l3 = canonical["L3"] if heavy_l3 is None else heavy_l3
        if n_l2 < 0 or n_l3 < 0 or n_l2 + n_l3 == 0:
            raise ValueError("heavy tier override must contain at least one task")
        bucket_specs["heavy"] = (args.heavy, n_l2, n_l3)
    entries = _load_entries(args.summary)
    blacklist = load_task_blacklist(args.blacklist)
    if blacklist:
        entries = [entry for entry in entries if entry.task_id not in blacklist]
        print(
            f"excluded {len(blacklist)} explicit blacklist task IDs; "
            f"remaining tiers={dict(Counter(entry.tier for entry in entries))}",
            flush=True,
        )
    pools = {tier: sorted((x for x in entries if x.tier == tier), key=lambda x: x.task_id) for tier in ("L2", "L3")}
    rng = random.Random(args.seed)
    task_counts: Counter[str] = Counter()
    pair_counts: Counter[tuple[str, str]] = Counter()
    records: list[dict[str, Any]] = []
    planned: list[tuple[str, str, list[Entry], int]] = []
    sequence = 0
    # Interleave buckets so global task/pair counts, rather than light alone,
    # drive the low-overlap objective.
    for round_index in range(max(args.light, args.medium, args.heavy)):
        for bucket, requested in (("light", args.light), ("medium", args.medium), ("heavy", args.heavy)):
            if round_index >= requested:
                continue
            _, n_l2, n_l3 = bucket_specs[bucket]
            chosen = _choose_tier(pools["L2"], n_l2, task_counts, pair_counts, rng)
            chosen += _choose_tier(pools["L3"], n_l3, task_counts, pair_counts, rng)
            ids = sorted(x.task_id for x in chosen)
            if len(ids) != len(set(ids)):
                raise AssertionError("candidate contains duplicate task ID")
            for entry in chosen:
                task_counts[entry.task_id] += 1
            for i, left in enumerate(ids):
                for right in ids[i + 1:]:
                    pair_counts[(left, right)] += 1
            sequence += 1
            name = f"{bucket}-{n_l2 + n_l3}-scan-{sequence:03d}"
            planned.append((bucket, name, chosen, args.seed + sequence * 1009))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    failures: list[dict[str, str]] = []

    def build_one(item: tuple[str, str, list[Entry], int]) -> dict[str, Any]:
        bucket, name, chosen, stream_seed = item
        out = args.out_dir / bucket / name
        try:
            spec = _stream_spec(name, bucket, chosen, random.Random(stream_seed))
            assemble(spec, out, force=True, mirror_profile=args.mirror_profile)
            return {
                "bucket": bucket,
                "name": name,
                "path": str(out.resolve()),
                "seed": stream_seed,
                "tasks": sorted(x.task_id for x in chosen),
                "tier_counts": dict(Counter(x.tier for x in chosen)),
                "benchmarks": dict(Counter(x.benchmark for x in chosen)),
            }
        except Exception as exc:  # keep one bad source from losing all candidates
            return {"bucket": bucket, "name": name, "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=args.assembly_concurrency) as executor:
        futures = [executor.submit(build_one, item) for item in planned]
        for future in as_completed(futures):
            record = future.result()
            if "error" in record:
                failures.append(record)
            else:
                records.append(record)
                print(f"assembled {record['bucket']}/{record['name']} ({len(record['tasks'])} tasks)", flush=True)
    records.sort(key=lambda x: (x["bucket"], x["name"]))
    tier_mix = {bucket: dict(mix) for bucket, mix in STREAM_TIER_MIX.items()}
    _, heavy_l2, heavy_l3 = bucket_specs["heavy"]
    tier_mix["heavy"] = {"L2": heavy_l2, "L3": heavy_l3}
    manifest = {
        "schema_version": 1,
        "tier_schema_version": V1_TIER_SCHEMA_VERSION,
        "tier_mix": tier_mix,
        "artifact_layout": "data/stream/V1-0/tier-v2",
        "summary": str(args.summary.resolve()),
        "seed": args.seed,
        "requested": {"light": args.light, "medium": args.medium, "heavy": args.heavy},
        "records": records,
        "assembly_failures": failures,
        "pool_tier_counts": dict(Counter(x.tier for x in entries)),
        "blacklist": str(args.blacklist.resolve()),
        "blacklisted_task_ids": sorted(blacklist),
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(records)} candidates; assembly_failures={len(failures)} -> {args.out_dir / 'manifest.json'}")
    if failures:
        for row in failures:
            print(f"assembly failure {row['bucket']}/{row['name']}: {row['error']}", file=sys.stderr)
    return 0 if len(records) == sum((args.light, args.medium, args.heavy)) else 2


def _load_manifest(scan_dir: Path) -> list[dict[str, Any]]:
    data = json.loads((scan_dir / "manifest.json").read_text())
    return list(data.get("records", []))


def _record_name(record: dict[str, Any]) -> str:
    return f"{record['bucket']}/{record['name']}"


def _select_named_records(
    records: list[dict[str, Any]],
    names: list[str] | None,
) -> list[dict[str, Any]]:
    if not names:
        return records
    requested = set(names)
    return [record for record in records if _record_name(record) in requested]


def _select_bucket_records(
    records: list[dict[str, Any]],
    buckets: list[str] | set[str] | tuple[str, ...] | None,
) -> list[dict[str, Any]]:
    if not buckets:
        return records
    requested = set(buckets)
    return [record for record in records if record.get("bucket") in requested]


def _resume_batch(
    snapshot: dict[str, Any],
    max_active_workers: int | None,
) -> list[str]:
    resumable = list(snapshot["resumable"])
    if max_active_workers is None:
        return resumable
    capacity = max(0, max_active_workers - int(snapshot["active_workers"]))
    if capacity <= 0:
        return []
    # ``resumable`` is persisted in sorted order, which otherwise makes the
    # ``heavy/...`` names win every launch cycle and starves light/medium.
    # Interleave buckets so every tier gets a chance to reach its target.
    by_bucket: dict[str, list[str]] = {bucket: [] for bucket in ("light", "medium", "heavy")}
    for name in resumable:
        bucket = name.split("/", 1)[0]
        by_bucket.setdefault(bucket, []).append(name)
    selected: list[str] = []
    while len(selected) < capacity:
        added = False
        for bucket in ("light", "medium", "heavy"):
            if by_bucket.get(bucket):
                selected.append(by_bucket[bucket].pop(0))
                added = True
                if len(selected) >= capacity:
                    break
        if not added:
            break
    return selected


def _active_run_dirs() -> set[str]:
    """Return run directories owned by live refine workers."""
    active: set[str] = set()
    for cmdline_path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = cmdline_path.read_bytes().split(b"\0")
        except OSError:
            continue
        if b"aos_bench.task_construction.assemble.refine_merge_stream" not in argv:
            continue
        try:
            index = argv.index(b"--run-dir")
            active.add(str(Path(argv[index + 1].decode()).resolve()))
        except (ValueError, IndexError, UnicodeDecodeError):
            continue
    return active


def _targets_for_records(
    records: list[dict[str, Any]],
    args: argparse.Namespace | None = None,
) -> dict[str, int]:
    targets: dict[str, int] = {}
    for bucket in sorted({str(record["bucket"]) for record in records}):
        override = getattr(args, f"target_{bucket}", None) if args else None
        target = FINAL_COUNTS[bucket] if override is None else int(override)
        if target <= 0:
            raise ValueError(f"target for {bucket} must be positive")
        targets[bucket] = target
    return targets


def _scoped_worker_processes(
    records: list[dict[str, Any]],
    run_root: Path,
) -> list[tuple[int, int, str]]:
    run_dirs = {
        str((run_root / "results" / record["bucket"] / record["name"]).resolve())
        for record in records
    }
    workers: list[tuple[int, int, str]] = []
    for cmdline_path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = [value for value in cmdline_path.read_bytes().split(b"\0") if value]
            if b"aos_bench.task_construction.assemble.refine_merge_stream" not in argv:
                continue
            index = argv.index(b"--run-dir")
            run_dir = str(Path(argv[index + 1].decode()).resolve())
            if run_dir not in run_dirs:
                continue
            pid = int(cmdline_path.parent.name)
            workers.append((pid, os.getpgid(pid), run_dir))
        except (OSError, ValueError, IndexError, UnicodeDecodeError):
            continue
    return workers


def _stop_scoped_workers(
    records: list[dict[str, Any]],
    run_root: Path,
    grace_sec: float = 30.0,
) -> list[str]:
    workers = _scoped_worker_processes(records, run_root)
    stopped = sorted({run_dir for _, _, run_dir in workers})
    process_groups = sorted({pgid for _, pgid, _ in workers})
    for process_group in process_groups:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        alive = []
        for process_group in process_groups:
            try:
                os.killpg(process_group, 0)
                alive.append(process_group)
            except ProcessLookupError:
                pass
        if not alive:
            break
        time.sleep(0.25)
    for process_group in process_groups:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return stopped


def launch(args: argparse.Namespace) -> int:
    buckets = set(getattr(args, "buckets", BUCKET_SPECS))
    records = [
        x for x in _load_manifest(args.scan_dir)
        if "error" not in x and x.get("bucket") in buckets
    ]
    records = _select_named_records(
        records,
        getattr(args, "record_names", None),
    )
    resume_failed = bool(getattr(args, "resume_failed", False))
    resume_exhausted = bool(getattr(args, "resume_exhausted", False))
    resume_interrupted = bool(getattr(args, "resume_interrupted", False))
    resume = resume_failed or resume_exhausted or resume_interrupted
    if resume:
        active_run_dirs = _active_run_dirs()
        filtered: list[dict[str, Any]] = []
        for record in records:
            state_path = args.run_root / "results" / record["bucket"] / record["name"] / "state.json"
            try:
                state = json.loads(state_path.read_text())
            except (OSError, ValueError):
                continue
            # ``codex-complete`` is a durable transaction boundary: the
            # model output is already collected, but a worker can exit before
            # applying it and starting the next verification round.  It is
            # therefore just as resumable as an explicit Codex error.
            run_dir = state_path.parent.resolve()
            resumable_statuses = set()
            if resume_failed:
                resumable_statuses.update({
                    "codex-error", "codex-complete", "security-violation",
                    "paused-infrastructure",
                })
            if resume_exhausted and int(state.get("codex_turn") or 0) < args.max_rounds:
                resumable_statuses.add("failed-max-rounds")
            if resume_interrupted:
                resumable_statuses.update({
                    "initialized", "verifying-build", "verifying-oracle",
                    "verifying-overlap", "codex-refine", "codex-complete",
                    "candidate-applied", "codex-error", "security-violation",
                    "retrying-infrastructure", "paused-infrastructure",
                })
                if int(state.get("codex_turn") or 0) < args.max_rounds:
                    resumable_statuses.add("failed-max-rounds")
            if state.get("status") in resumable_statuses and str(run_dir) not in active_run_dirs:
                filtered.append(record)
        records = filtered
    if not records:
        print("no candidates need launching")
        return 0
    if not resume and len(records) != args.concurrency:
        raise RuntimeError(
            f"scan has {len(records)} assembled candidates, but launch target is "
            f"{args.concurrency}; refusing a partial 80-loop scan"
        )
    if resume and len(records) != args.concurrency:
        print(
            f"resume snapshot changed while states were advancing: launching "
            f"the current {len(records)} resumable candidates "
            f"(requested {args.concurrency})",
            flush=True,
        )
    success_registry = Path(
        getattr(args, "success_registry", None)
        or (args.run_root / SUCCESS_REGISTRY_NAME)
    ).resolve()
    build_registry = Path(
        getattr(args, "build_registry", None)
        or (args.run_root / BUILD_REGISTRY_NAME)
    ).resolve()
    stage = str(getattr(args, "stage", "full"))
    args.run_root.mkdir(parents=True, exist_ok=True)
    (args.run_root / "launch.json").write_text(json.dumps({
        "started_at": time.time(), "scan_dir": str(args.scan_dir.resolve()),
        "queue": args.job_queue, "concurrency": args.concurrency,
        "codex_queue": getattr(args, "codex_job_queue", None),
        "records": len(records),
        "codex_image": args.codex_image,
        "success_registry": str(success_registry),
        "build_registry": str(build_registry),
        "stage": stage,
        "max_overlap_ratio_exclusive": args.max_overlap_ratio,
        "max_successful_streams": getattr(args, "max_successful_streams", None),
        "max_infrastructure_retries": getattr(
            args, "max_infrastructure_retries", 2
        ),
        "infrastructure_retry_backoff_sec": getattr(
            args, "infrastructure_retry_backoff_sec", 30.0
        ),
    }, indent=2) + "\n")
    python = sys.executable
    procs: list[tuple[dict[str, Any], subprocess.Popen[Any]]] = []
    for record in records:
        run_dir = args.run_root / "results" / record["bucket"] / record["name"]
        run_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "driver.log"
        cmd = [
            python, "-m", "aos_bench.task_construction.assemble.refine_merge_stream",
            "--stream-dir", record["path"], "--run-dir", str(run_dir),
            "--summary", str(args.summary), "--max-rounds", str(args.max_rounds),
            "--blacklist", str(args.blacklist),
            "--build-timeout-sec", str(args.build_timeout_sec),
            "--max-infrastructure-retries",
            str(getattr(args, "max_infrastructure_retries", 2)),
            "--infrastructure-retry-backoff-sec",
            str(getattr(args, "infrastructure_retry_backoff_sec", 30.0)),
            "--oracle-timeout-sec", str(args.oracle_timeout_sec),
            "--codex-timeout-sec", str(args.codex_timeout_sec),
            "--job-queue", args.job_queue, "--builder", args.builder,
            "--image-registry", getattr(
                args, "image_registry",
                os.environ.get("AOS_REFINE_IMAGE_REGISTRY", "zhuerle/aosbench"),
            ),
            "--mirror-profile", args.mirror_profile,
            "--codex-image", args.codex_image,
            "--max-overlap-ratio", str(args.max_overlap_ratio),
            "--success-registry", str(success_registry),
            "--build-registry", str(build_registry),
            "--stage", stage,
        ]
        if getattr(args, "codex_job_queue", None) is not None:
            cmd += ["--codex-job-queue", args.codex_job_queue]
        if getattr(args, "max_successful_streams", None) is not None:
            cmd += [
                "--max-successful-streams",
                str(args.max_successful_streams),
            ]
        if args.model:
            cmd += ["--model", args.model]
        if args.api_base:
            cmd += ["--api-base", args.api_base]
        if resume:
            cmd.append("--resume")
        env = os.environ.copy()
        if not env.get("GLM_API_KEY") and env.get("OPENAI_API_KEY"):
            env["GLM_API_KEY"] = env["OPENAI_API_KEY"]
        # A resume must not destroy the preceding worker diagnostics.  State,
        # prompt and verifier logs are already durable; keep driver.log equally
        # auditable by appending a clear worker boundary.
        with log_path.open("a") as log:
            log.write(f"\n=== worker launch {time.time():.6f} resume={resume} ===\n")
            log.flush()
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        procs.append((record, proc))
        print(f"launched pid={proc.pid} {record['bucket']}/{record['name']}", flush=True)
    pid_name = "resume-pids.json" if resume else "pids.json"
    (args.run_root / pid_name).write_text(json.dumps([
        {"pid": proc.pid, "bucket": rec["bucket"], "name": rec["name"], "run_dir": str(args.run_root / "results" / rec["bucket"] / rec["name"])}
        for rec, proc in procs
    ], indent=2) + "\n")
    print(f"launched {len(procs)} {'resume ' if resume else ''}refine loops under {args.run_root}; detached=True")
    if args.wait:
        return max(proc.wait() for _, proc in procs)
    return 0


def _monitor_snapshot(
    records: list[dict[str, Any]],
    run_root: Path,
    success_registry: Path | None = None,
    targets: dict[str, int] | None = None,
    *,
    resume_exhausted: bool = False,
    max_rounds: int | None = None,
    stage: str = "full",
    resume_infrastructure_after_sec: int | None = None,
) -> dict[str, Any]:
    record_run_dirs = {
        str((run_root / "results" / record["bucket"] / record["name"]).resolve())
        for record in records
    }
    active = _active_run_dirs() & record_run_dirs
    status_counts: dict[str, Counter[str]] = {
        bucket: Counter() for bucket in BUCKET_SPECS
    }
    resumable: list[str] = []
    successful_counts: Counter[str] = Counter()
    successful_names: list[str] = []
    for record in records:
        bucket = record["bucket"]
        run_dir = (run_root / "results" / bucket / record["name"]).resolve()
        state_path = run_dir / "state.json"
        state: dict[str, Any] = {}
        try:
            state = json.loads(state_path.read_text())
            status = str(state.get("status") or "unknown")
        except (OSError, ValueError):
            status = "missing-state"
        status_counts[bucket][status] += 1
        if (
            status in {
                # A scan starts with many candidates that have no state yet.
                # They are eligible for first launch when a worker slot opens;
                # treating them as non-resumable strands the scan after an
                # accepted stream exits.
                "missing-state",
                "initialized", "verifying-build", "verifying-oracle",
                "verifying-overlap", "codex-refine", "codex-complete",
                "candidate-applied", "codex-error", "security-violation",
                "retrying-infrastructure",
            }
            and str(run_dir) not in active
        ):
            resumable.append(f"{bucket}/{record['name']}")
        elif (
            resume_exhausted
            and max_rounds is not None
            and status == "failed-max-rounds"
            and int(state.get("codex_turn") or 0) < max_rounds
            and str(run_dir) not in active
        ):
            resumable.append(f"{bucket}/{record['name']}")
        elif (
            resume_infrastructure_after_sec is not None
            and status == "paused-infrastructure"
            and time.time() - state_path.stat().st_mtime
            >= resume_infrastructure_after_sec
            and str(run_dir) not in active
        ):
            resumable.append(f"{bucket}/{record['name']}")
        if (
            (stage == "build" and status == "build-passed")
            or (
                stage != "build"
                and _successful(record, run_root, success_registry) is not None
            )
        ):
            successful_counts[bucket] += 1
            successful_names.append(f"{bucket}/{record['name']}")
    registered_counts: Counter[str] = Counter()
    registered_names: list[str] = []
    if success_registry is not None:
        if stage == "build":
            try:
                registry = json.loads(success_registry.read_text())
            except (OSError, ValueError, TypeError):
                registry = {"streams": []}
        else:
            registry = _load_success_registry(success_registry)
        for row in registry["streams"]:
            name = str(row.get("name") or "")
            bucket = str(row.get("bucket") or name.split("-", 1)[0])
            if bucket not in FINAL_COUNTS:
                continue
            registered_counts[bucket] += 1
            registered_names.append(f"{bucket}/{name}")
    return {
        "timestamp": time.time(),
        "status_counts": {
            bucket: dict(sorted(counts.items()))
            for bucket, counts in status_counts.items()
        },
        "fully_passed": {
            bucket: successful_counts[bucket] for bucket in FINAL_COUNTS
        },
        "fully_passed_names": sorted(successful_names),
        "registered_successes": {
            bucket: registered_counts[bucket] for bucket in FINAL_COUNTS
        },
        "registered_success_names": sorted(registered_names),
        "active_workers": len(active),
        "active_run_dirs": sorted(active),
        "resumable": sorted(resumable),
        "target": targets or _targets_for_records(records),
        "success_registry": str(success_registry) if success_registry else None,
        "registered_success_count": sum(registered_counts.values()),
        "stage": stage,
    }


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _select_registered_streams(
    success_registry: Path,
    out_dir: Path,
    targets: dict[str, int],
) -> int:
    """Write a target-sized selection from the global accepted-stream registry.

    The watcher may combine historical accepted streams with candidates from
    the current scan.  Selecting only current records would therefore fail as
    soon as the global gate is satisfied using historical entries.
    """
    registry = _load_success_registry(success_registry)
    by_bucket: dict[str, list[dict[str, Any]]] = {
        bucket: [] for bucket in targets
    }
    for row in registry["streams"]:
        name = str(row.get("name") or "")
        bucket = str(row.get("bucket") or name.split("-", 1)[0])
        if bucket in by_bucket:
            by_bucket[bucket].append(dict(row))
    selected: list[dict[str, Any]] = []
    for bucket, target in targets.items():
        available = by_bucket[bucket]
        if len(available) < target:
            raise RuntimeError(
                "registered streams insufficient for selection target: "
                f"{bucket}={len(available)} < {target}"
            )
        selected.extend(available[:target])

    pairs: list[dict[str, Any]] = []
    for index, left in enumerate(selected):
        for right in selected[index + 1:]:
            overlap = _task_overlap(left["task_ids"], right["task_ids"])
            pairs.append({
                "left": left.get("name"),
                "right": right.get("name"),
                **overlap,
            })
    counts = Counter(str(row.get("bucket")) for row in selected)
    result = {
        "schema_version": 1,
        "tier_schema_version": V1_TIER_SCHEMA_VERSION,
        "tier_mix": STREAM_TIER_MIX,
        "artifact_layout": "data/stream/V1-0/tier-v2",
        "created_at": time.time(),
        "source_registry": str(success_registry),
        "successful_candidates": len(registry["streams"]),
        "selected_counts": dict(counts),
        "selected": selected,
        "pairwise": pairs,
        "max_pairwise_overlap": max(
            (float(pair["ratio"]) for pair in pairs), default=0.0
        ),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "selection.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    lines = ["bucket,name,state_path,tasks"]
    for row in selected:
        bucket = str(row.get("bucket") or str(row["name"]).split("-", 1)[0])
        name = str(row["name"])
        lines.append(
            f"{bucket},{name},{row.get('state_path', '')},"
            f"{' '.join(str(task) for task in row['task_ids'])}"
        )
        stream_dir = row.get("stream_dir")
        if stream_dir:
            link = out_dir / "streams" / bucket / name
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.exists():
                link.symlink_to(Path(str(stream_dir)).resolve(), target_is_directory=True)
    (out_dir / "selection.csv").write_text("\n".join(lines) + "\n")
    print(
        f"selected {len(selected)} registered streams; "
        f"max_overlap={result['max_pairwise_overlap']:.6f}"
    )
    return 0


def watch(args: argparse.Namespace) -> int:
    """Monitor, recover safe interruption states, and select at the full gate."""
    records = _select_bucket_records(
        [x for x in _load_manifest(args.scan_dir) if "error" not in x],
        getattr(args, "buckets", None),
    )
    if not records:
        raise RuntimeError("scan manifest contains no candidates")
    selected_buckets = sorted({str(record["bucket"]) for record in records})
    all_buckets = sorted(BUCKET_SPECS)
    monitor_name = (
        "monitor-state.json"
        if selected_buckets == all_buckets
        else f"monitor-state-{'-'.join(selected_buckets)}.json"
    )
    monitor_state = args.run_root / monitor_name
    stage = str(getattr(args, "stage", "full"))
    historical_success_registry = Path(
        args.success_registry or (args.run_root / SUCCESS_REGISTRY_NAME)
    ).resolve()
    build_registry = Path(
        getattr(args, "build_registry", None)
        or (args.run_root / BUILD_REGISTRY_NAME)
    ).resolve()
    monitor_registry = (
        build_registry if stage == "build" else historical_success_registry
    )
    targets = _targets_for_records(records, args)
    refill_interval_sec = int(
        getattr(args, "refill_interval_sec", None) or args.interval_sec
    )
    if refill_interval_sec <= 0:
        raise ValueError("refill interval must be positive")
    last_report_at = 0.0
    while True:
        snapshot = _monitor_snapshot(
            records,
            args.run_root,
            monitor_registry,
            targets,
            resume_exhausted=args.resume_exhausted,
            max_rounds=args.max_rounds,
            stage=stage,
            resume_infrastructure_after_sec=getattr(
                args, "resume_infrastructure_after_sec", None
            ),
        )
        report_due = (
            last_report_at == 0.0
            or snapshot["timestamp"] - last_report_at >= args.interval_sec
        )
        if report_due:
            _write_json_atomic(monitor_state, snapshot)
            last_report_at = snapshot["timestamp"]
        passed = snapshot["fully_passed"]
        registered = snapshot["registered_successes"]
        completed = snapshot["fully_passed"] if stage == "build" else registered
        if report_due:
            print(
                f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(snapshot['timestamp']))}] "
                f"stage={stage} complete light={completed['light']}/{targets.get('light', '-')} "
                f"medium={completed['medium']}/{targets.get('medium', '-')} "
                f"heavy={completed['heavy']}/{targets.get('heavy', '-')}; "
                f"scan-passed={passed}; "
                f"active={snapshot['active_workers']} resumable={len(snapshot['resumable'])}; "
                f"statuses={snapshot['status_counts']}",
                flush=True,
            )
        if all(
            completed[bucket] >= target
            for bucket, target in snapshot["target"].items()
        ):
            stopped: list[str] = []
            if args.stop_workers_at_target:
                stopped = _stop_scoped_workers(records, args.run_root)
            selection_path = args.out_dir / "selection.json"
            if stage != "build" and not selection_path.is_file():
                _select_registered_streams(
                    historical_success_registry,
                    args.out_dir,
                    snapshot["target"],
                )
            print(
                f"{stage} gate satisfied: "
                f"{monitor_registry if stage == 'build' else selection_path}; "
                f"stopped_workers={len(stopped)}",
                flush=True,
            )
            return 0
        resume_batch = _resume_batch(
            snapshot,
            getattr(args, "max_active_workers", None),
        )
        if resume_batch:
            # A missing-state candidate must be launched without --resume;
            # existing stateful workers need --resume to preserve their
            # Codex session and durable diagnostics.
            new_batch = [
                name for name in resume_batch
                if not (args.run_root / "results" / name / "state.json").is_file()
            ]
            stateful_batch = [name for name in resume_batch if name not in new_batch]

            def launch_batch(names: list[str], *, interrupted: bool) -> None:
                if not names:
                    return
                launch(argparse.Namespace(
                    scan_dir=args.scan_dir,
                    run_root=args.run_root,
                    summary=args.summary,
                    blacklist=args.blacklist,
                    job_queue=args.job_queue,
                    codex_job_queue=getattr(args, "codex_job_queue", None),
                    concurrency=len(names),
                    record_names=names,
                    max_rounds=args.max_rounds,
                    build_timeout_sec=args.build_timeout_sec,
                    max_infrastructure_retries=args.max_infrastructure_retries,
                    infrastructure_retry_backoff_sec=(
                        args.infrastructure_retry_backoff_sec
                    ),
                    oracle_timeout_sec=args.oracle_timeout_sec,
                    codex_timeout_sec=args.codex_timeout_sec,
                    builder=args.builder,
                    image_registry=args.image_registry,
                    mirror_profile=args.mirror_profile,
                    model=args.model,
                    api_base=args.api_base,
                    codex_image=args.codex_image,
                    max_overlap_ratio=args.max_overlap_ratio,
                    max_successful_streams=args.max_successful_streams,
                    success_registry=historical_success_registry,
                    build_registry=build_registry,
                    stage=stage,
                    wait=False,
                    resume_failed=False,
                    resume_exhausted=(args.resume_exhausted if interrupted else False),
                    resume_interrupted=interrupted,
                ))

            launch_batch(new_batch, interrupted=False)
            launch_batch(stateful_batch, interrupted=True)
        if args.once:
            return 0
        time.sleep(refill_interval_sec)


def _successful(
    record: dict[str, Any],
    run_root: Path,
    success_registry: Path | None = None,
) -> dict[str, Any] | None:
    state_path = run_root / "results" / record["bucket"] / record["name"] / "state.json"
    if not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text())
        rounds = state.get("rounds") or []
        last = rounds[-1]
        build = last.get("build") or {}
        oracle = last.get("oracle") or {}
        details = oracle.get("details") or {}
        rewards = details.get("rewards") or {}
        stream_dir = Path(str(state.get("stream_dir") or record["path"]))
        stream_payload = tomllib.loads((stream_dir / "stream.toml").read_text())
        current_tasks = [str(row["task_id"]) for row in stream_payload.get("arrivals", [])]
        if state.get("status") != "passed" or not build.get("passed") or not oracle.get("passed"):
            return None
        if details.get("returncode") != 0:
            return None
        if details.get("failed_tasks") or details.get("missing_tasks"):
            return None
        if (
            len(current_tasks) != len(set(current_tasks))
            or set(rewards) != set(current_tasks)
            or any(float(v) != 1.0 for v in rewards.values())
        ):
            return None
        if success_registry is not None:
            registry = _load_success_registry(success_registry)
            identity = str(state_path.resolve())
            entry = next(
                (
                    row for row in registry["streams"]
                    if str(row.get("state_path") or "") == identity
                ),
                None,
            )
            if entry is None or list(entry["task_ids"]) != current_tasks:
                return None
        return {
            **record,
            "tasks": current_tasks,
            "state_path": str(state_path),
            "rewards": rewards,
            "attempt": state.get("attempt"),
        }
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def select(args: argparse.Namespace) -> int:
    records = [x for x in _load_manifest(args.scan_dir) if "error" not in x]
    success_registry = Path(
        args.success_registry or (args.run_root / SUCCESS_REGISTRY_NAME)
    ).resolve()
    successful = [
        row
        for row in (
            _successful(record, args.run_root, success_registry)
            for record in records
        )
        if row
    ]
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in successful:
        by_bucket[row["bucket"]].append(row)
    selected: list[dict[str, Any]] = []
    task_frequency: Counter[str] = Counter()
    # Round-robin gives each bucket a chance before any one bucket dominates
    # the cross-bucket overlap objective.
    targets = _targets_for_records(records, args)
    while any(
        len([x for x in selected if x["bucket"] == bucket]) < target
        for bucket, target in targets.items()
    ):
        choices: list[tuple[float, str, dict[str, Any]]] = []
        for bucket, target in targets.items():
            if len([x for x in selected if x["bucket"] == bucket]) >= target:
                continue
            already = {x["name"] for x in selected}
            for candidate in by_bucket[bucket]:
                if candidate["name"] in already:
                    continue
                ids = set(candidate["tasks"])
                overlap = sum(_jaccard(ids, set(other["tasks"])) for other in selected)
                freq_penalty = sum((task_frequency[t] ** 2) for t in ids) * 0.01
                choices.append((overlap + freq_penalty, candidate["name"], candidate))
        if not choices:
            break
        _, _, chosen = min(choices, key=lambda x: (x[0], x[1]))
        selected.append(chosen)
        task_frequency.update(chosen["tasks"])
    counts = Counter(x["bucket"] for x in selected)
    if counts != Counter(targets):
        raise RuntimeError(
            "successful candidates insufficient for scoped targets: "
            f"selected={dict(counts)} available="
            f"{dict((key, len(value)) for key, value in by_bucket.items())}"
        )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    pairs = []
    for i, left in enumerate(selected):
        for right in selected[i + 1:]:
            pairs.append({"left": left["name"], "right": right["name"], "jaccard": _jaccard(set(left["tasks"]), set(right["tasks"]))})
    result = {
        "schema_version": 1,
        "tier_schema_version": V1_TIER_SCHEMA_VERSION,
        "tier_mix": STREAM_TIER_MIX,
        "artifact_layout": "data/stream/V1-0/tier-v2",
        "created_at": time.time(),
        "successful_candidates": len(successful),
        "selected_counts": dict(counts),
        "selected": selected,
        "task_frequency": dict(sorted(task_frequency.items())),
        "pairwise": pairs,
        "pairwise_jaccard_sum": sum(x["jaccard"] for x in pairs),
        "max_pairwise_jaccard": max((x["jaccard"] for x in pairs), default=0.0),
    }
    (args.out_dir / "selection.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    lines = ["bucket,name,state_path,tasks"]
    for row in selected:
        lines.append(f"{row['bucket']},{row['name']},{row['state_path']},{' '.join(row['tasks'])}")
        link = args.out_dir / "streams" / row["bucket"] / row["name"]
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(Path(row["path"]).resolve(), target_is_directory=True)
    (args.out_dir / "selection.csv").write_text("\n".join(lines) + "\n")
    print(
        f"selected {len(selected)} streams; "
        f"pairwise_jaccard_sum={result['pairwise_jaccard_sum']:.6f}; "
        f"max={result['max_pairwise_jaccard']:.6f}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    gen.add_argument("--blacklist", type=Path, default=DEFAULT_BLACKLIST)
    gen.add_argument("--agent-pack-lock", type=Path, default=DEFAULT_AGENT_PACK_LOCK)
    gen.add_argument("--out-dir", type=Path, default=DEFAULT_SCAN_DIR)
    gen.add_argument("--light", type=int, default=LOW_OVERLAP_SCAN_COUNTS["light"])
    gen.add_argument("--medium", type=int, default=LOW_OVERLAP_SCAN_COUNTS["medium"])
    gen.add_argument("--heavy", type=int, default=LOW_OVERLAP_SCAN_COUNTS["heavy"])
    gen.add_argument(
        "--heavy-l2",
        type=int,
        help="override the number of L2 tasks per generated heavy candidate",
    )
    gen.add_argument(
        "--heavy-l3",
        type=int,
        help="override the number of L3 tasks per generated heavy candidate",
    )
    gen.add_argument("--seed", type=int, default=20260726)
    gen.add_argument("--assembly-concurrency", type=int, default=8)
    gen.add_argument("--mirror-profile", default="tencent", choices=["tuna", "tencent", "huawei", "aliyun", "upstream"])
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--scan-dir", type=Path, default=DEFAULT_SCAN_DIR)
    launch_parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    launch_parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    launch_parser.add_argument("--blacklist", type=Path, default=DEFAULT_BLACKLIST)
    launch_parser.add_argument("--job-queue", default=DEFAULT_QUEUE)
    launch_parser.add_argument(
        "--codex-job-queue",
        default=None,
        help=(
            "queue label for Codex containers; omit to reuse --job-queue, or "
            "pass an empty value for the Docker proxy's default placement"
        ),
    )
    launch_parser.add_argument("--concurrency", type=int, default=80)
    launch_parser.add_argument(
        "--buckets",
        nargs="+",
        choices=sorted(BUCKET_SPECS),
        default=sorted(BUCKET_SPECS),
        help="launch only these tier buckets (useful for a targeted continuation)",
    )
    launch_parser.add_argument(
        "--record-names",
        nargs="+",
        help="launch exactly these bucket/name records (for bounded bootstrap batches)",
    )
    launch_parser.add_argument("--max-rounds", type=int, default=12)
    launch_parser.add_argument("--build-timeout-sec", type=int, default=3600)
    launch_parser.add_argument("--max-infrastructure-retries", type=int, default=2)
    launch_parser.add_argument(
        "--infrastructure-retry-backoff-sec", type=float, default=30.0
    )
    launch_parser.add_argument("--oracle-timeout-sec", type=int, default=10800)
    launch_parser.add_argument("--codex-timeout-sec", type=int, default=3600)
    launch_parser.add_argument("--builder", default="cluster")
    launch_parser.add_argument(
        "--image-registry",
        default=os.environ.get("AOS_REFINE_IMAGE_REGISTRY", "zhuerle/aosbench"),
    )
    launch_parser.add_argument("--codex-image", default=DEFAULT_CODEX_IMAGE)
    launch_parser.add_argument(
        "--max-overlap-ratio",
        type=float,
        default=DEFAULT_MAX_OVERLAP_RATIO,
        help="exclusive intersection/min-task-count acceptance threshold",
    )
    launch_parser.add_argument(
        "--success-registry",
        type=Path,
        help="shared successful-stream registry (defaults below --run-root)",
    )
    launch_parser.add_argument(
        "--build-registry",
        type=Path,
        help="shared immutable build-valid image registry",
    )
    launch_parser.add_argument(
        "--stage",
        choices=["full", "build", "solution"],
        default="full",
        help="run the legacy combined loop or one stage of the two-stage pipeline",
    )
    launch_parser.add_argument(
        "--max-successful-streams",
        type=int,
        help="global locked registry capacity for this acceptance epoch",
    )
    launch_parser.add_argument("--mirror-profile", default="tencent", choices=["tuna", "tencent", "huawei", "aliyun", "upstream"])
    launch_parser.add_argument("--model", default=os.environ.get("REFINE_MODEL", "gpt-5.6-sol"))
    launch_parser.add_argument("--api-base", default=os.environ.get("SYNTH_BASE_URL", "https://api.openai.com/v1"))
    launch_parser.add_argument("--wait", action="store_true")
    launch_parser.add_argument(
        "--resume-failed",
        action="store_true",
        help=(
            "resume candidates at codex-error, paused-infrastructure, or the "
            "pending codex-complete boundary"
        ),
    )
    launch_parser.add_argument(
        "--resume-exhausted",
        action="store_true",
        help=(
            "explicitly continue failed-max-rounds candidates whose existing "
            "Codex turn is below the new --max-rounds value; watch never enables this"
        ),
    )
    launch_parser.add_argument(
        "--resume-interrupted",
        action="store_true",
        help=(
            "resume every non-passed interrupted candidate, including active "
            "verifier boundaries; failed-max-rounds requires a larger max-rounds"
        ),
    )
    watch_parser = sub.add_parser("watch")
    watch_parser.add_argument("--scan-dir", type=Path, default=DEFAULT_SCAN_DIR)
    watch_parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    watch_parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    watch_parser.add_argument("--blacklist", type=Path, default=DEFAULT_BLACKLIST)
    watch_parser.add_argument("--job-queue", default=DEFAULT_QUEUE)
    watch_parser.add_argument(
        "--codex-job-queue",
        default=None,
        help=(
            "queue label for Codex containers; omit to reuse --job-queue, or "
            "pass an empty value for the Docker proxy's default placement"
        ),
    )
    watch_parser.add_argument("--interval-sec", type=int, default=1800)
    watch_parser.add_argument(
        "--refill-interval-sec",
        type=int,
        help=(
            "worker-pool scheduling interval; defaults to --interval-sec so "
            "existing invocations preserve their cadence"
        ),
    )
    watch_parser.add_argument(
        "--max-active-workers",
        type=int,
        help="cap active refine workers and refill only available slots",
    )
    watch_parser.add_argument(
        "--buckets",
        nargs="+",
        choices=sorted(BUCKET_SPECS),
        default=sorted(BUCKET_SPECS),
        help="monitor and refill only these tier buckets",
    )
    watch_parser.add_argument("--max-rounds", type=int, default=12)
    watch_parser.add_argument(
        "--resume-exhausted",
        action="store_true",
        help=(
            "continue failed-max-rounds candidates when their existing Codex "
            "turn is below the new --max-rounds value"
        ),
    )
    watch_parser.add_argument(
        "--resume-infrastructure-after-sec",
        type=int,
        help=(
            "make paused-infrastructure candidates resumable after this cooldown; "
            "omit to require manual recovery"
        ),
    )
    watch_parser.add_argument("--build-timeout-sec", type=int, default=3600)
    watch_parser.add_argument("--max-infrastructure-retries", type=int, default=2)
    watch_parser.add_argument(
        "--infrastructure-retry-backoff-sec", type=float, default=30.0
    )
    watch_parser.add_argument("--oracle-timeout-sec", type=int, default=10800)
    watch_parser.add_argument("--codex-timeout-sec", type=int, default=3600)
    watch_parser.add_argument("--builder", default="cluster")
    watch_parser.add_argument(
        "--image-registry",
        default=os.environ.get("AOS_REFINE_IMAGE_REGISTRY", "zhuerle/aosbench"),
    )
    watch_parser.add_argument("--codex-image", default=DEFAULT_CODEX_IMAGE)
    watch_parser.add_argument(
        "--max-overlap-ratio",
        type=float,
        default=DEFAULT_MAX_OVERLAP_RATIO,
        help="exclusive intersection/min-task-count acceptance threshold",
    )
    watch_parser.add_argument(
        "--success-registry",
        type=Path,
        help="shared successful-stream registry (defaults below --run-root)",
    )
    watch_parser.add_argument(
        "--build-registry",
        type=Path,
        help="shared immutable build-valid image registry",
    )
    watch_parser.add_argument(
        "--stage",
        choices=["full", "build", "solution"],
        default="full",
        help="monitor/refill the selected synthesis stage",
    )
    watch_parser.add_argument(
        "--max-successful-streams",
        type=int,
        help="global locked registry capacity passed to resumed workers",
    )
    watch_parser.add_argument("--target-light", type=int)
    watch_parser.add_argument("--target-medium", type=int)
    watch_parser.add_argument("--target-heavy", type=int)
    watch_parser.add_argument(
        "--stop-workers-at-target",
        action="store_true",
        help="terminate remaining scoped refine worker process groups at the gate",
    )
    watch_parser.add_argument("--mirror-profile", default="tencent", choices=["tuna", "tencent", "huawei", "aliyun", "upstream"])
    watch_parser.add_argument("--model", default=os.environ.get("REFINE_MODEL", "gpt-5.6-sol"))
    watch_parser.add_argument("--api-base", default=os.environ.get("SYNTH_BASE_URL", "https://api.openai.com/v1"))
    watch_parser.add_argument(
        "--out-dir",
        type=Path,
        default=tier_v2_root(REPO_ROOT) / "selected" / "low-overlap",
    )
    watch_parser.add_argument("--once", action="store_true", help="take one snapshot/recovery pass and exit")
    sel = sub.add_parser("select")
    sel.add_argument("--scan-dir", type=Path, default=DEFAULT_SCAN_DIR)
    sel.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    sel.add_argument(
        "--success-registry",
        type=Path,
        help="shared successful-stream registry (defaults below --run-root)",
    )
    sel.add_argument("--target-light", type=int)
    sel.add_argument("--target-medium", type=int)
    sel.add_argument("--target-heavy", type=int)
    sel.add_argument(
        "--out-dir",
        type=Path,
        default=tier_v2_root(REPO_ROOT) / "selected" / "low-overlap",
    )
    args = parser.parse_args()
    if args.command == "generate":
        return generate(args)
    if args.command == "launch":
        if args.concurrency != 80:
            print(f"warning: requested concurrency={args.concurrency}; target is 80", file=sys.stderr)
        return launch(args)
    if args.command == "watch":
        if args.interval_sec <= 0:
            parser.error("--interval-sec must be positive")
        if args.max_active_workers is not None and args.max_active_workers <= 0:
            parser.error("--max-active-workers must be positive")
        return watch(args)
    return select(args)


if __name__ == "__main__":
    raise SystemExit(main())
