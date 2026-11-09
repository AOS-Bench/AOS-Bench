"""Run the stream-level oracle on every (tb2 + skillsbench) stream and
record per-task pass/fail results.

For each stream:

  1. `docker build` the merged image (cached layers make this fast on re-runs).
     Set AOS_BENCH_ORACLE_REGISTRY to reuse images the build sweep already
     pushed instead of rebuilding locally.
  2. `docker run -v <stream>/solution:/solution <image> bash /solution/solve.sh`
     with a generous timeout. The solution tree is mounted rather than baked in:
     the assembler's anti-leak gate keeps tests/ and solution/ out of the image,
     so an image-only invocation would report no_solver for every task. This
     mirrors harbor's OracleAgent._run_stream, which uploads <stream>/solution/
     to /solution/ and execs the same entrypoint.
  3. Parse the orchestrator's summary lines:

         === oracle summary ===
         solved (N):    a b c
         no_solver (N): d e
         failed (N):    f (reason) g (reason)

  4. Append `(stream, task_id, status, reason)` rows to a CSV.

After the sweep is done, run with `--aggregate` to derive a per-task
blacklist (tasks that never passed oracle in any stream they appeared in).

Usage::

    # Build + sweep all default streams (~5-10 hours total).
    python -m aos_bench.task_construction.scripts.oracle_sweep

    # Sweep only specific streams.
    python -m aos_bench.task_construction.scripts.oracle_sweep \\
        --streams tb2-swe-3-42 tb2-swe-5-42

    # Just aggregate existing CSV without running anything.
    python -m aos_bench.task_construction.scripts.oracle_sweep --aggregate-only
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shlex
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_STREAM_DIR = _REPO_ROOT / "data" / "stream"
_DEFAULT_LOG_DIR = Path(
    os.environ.get(
        "AOS_BENCH_SWEEP_LOG_DIR",
        str(Path.home() / "aos-bench-logs/oracle-sweep"),
    )
)
_DEFAULT_CSV = _REPO_ROOT / "notes" / "oracle_sweep.csv"

_PROXY = os.environ.get("AOS_BENCH_PROXY", "")
# On hosts whose docker daemon doesn't accept `docker build` (e.g. the GLM
# docker proxy that only forwards `docker run`), set AOS_BENCH_BUILDX_BUILDER
# to a buildx builder name that points at a remote BuildKit cluster. The
# sweep then runs `docker buildx build --builder <name> --load …` instead of
# the plain `docker build`.
_BUILDX_BUILDER = os.environ.get("AOS_BENCH_BUILDX_BUILDER")
# Optional job-queue label (key HARBOR_JOB_QUEUE_LABEL) required by a docker proxy
# on hosts that gate `docker run` on queue membership. Empty = no label.
_DOCKER_RUN_LABEL = os.environ.get("AOS_BENCH_DOCKER_LABEL", "")
# Hosts that should bypass the proxy. Tsinghua mirror has direct CN routing
# and is faster without the proxy hop. hf-mirror.com / ghproxy.net are kept
# OUT of no_proxy so containers reach them through the proxy reliably (some
# tasks' HF_ENDPOINT downloads fail when hitting hf-mirror direct from a
# bridge-networked build container).
_NO_PROXY = os.environ.get("AOS_BENCH_NO_PROXY") or ",".join(
    [
        "localhost",
        "127.0.0.1",
        "mirrors.tuna.tsinghua.edu.cn",
        "mirrors.cloud.tencent.com",
        "pypi.tuna.tsinghua.edu.cn",
        "kkgithub.com",
        "ghproxy.net",
        "registry.example.com",
    ]
)


_BUILD_TIMEOUT_SEC = int(os.environ.get("AOS_BUILD_TIMEOUT_SEC", 30 * 60))
_ORACLE_TIMEOUT_SEC = int(os.environ.get("AOS_ORACLE_TIMEOUT_SEC", 30 * 60))

# When set, the sweep `docker tag` + `docker push` each successfully built
# image under this registry path and writes the resulting image reference
# back into the stream's `stream.toml [environment] docker_image` field so
# harbor's StreamTask can `docker pull` instead of re-building. Example:
#   AOS_BENCH_PUSH_REGISTRY=zhuerle/aosbench
# Empty = no push (default).
_PUSH_REGISTRY = os.environ.get("AOS_BENCH_PUSH_REGISTRY", "").rstrip("/")

# WildClaw streams have no per-task solve.sh; skip them by name prefix.
_SKIP_PREFIXES = ("wildclaw-",)

# Registry holding images that were already built and pushed by the build sweep.
# When set, the oracle reuses `<registry>/aosbench:v1-0-<stream>` instead of the
# local `aos-bench/<stream>:sweep` tag. This matters after a host reboot: the
# local image store is wiped, so the pushed copies are the only
# surviving ones and rebuilding 66 streams to re-derive them would be pointless.
# Example: AOS_BENCH_ORACLE_REGISTRY=zhuerle/aosbench
_ORACLE_REGISTRY = os.environ.get("AOS_BENCH_ORACLE_REGISTRY", "").rstrip("/")

# Flags needed to run a container from inside this container. Each one was
# added in response to an actual failure, so none is speculative:
#   --runtime=crun     runc rejects --cgroups=disabled ("not compatible with
#                      NoCgroups"); crun accepts it.
#   --cgroups=disabled /sys/fs/cgroup is read-only here, so creating
#                      /sys/fs/cgroup/cpuset/libpod_parent fails.
#   --network=host     with a private netns, crun tries to write
#                      /proc/sys/net/ipv4/ping_group_range on a read-only /proc.
# Note this is the run-time counterpart of the build sweep's
# DOCKER_BUILD_FLAGS="--isolation=chroot"; `--isolation` is a build-only flag
# and podman run rejects it.
_RUN_ISOLATION_FLAGS = [
    f
    for f in os.environ.get(
        "AOS_BENCH_RUN_FLAGS", "--runtime=crun --cgroups=disabled"
    ).split()
    if f
]


def _sweep_image(stream_dir: Path) -> str:
    """Image reference the oracle should run.

    Prefers an explicit per-stream `docker_image` recorded in the stream's
    verification log over a name derived from the stream directory: the pushed
    tags carry an attempt suffix, so they cannot be reconstructed from the
    stream name alone.
    """
    if _ORACLE_REGISTRY:
        tsv = stream_dir / "verification-logs" / "build-result.tsv"
        if tsv.is_file():
            try:
                rows = [r for r in tsv.read_text().splitlines() if r.strip()]
                if len(rows) > 1:
                    fields = rows[1].split("\t")
                    tag = fields[4].strip() if len(fields) > 4 else ""
                    # A `localhost/...` tag is a local-only rebuild that was
                    # never pushed, so it is useless to a registry-based run.
                    if tag and not tag.startswith("localhost/"):
                        return tag
            except OSError:
                pass
    return f"aos-bench/{stream_dir.name}:sweep"


def _enumerate_streams(stream_dir: Path, only: list[str] | None) -> list[Path]:
    if only:
        return [stream_dir / s for s in only if (stream_dir / s).is_dir()]
    streams: list[Path] = []
    for d in sorted(stream_dir.iterdir()):
        if not d.is_dir():
            continue
        if any(d.name.startswith(p) for p in _SKIP_PREFIXES):
            continue
        streams.append(d)
    return streams


def _docker_build(stream_dir: Path, log_path: Path) -> bool:
    image = f"aos-bench/{stream_dir.name}:sweep"
    if _BUILDX_BUILDER:
        cmd = ["docker", "buildx", "build", "--builder", _BUILDX_BUILDER, "--load"]
    else:
        cmd = ["docker", "build"]
    if _PROXY:
        cmd += [
            "--build-arg", f"HTTP_PROXY={_PROXY}",
            "--build-arg", f"HTTPS_PROXY={_PROXY}",
            "--build-arg", f"NO_PROXY={_NO_PROXY}",
        ]
    cmd += ["-t", image, str(stream_dir / "environment")]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as f:
        f.write(f"$ {' '.join(shlex.quote(c) for c in cmd)}\n")
        f.flush()
        try:
            r = subprocess.run(
                cmd, stdout=f, stderr=subprocess.STDOUT, timeout=_BUILD_TIMEOUT_SEC
            )
        except subprocess.TimeoutExpired:
            f.write("\nBUILD_TIMEOUT\n")
            return False
    return r.returncode == 0


def _push_to_registry(stream_dir: Path, log_path: Path) -> str | None:
    """Tag the locally built image under _PUSH_REGISTRY and `docker push` it.

    Returns the pushed image reference on success, None on failure. Push
    failures are non-fatal — the oracle still runs against the local tag.
    """
    local = f"aos-bench/{stream_dir.name}:sweep"
    remote = f"{_PUSH_REGISTRY}/{stream_dir.name}:sweep"
    with log_path.open("a") as f:
        f.write(f"\n$ docker tag {local} {remote}\n")
        f.flush()
        rc = subprocess.run(
            ["docker", "tag", local, remote],
            stdout=f,
            stderr=subprocess.STDOUT,
        ).returncode
        if rc != 0:
            print(f"  push: tag FAIL ({local} → {remote})")
            return None
        f.write(f"$ docker push {remote}\n")
        f.flush()
        try:
            rc = subprocess.run(
                ["docker", "push", remote],
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=_BUILD_TIMEOUT_SEC,
            ).returncode
        except subprocess.TimeoutExpired:
            f.write("\nPUSH_TIMEOUT\n")
            print("  push: timeout")
            return None
    if rc != 0:
        print(f"  push: FAIL ({remote})")
        return None
    print(f"  push: OK ({remote})")
    return remote


def _write_stream_image(stream_dir: Path, image_ref: str) -> None:
    """Set `[environment].docker_image = "<image_ref>"` in the stream's
    stream.toml so harbor's StreamTask uses it as a prebuilt image instead of
    rebuilding from `environment/Dockerfile`.
    """
    toml = stream_dir / "stream.toml"
    if not toml.exists():
        return
    txt = toml.read_text()
    if "docker_image" in txt:
        new = re.sub(
            r'docker_image\s*=\s*"[^"]*"',
            f'docker_image = "{image_ref}"',
            txt,
        )
    else:
        # Insert under [environment] after the `allow_internet = ...` line.
        new, n = re.subn(
            r'(\[environment\][^\[]*?allow_internet\s*=[^\n]*\n)',
            rf'\1docker_image   = "{image_ref}"\n',
            txt,
            count=1,
            flags=re.DOTALL,
        )
        if not n:
            print(f"  stream.toml: could not locate [environment] in {toml}")
            return
    if new != txt:
        toml.write_text(new)


def _docker_run_oracle(stream_dir: Path, log_path: Path) -> tuple[int, str]:
    image = _sweep_image(stream_dir)
    # The solution tree is deliberately NOT baked into the image (the assembler's
    # anti-leak gate drops any COPY of tests/ or solution/), so it has to be
    # supplied at run time. Harbor's OracleAgent._run_stream uploads
    # <stream>/solution/ to /solution/ and execs `bash /solution/solve.sh`; we
    # bind-mount to the same place so the generated orchestrator — which resolves
    # per-task solvers as ${SOLUTION_DIR:-/solution}/tasks/<id>/solve.sh — sees
    # the layout it was generated for. Mounting read-only would break solvers
    # that write scratch files next to themselves, so it is mounted rw.
    solution_dir = stream_dir / "solution"
    if not (solution_dir / "solve.sh").is_file():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        msg = (
            f"stream solution missing: {solution_dir / 'solve.sh'}\n"
            "Refusing to run: without it every task would be reported as "
            "no_solver, which is indistinguishable from a real solver gap.\n"
        )
        log_path.write_text(msg)
        return -2, msg
    # Stable container name so that on subprocess timeout we can SIGTERM the
    # underlying container — `subprocess.run(timeout=...)` only kills the
    # docker CLI; the container keeps running until we `docker stop` it.
    container_name = f"sweep-{stream_dir.name}-{os.getpid()}"
    cmd = [
        "docker",
        "run",
        "--rm",
        "--name", container_name,
        "--network=host",
        "-v", f"{solution_dir.resolve()}:/solution",
    ]
    cmd += _RUN_ISOLATION_FLAGS
    if _DOCKER_RUN_LABEL:
        cmd += ["--label", _DOCKER_RUN_LABEL]
    if _PROXY:
        cmd += [
            "-e", f"http_proxy={_PROXY}",
            "-e", f"https_proxy={_PROXY}",
            "-e", f"no_proxy={_NO_PROXY}",
        ]
    # Point the HuggingFace client at the public hub. Some streams bake
    # `ENV HF_ENDPOINT=https://hf-mirror.com` from earlier images; overriding
    # at run-time keeps them honest when the GLM docker proxy can reach the
    # real hub directly. New images (post HF-direct merger change) already
    # have huggingface.co baked in, so this is a no-op there.
    cmd += ["-e", "HF_ENDPOINT=https://huggingface.co"]
    # `uv pip install` defaults to a 30s per-request HTTP timeout, which is
    # not enough for 300-700 MB nvidia/cuda wheels through the GLM proxy;
    # bump it so torch-heavy solve.sh scripts (pytorch-model-recovery /
    # llm-inference-batching-scheduler / count-dataset-tokens) don't trip on
    # large wheel downloads.
    cmd += ["-e", "UV_HTTP_TIMEOUT=600"]
    cmd += [image, "bash", "/solution/solve.sh"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as f:
        f.write(f"$ {' '.join(shlex.quote(c) for c in cmd)}\n")
        f.flush()
        try:
            r = subprocess.run(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=_ORACLE_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            f.write("\nORACLE_TIMEOUT\n")
            # Best-effort cleanup so the orphan container doesn't keep
            # consuming proxy slots / image-cache locks / disk.
            subprocess.run(
                ["docker", "stop", "--time", "5", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            return -1, log_path.read_text()
    return r.returncode, log_path.read_text()


_SUMMARY_RE = re.compile(
    # The generated orchestrator (assemble.py) emits "oracle solver summary";
    # `solver` is optional here only so that logs captured by older generators
    # still parse. Without it a fully-solved stream parses as empty, which the
    # aggregator cannot tell apart from a stream that produced no results.
    r"^=== oracle (?:solver )?summary ===\s*\n"
    r"solved\s*\(\d+\):\s*(?P<solved>.*)\n"
    r"no_solver\s*\(\d+\):\s*(?P<nosolver>.*)\n"
    r"failed\s*\(\d+\):\s*(?P<failed>.*)\n",
    re.MULTILINE,
)


def _parse_oracle_log(log_text: str) -> dict[str, list[str]]:
    """Extract solved / no_solver / failed lists from orchestrator output."""
    m = _SUMMARY_RE.search(log_text)
    if not m:
        return {"solved": [], "no_solver": [], "failed": []}

    def _split(s: str) -> list[str]:
        s = s.strip()
        if not s or s == "—":
            return []
        return s.split()

    failed_raw = _split(m.group("failed"))
    # The orchestrator emits "task_id (reason)" tokens; rejoin and re-split on
    # the closing paren so multi-word reasons stay intact.
    failed_full = m.group("failed").strip()
    failed_entries = []
    if failed_full and failed_full != "—":
        # crude: split on " " before "(" so "tid (reason ...)\n tid2 (reason)" parses
        for part in re.split(r"\s+(?=[\w\-]+\s*\()", failed_full):
            failed_entries.append(part.strip())
    return {
        "solved": _split(m.group("solved")),
        "no_solver": _split(m.group("nosolver")),
        "failed": failed_entries,
    }


def _sweep(
    streams: list[Path],
    log_dir: Path,
    csv_path: Path,
    skip_build: bool = False,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_exists = csv_path.exists()
    with csv_path.open("a", newline="") as cf:
        w = csv.writer(cf)
        if not csv_exists:
            w.writerow(["stream", "task_id", "status", "reason", "ts"])
        cf.flush()
        for stream_dir in streams:
            print(f"\n[{time.strftime('%H:%M:%S')}] === {stream_dir.name} ===")
            build_log = log_dir / stream_dir.name / "build.log"
            run_log = log_dir / stream_dir.name / "oracle.log"
            if not skip_build:
                t0 = time.time()
                ok = _docker_build(stream_dir, build_log)
                print(f"  build: {'OK' if ok else 'FAIL'} ({time.time() - t0:.1f}s)")
                if not ok:
                    w.writerow(
                        [
                            stream_dir.name,
                            "<stream>",
                            "build_fail",
                            f"see {build_log}",
                            int(time.time()),
                        ]
                    )
                    cf.flush()
                    continue
                if _PUSH_REGISTRY:
                    pushed = _push_to_registry(stream_dir, build_log)
                    if pushed:
                        _write_stream_image(stream_dir, pushed)
            t0 = time.time()
            rc, log_text = _docker_run_oracle(stream_dir, run_log)
            print(f"  oracle: rc={rc} ({time.time() - t0:.1f}s)")
            parsed = _parse_oracle_log(log_text)
            ts = int(time.time())
            # Surface oracle timeouts as a distinct status so re-runs can
            # target them. Without this the seed appears as "pending" /
            # ⏳ in the progress doc because no row was written.
            no_summary = not (
                parsed["solved"] or parsed["no_solver"] or parsed["failed"]
            )
            if no_summary:
                if "ORACLE_TIMEOUT" in log_text:
                    reason = "oracle_timeout"
                elif rc != 0:
                    reason = f"no_summary_rc_{rc}"
                else:
                    reason = "no_summary"
                w.writerow(
                    [
                        stream_dir.name,
                        "<stream>",
                        "timeout" if reason == "oracle_timeout" else "no_summary",
                        f"{reason}; see {run_log}",
                        ts,
                    ]
                )
            for tid in parsed["solved"]:
                w.writerow([stream_dir.name, tid, "solved", "", ts])
            for tid in parsed["no_solver"]:
                w.writerow([stream_dir.name, tid, "no_solver", "", ts])
            for entry in parsed["failed"]:
                m = re.match(r"^(?P<tid>[\w\-]+)\s*\((?P<reason>.*)\)\s*$", entry)
                if m:
                    w.writerow(
                        [
                            stream_dir.name,
                            m.group("tid"),
                            "failed",
                            m.group("reason"),
                            ts,
                        ]
                    )
                else:
                    w.writerow([stream_dir.name, entry, "failed", "?", ts])
            cf.flush()


def _aggregate(csv_path: Path) -> None:
    """Print a per-task summary across all streams."""
    if not csv_path.exists():
        print(f"no CSV at {csv_path}")
        return
    by_task: dict[str, dict[str, int]] = defaultdict(
        lambda: {"solved": 0, "failed": 0, "no_solver": 0}
    )
    fail_reasons: dict[str, list[str]] = defaultdict(list)
    with csv_path.open() as cf:
        r = csv.DictReader(cf)
        for row in r:
            tid = row["task_id"]
            st = row["status"]
            if st in by_task[tid]:
                by_task[tid][st] += 1
            if st == "failed" and row["reason"]:
                fail_reasons[tid].append(row["reason"])

    always_solved: list[str] = []
    sometimes_solved: list[str] = []
    never_solved: list[str] = []
    no_solver_only: list[str] = []
    for tid, counts in sorted(by_task.items()):
        s, f, n = counts["solved"], counts["failed"], counts["no_solver"]
        if s == 0 and f == 0 and n > 0:
            no_solver_only.append(tid)
        elif s > 0 and f == 0:
            always_solved.append(tid)
        elif s > 0 and f > 0:
            sometimes_solved.append(tid)
        else:
            never_solved.append(tid)

    print(f"\n=== aggregate ({len(by_task)} unique task ids) ===")
    print(f"always_solved ({len(always_solved)}):    {' '.join(always_solved)}")
    print(
        f"sometimes_solved ({len(sometimes_solved)}): "
        f"{' '.join(sometimes_solved)}"
    )
    print(f"never_solved ({len(never_solved)}):     {' '.join(never_solved)}")
    print(f"no_solver_only ({len(no_solver_only)}): {' '.join(no_solver_only)}")
    if never_solved:
        print("\n=== never-solved fail reasons (top 5 per task) ===")
        for tid in never_solved:
            reasons = fail_reasons[tid][:5]
            print(f"  {tid}: {reasons}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream-dir", type=Path, default=_DEFAULT_STREAM_DIR)
    parser.add_argument("--log-dir", type=Path, default=_DEFAULT_LOG_DIR)
    parser.add_argument("--csv", type=Path, default=_DEFAULT_CSV)
    parser.add_argument(
        "--streams",
        nargs="+",
        help="Only sweep these stream names (default: all under --stream-dir, "
        "minus wildclaw-*).",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Don't (re)build images; assume aos-bench/<stream>:sweep already exists.",
    )
    parser.add_argument(
        "--aggregate-only",
        action="store_true",
        help="Skip building+running; just aggregate the existing CSV.",
    )
    args = parser.parse_args(argv)

    if args.aggregate_only:
        _aggregate(args.csv)
        return 0

    streams = _enumerate_streams(args.stream_dir, args.streams)
    if not streams:
        print("no streams to sweep")
        return 1
    print(f"sweeping {len(streams)} stream(s) → CSV={args.csv}, logs={args.log_dir}")
    _sweep(streams, args.log_dir, args.csv, skip_build=args.skip_build)
    _aggregate(args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
