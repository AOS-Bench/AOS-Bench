"""Persistent Codex verifier-in-loop for merged stream refinement.

This is the stream counterpart of TB3's ``loop_v1/multi_turn_orchestrator``:

    verify build -> verify oracle -> Codex refine/resume -> repeat

Codex works in a controlled copy containing ``environment/``, a stage-specific
task overlay, and a ``stream_plan.json`` control file. It may align instruction,
tests, verifier, and (outside build stage) solution packaging while preserving
task logic and verifier strength. A plan edit may replace tasks, but replacement
must be performed in a separate turn and the driver enforces the original task
count and tier distribution.

The state and Codex session live below ``--run-dir`` so an interrupted loop can
be continued with ``--resume``.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import pathlib
import random
import re
import signal
import shlex
import shutil
import subprocess
import time
import tomllib
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from aos_bench.task_construction.assemble.assemble import (
    StreamSpec,
    TaskRef,
    assemble,
)
from aos_bench.task_construction.assemble.install_mirrors import (
    normalize_domestic_install_sources,
)
from aos_bench.task_construction.scripts.generate_v1_0 import (
    DEFAULT_BLACKLIST,
    DEFAULT_SUMMARY,
    TaskEntry,
    load_pool,
    load_task_blacklist,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
DEFAULT_MODEL = os.environ.get("REFINE_MODEL", os.environ.get("SYNTH_MODEL", "gpt-5.6-sol"))
DEFAULT_API_BASE = os.environ.get("SYNTH_BASE_URL", "https://api.openai.com/v1")
# Container label a scheduling docker proxy routes on; same key Harbor uses.
_JOB_QUEUE_LABEL = os.environ.get("HARBOR_JOB_QUEUE_LABEL", "harbor.job-queue")
DEFAULT_CODEX_IMAGE = os.environ.get("AOS_REFINE_CODEX_IMAGE", "tb3-agent:codex-0.144.1")
DEFAULT_JOB_QUEUE = os.environ.get("AOS_REFINE_JOB_QUEUE", "")
DEFAULT_IMAGE_REGISTRY = os.environ.get(
    "AOS_REFINE_IMAGE_REGISTRY", "zhuerle/aosbench"
)
DEFAULT_MAX_OVERLAP_RATIO = float(os.environ.get("AOS_REFINE_MAX_OVERLAP_RATIO", "0.60"))
SUCCESS_REGISTRY_NAME = "successful-streams.json"
BUILD_REGISTRY_NAME = "build-valid-streams.json"
OVERLAP_METRIC = "intersection_over_min_task_count"
REFINE_PROMPT_PLAYBOOK = pathlib.Path(__file__).with_name("refine_prompt_playbook.md")
ALLOWED_WORKSPACE_FILES = {"environment/Dockerfile", "stream_plan.json"}
BUILD_TASK_EDIT_ROOTS = {"instruction.md", "tests", "verifier"}
READ_ONLY_WORKSPACE_PREFIXES = ("logs/",)
SENSITIVE_PARTS = {"tests", "test", "solution", "solutions", "verifier", ".git"}
SENSITIVE_FILES = {"test.sh", "test_outputs.py", "solve.sh", "check.py"}
ERROR_WORDS = (
    "error", "failed", "failure", "traceback", "exception", "not found",
    "no such file", "missing", "timeout", "timed out", "killed", "oom",
    "unable to", "could not", "modulenotfounderror", "assertionerror",
)
TRANSIENT_BUILD_PATTERNS = (
    r"InvalidChunkLength",
    r"Response ended prematurely",
    r"Connection (?:broken|reset|aborted|closed)",
    r"Temporary failure (?:in name resolution|resolving)",
    r"TLS connection was non-properly terminated",
    r"peer closed connection without sending complete message body",
    r"unexpected EOF",
    r"hashes from the requirements file",
    r"(?:HTTP|status code)\s*(?:429|500|502|503|504)\b",
    r"rpc error:\s*code\s*=\s*(?:Unavailable|Canceled|Unknown)",
    r"failed to receive status",
    r"client connection is closing",
    r"context canceled",
)
INFRASTRUCTURE_ERROR_PATTERNS = (
    r"\bBUILD_TIMEOUT\b",
    r"\bORACLE_TIMEOUT\b",
    r"did not pass /healthz",
    r"container .+ is not running",
    r"\bAddTestsDirError\b",
    r"docker compose cp transient failure",
    r"no such (?:pod|container)",
    r"docker (?:run|exec|cp) .+ timed out",
    r"command timed out after \d+ seconds",
    r"(?:HTTP|status code)\s*(?:429|500|502|503|504)\b",
    r"rpc error:\s*code\s*=\s*(?:Unavailable|Canceled|Unknown)",
    r"failed to receive status",
    r"client connection is closing",
    r"context canceled",
)


@contextlib.contextmanager
def _buildx_concurrency_slot(log: Any):
    """Bound concurrent clients of the single remote BuildKit worker.

    REFINE workers remain concurrent while build verification waits outside
    the build timeout. File locks are process-scoped and automatically release
    if a worker is interrupted, so stale slot state cannot strand the pool.
    """
    limit = int(os.environ.get("AOS_REFINE_BUILDX_MAX_CONCURRENCY", "0") or 0)
    if limit <= 0:
        yield
        return
    lock_dir = pathlib.Path(os.environ.get(
        "AOS_REFINE_BUILDX_LOCK_DIR",
        str(Path.home() / "aos-bench-logs-data-synth/.buildx-slots"),
    ))
    lock_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    handles = [(lock_dir / f"slot-{index:03d}.lock").open("a+") for index in range(limit)]
    selected = None
    try:
        while selected is None:
            for index, handle in enumerate(handles):
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                selected = index
                break
            if selected is None:
                time.sleep(1.0 + random.random())
        waited = time.monotonic() - started
        log.write(
            f"# AOS acquired BuildKit slot {selected + 1}/{limit} "
            f"after {waited:.1f}s; build timeout starts now\n"
        )
        log.flush()
        yield
    finally:
        if selected is not None:
            fcntl.flock(handles[selected].fileno(), fcntl.LOCK_UN)
        for handle in handles:
            handle.close()


def _is_single_repository(registry: str) -> bool:
    """True for a Docker Hub-style ``namespace/repository`` target, optionally
    prefixed with ``docker.io/``: all images share that one repository and
    are told apart by tag."""
    name = registry.removeprefix("docker.io/")
    parts = name.split("/")
    return len(parts) == 2 and "." not in parts[0] and ":" not in parts[0]


@dataclass
class VerifierResult:
    layer: str
    passed: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    log_path: str | None = None


@dataclass
class RoundRecord:
    attempt: int
    build: dict[str, Any] | None = None
    oracle: dict[str, Any] | None = None
    overlap: dict[str, Any] | None = None
    codex_turn: int | None = None
    codex_rc: int | None = None
    prompt_path: str | None = None
    changed_tasks: bool = False
    infrastructure_retry: str | None = None


@dataclass
class LoopState:
    version: int = 1
    status: str = "initialized"
    attempt: int = 0
    codex_turn: int = 0
    session_id: str | None = None
    stream_dir: str = ""
    original_task_count: int = 0
    original_tier_counts: dict[str, int] = field(default_factory=dict)
    failure_counts: dict[str, int] = field(default_factory=dict)
    infrastructure_failure_streak: int = 0
    infrastructure_failure_total: int = 0
    last_infrastructure_failure: str = ""
    rounds: list[dict[str, Any]] = field(default_factory=list)
    final_reason: str = ""


@dataclass
class LoopConfig:
    stream_dir: pathlib.Path
    run_dir: pathlib.Path
    max_rounds: int = 8
    build_timeout_sec: int = 3600
    build_retries: int = 2
    build_retry_backoff_sec: float = 5.0
    max_infrastructure_retries: int = 2
    infrastructure_retry_backoff_sec: float = 30.0
    oracle_timeout_sec: int = 7200
    codex_timeout_sec: int = 3600
    codex_container_ready_timeout_sec: int = 120
    model: str = DEFAULT_MODEL
    api_base: str = DEFAULT_API_BASE
    codex_image: str = DEFAULT_CODEX_IMAGE
    job_queue: str = DEFAULT_JOB_QUEUE
    # Codex containers can use the proxy's default placement when the stream
    # verifier queue is saturated.  ``None`` preserves the historical behavior
    # of reusing ``job_queue``; an empty string deliberately omits the label.
    codex_job_queue: str | None = None
    buildx_builder: str = os.environ.get("AOS_BENCH_BUILDX_BUILDER", "cluster")
    image_registry: str = DEFAULT_IMAGE_REGISTRY
    summary_path: pathlib.Path = DEFAULT_SUMMARY
    blacklist_path: pathlib.Path | None = DEFAULT_BLACKLIST
    whitelist_path: pathlib.Path | None = None
    mirror_profile: str = "upstream"
    verifier_concurrency: int = 1
    # Harbor environment backing the oracle verifier.  Only ``docker`` is
    # supported: it can build images and reach the v0.3 dispatcher.
    oracle_env: str = os.environ.get("AOS_REFINE_ORACLE_ENV", "docker")
    # Pool transport for the oracle verifier.  See
    # ``docs/framework/task_assign.md``: v0.1 writes the schedule to disk
    # agent-readable, v0.2 keeps it in memory behind a UDS (needs images built
    # with the v0.2 boilerplate), v0.3 routes MCP through an out-of-container
    # dispatcher on a docker network.
    oracle_pool_version: str = os.environ.get("AOS_REFINE_ORACLE_POOL", "v03")
    max_overlap_ratio: float = DEFAULT_MAX_OVERLAP_RATIO
    max_successful_streams: int | None = None
    success_registry_path: pathlib.Path | None = None
    build_registry_path: pathlib.Path | None = None
    stage: str = "full"
    resume: bool = False
    dry_run: bool = False
    verify_only: bool = False

    def __post_init__(self) -> None:
        if self.oracle_env != "docker":
            raise ValueError(
                f"--oracle-env {self.oracle_env!r} is not supported; use docker"
            )


class CodexNoOpError(RuntimeError):
    """Codex completed a turn without changing either allowed control file."""


def _infrastructure_failure_class(result: VerifierResult) -> str | None:
    """Return a retry class only when verifier evidence is infrastructure-only.

    These failures contain no task-level signal and must never increase task
    failure reputation or trigger a Codex mutation.  The explicit details flag
    is authoritative; pattern matching keeps older persisted states resumable.
    """
    details = result.details or {}
    if details.get("infrastructure_failure"):
        return str(details.get("failure_class") or f"{result.layer}-infrastructure")
    if result.layer == "build" and details.get("transient_failure"):
        return "build-transport"
    signals = "\n".join(str(value) for value in details.get("error_signals", []))
    evidence = f"{result.summary}\n{signals}"
    for pattern in INFRASTRUCTURE_ERROR_PATTERNS:
        if re.search(pattern, evidence, re.IGNORECASE):
            return f"{result.layer}-infrastructure"
    if result.layer == "oracle" and result.summary == "Oracle produced no reward.json":
        return "oracle-no-reward"
    return None


def _atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _task_ids(stream_dir: pathlib.Path) -> list[str]:
    raw = tomllib.loads((stream_dir / "stream.toml").read_text())
    return [str(x["task_id"]) for x in raw.get("arrivals", [])]


def _task_overlap(left: list[str], right: list[str]) -> dict[str, Any]:
    """Return the strict stream overlap metric used by the acceptance gate."""
    left_set, right_set = set(left), set(right)
    denominator = min(len(left_set), len(right_set))
    shared = sorted(left_set & right_set)
    return {
        "metric": OVERLAP_METRIC,
        "shared_task_count": len(shared),
        "denominator": denominator,
        "ratio": len(shared) / denominator if denominator else 0.0,
        "shared_tasks": shared,
    }


def _empty_success_registry(
    max_overlap_ratio: float,
    max_successful_streams: int | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "overlap_metric": OVERLAP_METRIC,
        "max_overlap_ratio_exclusive": max_overlap_ratio,
        "max_successful_streams": max_successful_streams,
        "streams": [],
    }


def _load_success_registry(
    path: pathlib.Path,
    max_overlap_ratio: float | None = None,
    max_successful_streams: int | None = None,
) -> dict[str, Any]:
    """Read and structurally validate the shared accepted-stream registry."""
    if not path.is_file():
        if max_overlap_ratio is None:
            max_overlap_ratio = DEFAULT_MAX_OVERLAP_RATIO
        return _empty_success_registry(max_overlap_ratio, max_successful_streams)
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != 1:
        raise ValueError(f"unsupported success registry schema: {path}")
    if payload.get("overlap_metric") != OVERLAP_METRIC:
        raise ValueError(f"success registry uses a different overlap metric: {path}")
    configured = float(payload.get("max_overlap_ratio_exclusive"))
    if max_overlap_ratio is not None and configured != max_overlap_ratio:
        raise ValueError(
            "success registry overlap threshold mismatch: "
            f"{configured} != {max_overlap_ratio}"
        )
    configured_limit = payload.get("max_successful_streams")
    if configured_limit is not None:
        configured_limit = int(configured_limit)
        if configured_limit <= 0:
            raise ValueError(f"success registry has an invalid stream limit: {path}")
    if (
        max_successful_streams is not None
        and configured_limit != max_successful_streams
    ):
        raise ValueError(
            "success registry stream limit mismatch: "
            f"{configured_limit} != {max_successful_streams}"
        )
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise ValueError(f"success registry streams must be a list: {path}")
    if configured_limit is not None and len(streams) > configured_limit:
        raise ValueError(
            f"success registry exceeds its stream limit: {len(streams)} > "
            f"{configured_limit}"
        )
    for row in streams:
        if not isinstance(row, dict) or not isinstance(row.get("task_ids"), list):
            raise ValueError(f"success registry contains an invalid stream entry: {path}")
        task_ids = [str(task_id) for task_id in row["task_ids"]]
        if not task_ids or len(task_ids) != len(set(task_ids)):
            raise ValueError(f"success registry contains empty or duplicate task IDs: {path}")
    for index, left in enumerate(streams):
        for right in streams[index + 1:]:
            overlap = _task_overlap(left["task_ids"], right["task_ids"])
            if overlap["ratio"] >= configured:
                raise ValueError(
                    "success registry violates its exclusive overlap threshold: "
                    f"{left.get('name')} vs {right.get('name')} = "
                    f"{overlap['ratio']:.6f} >= {configured:.6f}"
                )
    return payload


def _tail(path: pathlib.Path, chars: int = 6000) -> str:
    if not path.is_file():
        return ""
    return path.read_text(errors="replace")[-chars:]


def _error_signals(text: str, limit: int = 24) -> list[str]:
    """Return bounded error evidence without forwarding full oracle output."""
    out: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw).strip()
        if not line or not any(word in line.lower() for word in ERROR_WORDS):
            continue
        line = re.sub(r"(?i)(api[_-]?key|authorization|token)=?\S+", r"\1=<redacted>", line)
        if line not in out:
            out.append(line[:700])
        if len(out) >= limit:
            break
    return out


def _redact_log_text(text: str) -> str:
    """Remove common credential forms before logs enter the agent workspace."""
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+",
        r"\1<redacted>",
        text,
    )
    return re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)"
        r"(\s*[:=]\s*)([^\s,;\"']+)",
        r"\1\2<redacted>",
        text,
    )


def _copy_redacted_log(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_redact_log_text(source.read_text(errors="replace")))


def _load_refine_prompt_playbook() -> str:
    """Load validated repair lessons at prompt time so live loops see updates."""
    try:
        content = REFINE_PROMPT_PLAYBOOK.read_text(errors="replace").strip()
    except OSError:
        return "No additional verifier-confirmed repair lessons are available."
    return content or "No additional verifier-confirmed repair lessons are available."


def _task_oracle_reputation(results_root: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Aggregate exact-pass history from sibling stream refine states."""
    totals: dict[str, dict[str, int]] = {}
    for state_path in results_root.glob("*/*/state.json"):
        try:
            rounds = json.loads(state_path.read_text()).get("rounds") or []
        except (OSError, ValueError, TypeError):
            continue
        for round_record in rounds:
            details = ((round_record.get("oracle") or {}).get("details") or {})
            for task_id, raw_reward in (details.get("rewards") or {}).items():
                try:
                    passed = float(raw_reward) == 1.0
                except (TypeError, ValueError):
                    passed = False
                row = totals.setdefault(str(task_id), {"passes": 0, "failures": 0})
                row["passes" if passed else "failures"] += 1
    reputation: dict[str, dict[str, Any]] = {}
    for task_id, row in totals.items():
        passes, failures = row["passes"], row["failures"]
        evaluations = passes + failures
        exact_pass_rate = passes / evaluations if evaluations else 0.0
        avoid = (
            (failures >= 3 and passes == 0)
            or (evaluations >= 8 and exact_pass_rate < 0.1)
        )
        reputation[task_id] = {
            "oracle_passes": passes,
            "oracle_failures": failures,
            "exact_pass_rate": round(exact_pass_rate, 4),
            "replacement_status": "avoid" if avoid else "eligible",
        }
    return reputation


def _accepted_stream_usage(
    results_root: pathlib.Path,
    success_registry_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Return task usage across fully passed sibling streams.

    A configured diversity-gated registry is authoritative, including before its
    first entry is written. Legacy callers without a registry fall back to
    canonical paths recorded in fully passed states.
    """
    usage: dict[str, int] = {}
    streams: list[dict[str, Any]] = []
    if success_registry_path is not None:
        payload = _load_success_registry(success_registry_path)
        for row in payload["streams"]:
            task_ids = [str(task_id) for task_id in row["task_ids"]]
            for task_id in task_ids:
                usage[task_id] = usage.get(task_id, 0) + 1
            streams.append({
                "name": str(row.get("name") or pathlib.Path(str(row.get("stream_dir", ""))).name),
                "task_ids": task_ids,
            })
        return {
            "accepted_stream_count": len(streams),
            "accepted_task_usage": dict(sorted(usage.items())),
            "accepted_streams": streams,
        }
    for state_path in sorted(results_root.glob("*/*/state.json")):
        try:
            payload = json.loads(state_path.read_text())
        except (OSError, ValueError, TypeError):
            continue
        passed = payload.get("status") == "passed" or any(
            bool((record.get("oracle") or {}).get("passed"))
            for record in payload.get("rounds") or []
        )
        stream_dir = pathlib.Path(str(payload.get("stream_dir", "")))
        if not passed or not (stream_dir / "stream.toml").is_file():
            continue
        try:
            task_ids = _task_ids(stream_dir)
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            continue
        for task_id in task_ids:
            usage[task_id] = usage.get(task_id, 0) + 1
        streams.append({
            "name": stream_dir.name,
            "task_ids": task_ids,
        })
    return {
        "accepted_stream_count": len(streams),
        "accepted_task_usage": dict(sorted(usage.items())),
        "accepted_streams": streams,
    }


def _is_transient_build_failure(text: str) -> bool:
    """Identify transport/integrity failures safe to retry without Codex."""
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in TRANSIENT_BUILD_PATTERNS)


def _dockerfile_repair_chunks(before: str, after: str) -> list[dict[str, str]]:
    """Extract exact non-insertion edits that can be replayed after assembly."""
    matcher = difflib.SequenceMatcher(
        a=before.splitlines(keepends=True),
        b=after.splitlines(keepends=True),
        autojunk=False,
    )
    chunks: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in {"replace", "delete"}:
            continue
        old = "".join(matcher.a[i1:i2])
        new = "".join(matcher.b[j1:j2])
        if old and old != new:
            chunks.append({"before": old, "after": new})
    return chunks


def _tree_hashes(root: pathlib.Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel == ".refine-baseline.json":
            continue
        if rel.startswith(READ_ONLY_WORKSPACE_PREFIXES):
            continue
        hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _prune_sensitive_workspace(environment_dir: pathlib.Path) -> None:
    """Remove test/oracle material accidentally staged below environment/_sources."""
    paths = sorted(environment_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for path in paths:
        relative_parts = {part.lower() for part in path.relative_to(environment_dir).parts}
        sensitive = bool(relative_parts & SENSITIVE_PARTS)
        if path.is_file() and path.name.lower() in SENSITIVE_FILES:
            sensitive = True
        if not sensitive or not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _sensitive_dockerfile_lines(text: str) -> list[str]:
    violations: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"(?i)^(COPY|ADD)\s+(.+)$", line)
        if not match:
            continue
        args = match.group(2).lower()
        if any(re.search(rf"(?:^|[/\s]){re.escape(part)}(?:[/\s]|$)", args) for part in SENSITIVE_PARTS):
            violations.append(line)
        if any(name in args for name in SENSITIVE_FILES):
            violations.append(line)
    return violations


def _validate_dockerfile_security(text: str, baseline_text: str = "") -> None:
    """Reject sensitive COPY/ADD instructions introduced by Codex.

    Some upstream task Dockerfiles legitimately copy fixtures below paths
    named ``test``. Those assembler-owned instructions are part of the build
    context and may remain, but Codex must not add any new sensitive source.
    """
    baseline = _sensitive_dockerfile_lines(baseline_text)
    violations = _sensitive_dockerfile_lines(text)
    # Path-only repairs may legitimately rewrite an assembler-owned sensitive
    # COPY/ADD line.  Do not permit the edit to increase the number of such
    # instructions, which would allow new tests/solution material to be added
    # to the runtime image.
    if len(violations) > len(baseline):
        raise ValueError(
            "Dockerfile adds tests/solution material beyond the assembler baseline: "
            + " | ".join(violations[:8])
        )


def _allowed_task_workspace_path(
    path: str,
    task_ids: set[str],
    *,
    stage: str = "full",
) -> bool:
    """Allow only stage-specific contract files for current stream tasks."""
    parts = pathlib.PurePosixPath(path).parts
    if len(parts) < 3 or parts[0] != "tasks" or parts[1] not in task_ids:
        return False
    relative = parts[2:]
    if relative == ("instruction.md",):
        return True
    if stage == "build":
        return relative[0] in BUILD_TASK_EDIT_ROOTS
    return relative[0] in {"tests", "verifier", "solution"}


def _allowed_solution_workspace_path(path: str, task_ids: set[str]) -> bool:
    """Allow only solution files belonging to a task in the frozen stream."""
    parts = pathlib.PurePosixPath(path).parts
    return (
        len(parts) >= 4
        and parts[0] == "tasks"
        and parts[1] in task_ids
        and parts[2] == "solution"
    )


def _editable_workspace_roots(
    task_ids: list[str],
    workspace: pathlib.Path | None = None,
    stage: str = "full",
) -> list[str]:
    """Return the exact roots collected from a completed Codex turn."""
    roots = [] if stage == "solution" else sorted(ALLOWED_WORKSPACE_FILES)
    for task_id in sorted(task_ids):
        candidates = [f"tasks/{task_id}/solution"]
        if stage == "build":
            candidates = [
                f"tasks/{task_id}/instruction.md",
                f"tasks/{task_id}/tests",
                f"tasks/{task_id}/verifier",
            ]
        elif stage != "solution":
            candidates[:0] = [
                f"tasks/{task_id}/instruction.md",
                f"tasks/{task_id}/tests",
                f"tasks/{task_id}/verifier",
            ]
        roots.extend(
            path for path in candidates
            if workspace is None or (workspace / path).exists()
        )
    return roots


def _extract_session_id(text: str) -> str | None:
    match = re.search(r'"thread_id":"([^"]+)"', text)
    return match.group(1) if match else None


def _audit_codex_commands(jsonl_text: str, workspace: pathlib.Path) -> list[str]:
    """Flag Codex shell commands that try to escape the sanitized workspace."""
    violations: list[str] = []
    # Match repository/host paths at shell-token boundaries below.  A broad
    # substring check would incorrectly reject URLs such as
    # ``.../anaconda/cloud/conda-forge/...`` used for package probing.
    forbidden = (str(REPO_ROOT),)
    for line in jsonl_text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if item.get("type") != "command_execution":
            continue
        command = str(item.get("command", ""))
        inspected = command.replace(str(workspace.resolve()), "<workspace>")
        lowered = inspected.lower()
        reasons = [token for token in forbidden if token.lower() in lowered]
        try:
            shell_tokens = shlex.split(inspected)
        except ValueError:
            shell_tokens = inspected.split()
        for token in shell_tokens:
            normalized = token.strip("'\"").lower()
            if normalized.startswith((
                "/cloud/", "/root/", "/workspace/terminal/",
                "/solution/", "/tests/", "/verifier/",
            )):
                reasons.append("absolute sensitive path")
            if normalized.startswith(("../", "./../")):
                reasons.append("path escape")
        if reasons:
            violations.append(f"{', '.join(sorted(set(reasons)))}: {command[:300]}")
    return violations


class StreamRefineLoop:
    def __init__(self, config: LoopConfig):
        self.config = config
        self.stream_dir = config.stream_dir.resolve()
        self.run_dir = config.run_dir.resolve()
        self.workspace = self.run_dir / "workspace"
        self.codex_home = self.run_dir / "codex_home"
        self.logs_dir = self.run_dir / "logs"
        self.jobs_dir = self.run_dir / "jobs"
        self.state_path = self.run_dir / "state.json"
        if self.config.stage not in {"full", "build", "solution"}:
            raise ValueError("stage must be one of: full, build, solution")
        if not 0.0 < self.config.max_overlap_ratio <= 1.0:
            raise ValueError("max_overlap_ratio must be in (0, 1]")
        if (
            self.config.max_successful_streams is not None
            and self.config.max_successful_streams <= 0
        ):
            raise ValueError("max_successful_streams must be positive")
        if self.config.max_infrastructure_retries < 0:
            raise ValueError("max_infrastructure_retries must be non-negative")
        if self.config.infrastructure_retry_backoff_sec < 0:
            raise ValueError("infrastructure_retry_backoff_sec must be non-negative")
        default_registry = self.run_dir.parents[1] / SUCCESS_REGISTRY_NAME
        self.success_registry_path = (
            self.config.success_registry_path or default_registry
        ).resolve()
        default_build_registry = self.run_dir.parents[2] / BUILD_REGISTRY_NAME
        self.build_registry_path = (
            self.config.build_registry_path or default_build_registry
        ).resolve()
        if (
            self.config.stage == "build"
            and self.build_registry_path == self.success_registry_path
        ):
            raise ValueError(
                "build and historical success registries must be different files"
            )
        run_hash = hashlib.sha256(str(self.run_dir).encode()).hexdigest()[:12]
        self.codex_container = f"aos-refine-codex-{run_hash}"
        self.all_pool = [
            entry
            for entry in load_pool(self.config.summary_path)
            if entry.tier in {"L2", "L3"}
        ]
        self.all_pool_by_id = {entry.task_id: entry for entry in self.all_pool}
        self.explicit_blacklist = load_task_blacklist(self.config.blacklist_path)
        self.pool = self._filter_candidate_pool(self.all_pool)
        self.pool_by_id = {entry.task_id: entry for entry in self.pool}
        self.state = self._load_or_initialize_state()
        if self.config.stage == "solution":
            image = self._configured_stream_image()
            identity, runtime_image = self._solution_image_references(image)
            self.solution_image_identity = identity
            self.solution_runtime_image = runtime_image
            if image != runtime_image:
                # The host docker-proxy rejects every repo@sha256 reference,
                # including known public dispatcher images, while accepting
                # the exact same manifest through a tag.  Use the unique
                # build-attempt tag only after verifying below that it still
                # resolves to the immutable digest recorded at build time.
                self._set_docker_image(runtime_image)
        if self.config.resume and self.state.status == "paused-infrastructure":
            # A manual resume is the operator's signal that the shared
            # builder/job queue was repaired or has recovered. Start a fresh
            # bounded retry window without discarding the prior audit trail.
            self.state.status = "candidate-applied"
            self.state.infrastructure_failure_streak = 0
            self.state.last_infrastructure_failure = ""
            self.state.final_reason = ""
            self._save_state()
        baseline_path = self.workspace / ".refine-baseline.json"
        legacy_workspace = False
        if self.config.resume and baseline_path.is_file():
            try:
                legacy_workspace = any(
                    path.startswith("environment/_sources/")
                    for path in json.loads(baseline_path.read_text())
                )
            except (OSError, ValueError):
                legacy_workspace = True
        if legacy_workspace and self.state.status == "codex-complete":
            # Older runs exposed the merged build source bundle.  Do not
            # apply or continue those model outputs: discard the transaction
            # and force a new session from a clean, source-free workspace.
            self.state.status = "codex-error"
            self.state.session_id = None
            self.state.final_reason = (
                "discarded legacy Codex turn staged with environment/_sources"
            )
            self._save_state()
        # A prior run may have been created before the sanitized staging
        # policy was tightened. Rebuild that temporary workspace on resume
        # so stale ``_sources`` material cannot be sent to Codex.
        if self.config.resume and self.state.status in {
            "initialized", "verifying-build", "verifying-oracle", "verifying-overlap",
            "codex-error", "security-violation", "codex-refine",
            "candidate-applied",
        }:
            self._prepare_workspace()

    def _filter_candidate_pool(self, entries: list[TaskEntry]) -> list[TaskEntry]:
        entries = [
            entry for entry in entries
            if entry.task_id not in self.explicit_blacklist
        ]
        if self.config.whitelist_path:
            allowed = set(self.config.whitelist_path.read_text().split())
            entries = [entry for entry in entries if entry.task_id in allowed]
        return entries

    def _canonical_task_id(self, task_id: str) -> str:
        if task_id in self.all_pool_by_id:
            return task_id
        # Assembler suffixes duplicate IDs as foo-2, foo-3.  Replacement is
        # required to remove duplicates, but tier inference must still work.
        base = re.sub(r"-\d+$", "", task_id)
        return base if base in self.all_pool_by_id else task_id

    def _tiers_for(self, task_ids: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task_id in task_ids:
            canonical = self._canonical_task_id(task_id)
            entry = self.all_pool_by_id.get(canonical)
            if not entry:
                raise ValueError(f"task {task_id!r} is absent from the configured candidate pool")
            counts[entry.tier] = counts.get(entry.tier, 0) + 1
        return counts

    def _load_or_initialize_state(self) -> LoopState:
        if self.config.resume:
            if not self.state_path.is_file():
                raise FileNotFoundError(f"cannot resume: no state at {self.state_path}")
            return LoopState(**json.loads(self.state_path.read_text()))
        if self.state_path.exists():
            raise FileExistsError(f"run already exists at {self.run_dir}; pass --resume")
        tasks = _task_ids(self.stream_dir)
        state = LoopState(
            stream_dir=str(self.stream_dir),
            original_task_count=len(tasks),
            original_tier_counts=self._tiers_for(tasks),
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir()
        self.jobs_dir.mkdir()
        self._save_state(state)
        self._prepare_workspace()
        return state

    def _save_state(self, state: LoopState | None = None) -> None:
        if state is not None:
            self.state = state
        _atomic_json(self.state_path, asdict(self.state))

    def _plan_payload(self) -> dict[str, Any]:
        current = _task_ids(self.stream_dir)
        reputation = self._replacement_reputation()
        diversity = self._accepted_stream_usage()
        accepted_usage = diversity["accepted_task_usage"]
        candidates = [
            {
                "task_id": e.task_id,
                "tier": e.tier,
                "pool": e.pool,
                "accepted_usage": accepted_usage.get(e.task_id, 0),
                **reputation.get(e.task_id, {
                    "oracle_passes": 0,
                    "oracle_failures": 0,
                    "exact_pass_rate": None,
                    "replacement_status": "unknown",
                }),
            }
            for e in sorted(self.pool, key=lambda x: (x.tier, x.task_id))
        ]
        return {
            "constraints": {
                "task_count": self.state.original_task_count,
                "tier_counts": self.state.original_tier_counts,
                "duplicates_allowed": False,
            },
            "task_ids": current,
            "candidate_pool": candidates,
            "replacement_blocklist": sorted(
                self.explicit_blacklist | {
                    task_id for task_id, row in reputation.items()
                    if row["replacement_status"] == "avoid"
                }
            ),
            "mandatory_replacements": sorted(
                set(current) & self.explicit_blacklist
            ),
            "diversity": {
                **diversity,
                "current_overlap_by_accepted_stream": {
                    row["name"]: len(set(current) & set(row["task_ids"]))
                    for row in diversity["accepted_streams"]
                },
            },
        }

    def _replacement_reputation(self) -> dict[str, dict[str, Any]]:
        results_root = self.run_dir.parents[1]
        return _task_oracle_reputation(results_root)

    def _accepted_stream_usage(self) -> dict[str, Any]:
        return _accepted_stream_usage(
            self.run_dir.parents[1],
            self.success_registry_path,
        )

    def _current_explicit_blacklist(self) -> set[str]:
        if not self.explicit_blacklist:
            return set()
        return set(_task_ids(self.stream_dir)) & self.explicit_blacklist

    def _configured_stream_image(self) -> str:
        raw = tomllib.loads((self.stream_dir / "stream.toml").read_text())
        return str((raw.get("environment") or {}).get("docker_image") or "")

    def _solution_image_references(self, configured: str) -> tuple[str, str]:
        """Return the immutable identity and queue-compatible runtime tag.

        GLM's docker-proxy currently rejects digest-form pulls even for public
        repositories.  BuildKit and tag-form docker pulls work.  Solution-only
        REFINE therefore permits a tag only when the build-valid registry binds
        that exact per-attempt tag to an immutable digest and a live registry
        inspection confirms the binding has not changed.
        """
        if not self.build_registry_path.is_file():
            raise ValueError(
                f"solution stage requires build registry: {self.build_registry_path}"
            )
        try:
            rows = json.loads(self.build_registry_path.read_text()).get("streams", [])
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(
                f"solution stage could not read build registry: {self.build_registry_path}"
            ) from exc
        matches = [
            row for row in rows
            if str(row.get("name") or "") == self.stream_dir.name
            and configured in {
                str(row.get("image") or ""),
                str(row.get("image_tag") or ""),
            }
        ]
        if len(matches) != 1:
            raise ValueError(
                "solution stage image is not uniquely registered for stream "
                f"{self.stream_dir.name}: {configured!r}"
            )
        row = matches[0]
        identity = str(row.get("image") or "")
        runtime_tag = str(row.get("image_tag") or "")
        digest = str(row.get("image_digest") or "")
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", identity):
            raise ValueError(f"build registry image is not immutable: {identity!r}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"build registry digest is invalid: {digest!r}")
        if identity.rsplit("@", 1)[1] != digest:
            raise ValueError("build registry image and digest disagree")
        if (
            not runtime_tag
            or "@" in runtime_tag
            or ":" not in runtime_tag.rsplit("/", 1)[-1]
            or runtime_tag.rsplit(":", 1)[0] != identity.rsplit("@", 1)[0]
        ):
            raise ValueError(
                f"build registry runtime tag is not a matching tagged image: {runtime_tag!r}"
            )
        try:
            inspect = subprocess.run(
                [
                    "docker", "buildx", "imagetools", "inspect", runtime_tag,
                    "--format", "{{json .Manifest}}",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"timed out validating solution image tag: {runtime_tag}"
            ) from exc
        if inspect.returncode:
            raise RuntimeError(
                "could not validate solution image tag against its immutable digest: "
                + _redact_log_text(inspect.stderr.strip())[-800:]
            )
        try:
            resolved_digest = str(json.loads(inspect.stdout).get("digest") or "")
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"registry returned invalid manifest metadata for {runtime_tag}"
            ) from exc
        if resolved_digest != digest:
            raise RuntimeError(
                "solution image tag digest changed: "
                f"registered={digest}, resolved={resolved_digest or '<missing>'}"
            )
        return identity, runtime_tag

    def _workspace_contract(self) -> tuple[str, str]:
        if self.config.stage == "build":
            return (
                "`environment/Dockerfile`, `stream_plan.json`, and the current "
                "tasks' `instruction.md`, `tests/**`, and `verifier/**`",
                "Build-stage edits may repair the image recipe or replace one "
                "unbuildable task while preserving the stream tier mix. If a "
                "Dockerfile edit changes a task-visible path or command, update "
                "instruction, tests, and verifier references in the same turn. "
                "Solution files remain hidden and immutable.",
            )
        if self.config.stage == "solution":
            return (
                "the current tasks' `solution/**` files only",
                "The image is immutable. Solution-stage edits may isolate paths, "
                "create task-local virtual environments, or install independent "
                "runtime dependencies, but may not edit the image, task contract, "
                "tests, verifier, or task composition.",
            )
        return (
            "`environment/Dockerfile`, `stream_plan.json`, and the current "
            "tasks' `instruction.md`, `tests/**`, and `solution/**`",
            "Task edits must preserve the original task logic and verifier strength.",
        )

    def _prepare_workspace(self) -> None:
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.workspace.mkdir(parents=True)
        # The merged build context contains ``environment/_sources`` with
        # task source trees (and, for some tasks, solution-adjacent material).
        # It is needed by the host-side Docker build, but must never be
        # exposed to Codex.  Copy only the environment metadata/files and
        # explicitly omit that source bundle from the sanitized workspace.
        shutil.copytree(
            self.stream_dir / "environment",
            self.workspace / "environment",
            ignore=shutil.ignore_patterns("_sources"),
        )
        _prune_sensitive_workspace(self.workspace / "environment")
        # Stage the task-facing contract as an editable overlay.  The merged
        # environment source bundle remains hidden, while instruction, tests,
        # and task-local oracle solution may be repaired together when path or
        # packaging changes must stay logically consistent.
        # Build-stage Codex receives only the explicit contract surfaces needed
        # to keep paths aligned with Dockerfile repairs. Solutions stay hidden.
        # Solution/full stages retain their existing controlled task bundle.
        tasks_src = self.stream_dir / "tasks"
        if self.config.stage == "build" and tasks_src.is_dir():
            tasks_dst = self.workspace / "tasks"
            for task_id in _task_ids(self.stream_dir):
                source = tasks_src / task_id
                target = tasks_dst / task_id
                for name in sorted(BUILD_TASK_EDIT_ROOTS):
                    item = source / name
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(item, target / name)
                    elif item.is_file():
                        target.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, target / name)
        elif tasks_src.is_dir():
            shutil.copytree(tasks_src, self.workspace / "tasks")
        _atomic_json(self.workspace / "stream_plan.json", self._plan_payload())
        allowed, contract = self._workspace_contract()
        (self.workspace / "README.md").write_text(
            "# Controlled stream refinement workspace\n\n"
            f"Stage: `{self.config.stage}`. Allowed edits: {allowed}.\n"
            "Read-only build/oracle diagnostics are refreshed under `logs/` "
            "before every Codex turn. Inspect the current attempt there when "
            "the prompt's bounded error summary is insufficient.\n"
            f"{contract}\n"
            "Do not add files outside these paths or copy task solution material "
            "into the runtime image.\n"
        )
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=False)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.email", "refine@aos"], check=False)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.name", "aos-refine"], check=False)
        subprocess.run(["git", "-C", str(self.workspace), "add", "."], check=False)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-q", "-m", "baseline"], check=False)
        _atomic_json(self.workspace / ".refine-baseline.json", _tree_hashes(self.workspace))

    def _sync_diagnostic_logs(self) -> None:
        """Expose redacted build/oracle logs as read-only Codex context.

        The normal run log tree contains full build and Harbor command output.
        Per-task verifier stdout and the oracle-agent transcript live under
        ``jobs/`` instead, so materialize the latest copy for each attempt next
        to its build/oracle logs. None of these files participate in the edit
        allowlist or get copied back from the Codex container.
        """
        destination = self.workspace / "logs"
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)

        if self.logs_dir.is_dir():
            for source in sorted(path for path in self.logs_dir.rglob("*") if path.is_file()):
                relative = source.relative_to(self.logs_dir)
                _copy_redacted_log(source, destination / relative)

        if not self.jobs_dir.is_dir():
            return
        for attempt_jobs in sorted(path for path in self.jobs_dir.glob("attempt-*") if path.is_dir()):
            rewards = sorted(
                attempt_jobs.rglob("verifier/reward.json"),
                key=lambda path: path.stat().st_mtime,
            )
            if not rewards:
                continue
            reward = rewards[-1]
            target = destination / attempt_jobs.name / "oracle-artifacts"
            _copy_redacted_log(reward, target / "reward.json")
            verifier_root = reward.parent
            for stdout in sorted(verifier_root.glob("*/stdout.txt")):
                _copy_redacted_log(
                    stdout,
                    target / "verifier" / stdout.parent.name / "stdout.txt",
                )
            oracle = reward.parents[1] / "agent" / "oracle.txt"
            if oracle.is_file():
                _copy_redacted_log(oracle, target / "oracle.txt")

    def _validate_workspace_edits(self) -> set[str]:
        baseline = json.loads((self.workspace / ".refine-baseline.json").read_text())
        current = _tree_hashes(self.workspace)
        changed = {
            path for path in set(baseline) | set(current)
            if baseline.get(path) != current.get(path)
        }
        # Ignore git internals and the generated README; only user-visible
        # workspace files participate in the allowlist.
        changed = {path for path in changed if not path.startswith(".git/")}
        task_ids = set(_task_ids(self.stream_dir))
        if self.config.stage == "build":
            illegal = sorted(
                path for path in changed
                if path not in ALLOWED_WORKSPACE_FILES
                and not _allowed_task_workspace_path(path, task_ids, stage="build")
            )
        elif self.config.stage == "solution":
            illegal = sorted(
                path for path in changed
                if not _allowed_solution_workspace_path(path, task_ids)
            )
        else:
            illegal = sorted(
                path for path in changed
                if path not in ALLOWED_WORKSPACE_FILES
                and not _allowed_task_workspace_path(path, task_ids)
            )
        if illegal:
            raise ValueError(
                f"Codex changed files forbidden in {self.config.stage} stage: {illegal}"
            )
        return changed

    def _write_edit_audit(self, changed_files: set[str]) -> None:
        attempt_dir = self.logs_dir / f"attempt-{self.state.attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "-C", str(self.workspace), "diff", "--binary", "--", "."],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        (attempt_dir / "codex-edit.patch").write_text(proc.stdout)
        _atomic_json(attempt_dir / "codex-edit-files.json", {
            "changed_files": sorted(changed_files),
            "hashes": {
                path: hashlib.sha256((self.workspace / path).read_bytes()).hexdigest()
                for path in sorted(changed_files)
                if (self.workspace / path).is_file()
            },
        })

    def _validate_task_overlay_equivalence(self, changed_files: set[str]) -> None:
        """Reject obvious task weakening before any staged task edit is applied.

        Semantic equivalence ultimately remains a verifier property, but these
        structural checks prevent the common unsafe shortcuts (deleting tests,
        truncating the instruction, or removing assertion/failure checks).
        """
        changed_task_ids = {
            pathlib.PurePosixPath(path).parts[1]
            for path in changed_files
            if path.startswith("tasks/")
        }
        check_tokens = ("assert", "pytest", "unittest", "raise ", "exit 1", " fail")
        for task_id in changed_task_ids:
            staged = self.workspace / "tasks" / task_id
            original = self.stream_dir / "tasks" / task_id
            instruction = staged / "instruction.md"
            tests = staged / "tests"
            solution = staged / "solution"
            if not instruction.is_file() or not instruction.read_text(errors="replace").strip():
                raise ValueError(f"task {task_id} removed or emptied instruction.md")
            if not tests.is_dir() or not any(path.is_file() for path in tests.rglob("*")):
                raise ValueError(f"task {task_id} removed or emptied tests/")
            if self.config.stage != "build":
                original_solution = original / "solution"
                if original_solution.is_dir():
                    if not solution.is_dir() or not any(path.is_file() for path in solution.rglob("*")):
                        raise ValueError(f"task {task_id} removed or emptied solution/")
                elif solution.exists():
                    raise ValueError(f"task {task_id} added solution/ absent from its baseline")

            old_instruction = (original / "instruction.md").read_text(errors="replace")
            new_instruction = instruction.read_text(errors="replace")
            if len(new_instruction.strip()) < max(40, int(len(old_instruction.strip()) * 0.6)):
                raise ValueError(f"task {task_id} instruction was substantially shortened")

            old_test_files = [path for path in (original / "tests").rglob("*") if path.is_file()]
            new_test_files = [path for path in tests.rglob("*") if path.is_file()]
            if len(new_test_files) < len(old_test_files):
                raise ValueError(f"task {task_id} reduced test file count")
            old_tests = "\n".join(path.read_text(errors="replace") for path in old_test_files)
            new_tests = "\n".join(path.read_text(errors="replace") for path in new_test_files)
            for token in check_tokens:
                if new_tests.lower().count(token) < old_tests.lower().count(token):
                    raise ValueError(
                        f"task {task_id} reduced test guard token {token!r}; "
                        "logic-preserving repair may not weaken verification"
                    )

            original_verifier = original / "verifier"
            staged_verifier = staged / "verifier"
            if original_verifier.is_dir():
                old_files = [path for path in original_verifier.rglob("*") if path.is_file()]
                new_files = [path for path in staged_verifier.rglob("*") if path.is_file()]
                if len(new_files) < len(old_files):
                    raise ValueError(f"task {task_id} reduced verifier file count")
                old_text = "\n".join(path.read_text(errors="replace") for path in old_files)
                new_text = "\n".join(path.read_text(errors="replace") for path in new_files)
                for token in check_tokens:
                    if new_text.lower().count(token) < old_text.lower().count(token):
                        raise ValueError(
                            f"task {task_id} reduced verifier guard token {token!r}; "
                            "path alignment may not weaken verification"
                        )

    def _apply_task_overlay(self, changed_files: set[str]) -> None:
        """Apply only changed controlled roots, preserving hidden solutions."""
        current_ids = _task_ids(self.stream_dir)
        for task_id in current_ids:
            staged = self.workspace / "tasks" / task_id
            target = self.stream_dir / "tasks" / task_id
            if not staged.is_dir() or not target.is_dir():
                raise ValueError(f"missing staged task tree for {task_id}")
            changed_roots = {
                pathlib.PurePosixPath(path).parts[2]
                for path in changed_files
                if path.startswith(f"tasks/{task_id}/")
            }
            for name in sorted(changed_roots):
                src = staged / name
                dst = target / name
                if dst.is_dir():
                    shutil.rmtree(dst)
                elif dst.exists():
                    dst.unlink()
                if src.is_dir():
                    shutil.copytree(src, dst)
                elif src.is_file():
                    shutil.copy2(src, dst)

            # The stream oracle executes the aggregate solution mirror. Refresh
            # it only if a full-stage turn explicitly changed solution/.
            if "solution" not in changed_roots:
                continue
            local_solution = target / "solution"
            oracle_solution = self.stream_dir / "solution" / "tasks" / task_id
            if oracle_solution.is_dir():
                shutil.rmtree(oracle_solution)
            elif oracle_solution.exists():
                oracle_solution.unlink()
            if local_solution.is_dir():
                oracle_solution.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(local_solution, oracle_solution)

    def _apply_solution_overlay(self) -> None:
        """Apply only task-local solutions and refresh the aggregate Oracle bundle."""
        for task_id in _task_ids(self.stream_dir):
            staged = self.workspace / "tasks" / task_id / "solution"
            target = self.stream_dir / "tasks" / task_id / "solution"
            if not staged.is_dir() or not any(path.is_file() for path in staged.rglob("*")):
                raise ValueError(f"task {task_id} removed or emptied solution/")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            shutil.copytree(staged, target)

            oracle_solution = self.stream_dir / "solution" / "tasks" / task_id
            if oracle_solution.is_dir():
                shutil.rmtree(oracle_solution)
            elif oracle_solution.exists():
                oracle_solution.unlink()
            oracle_solution.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target, oracle_solution)

    def _validate_plan(self, payload: dict[str, Any]) -> list[str]:
        task_ids = payload.get("task_ids")
        if not isinstance(task_ids, list) or not all(isinstance(x, str) for x in task_ids):
            raise ValueError("stream_plan.json task_ids must be a list of strings")
        if len(task_ids) != self.state.original_task_count:
            raise ValueError(
                f"replacement changed task count: {len(task_ids)} != {self.state.original_task_count}"
            )
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("replacement contains duplicate task IDs")
        current = set(_task_ids(self.stream_dir))
        requested = set(task_ids)
        unknown = sorted(requested - set(self.all_pool_by_id))
        if unknown:
            raise ValueError(f"replacement uses tasks outside candidate pool: {unknown}")
        explicitly_blocked = sorted(requested & self.explicit_blacklist)
        if explicitly_blocked:
            raise ValueError(
                "replacement still contains explicitly blacklisted tasks: "
                f"{explicitly_blocked}"
            )
        unavailable = sorted((requested - current) - set(self.pool_by_id))
        if unavailable:
            raise ValueError(
                f"replacement uses tasks outside candidate pool: {unavailable}"
            )
        tiers = self._tiers_for(task_ids)
        if tiers != self.state.original_tier_counts:
            raise ValueError(
                f"replacement changed tier mix: {tiers} != {self.state.original_tier_counts}"
            )
        introduced = requested - current
        blocked = {
            task_id for task_id, row in self._replacement_reputation().items()
            if row["replacement_status"] == "avoid"
        }
        poisoned = sorted(introduced & blocked)
        if poisoned:
            raise ValueError(
                "replacement introduced tasks blocklisted by repeated Oracle failures: "
                f"{poisoned}"
            )
        return task_ids

    def _backup_stream(self) -> None:
        dst = self.run_dir / "snapshots" / f"attempt-{self.state.attempt:02d}"
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            # A full assembled stream contains copied task fixtures and
            # solutions and can be several hundred MB.  Keeping one such
            # copy per refine attempt exhausted the shared run-volume quota
            # during the 80-way scan.  The stream can be deterministically
            # reassembled from the task ids in stream.toml and the pool
            # manifest, so retain only the small, non-pool inputs needed to
            # reconstruct or audit an attempt.
            dst.mkdir()
            shutil.copy2(self.stream_dir / "stream.toml", dst / "stream.toml")
            environment = self.stream_dir / "environment"
            if environment.is_dir():
                shutil.copytree(
                    environment,
                    dst / "environment",
                    ignore=shutil.ignore_patterns("_sources"),
                )

    @property
    def _dockerfile_repair_ledger_path(self) -> pathlib.Path:
        return self.run_dir / "dockerfile-repair-ledger.json"

    def _record_dockerfile_repairs(self, before: str, after: str) -> None:
        """Persist exact, security-validated Dockerfile replacements."""
        chunks = _dockerfile_repair_chunks(before, after)
        if not chunks:
            return
        payload: dict[str, Any] = {"version": 1, "repairs": []}
        if self._dockerfile_repair_ledger_path.is_file():
            try:
                payload = json.loads(self._dockerfile_repair_ledger_path.read_text())
            except (OSError, ValueError, TypeError):
                payload = {"version": 1, "repairs": []}
        repairs = payload.setdefault("repairs", [])
        known = {(row.get("before"), row.get("after")) for row in repairs}
        for chunk in chunks:
            key = (chunk["before"], chunk["after"])
            if key in known:
                continue
            repairs.append({
                **chunk,
                "source_attempt": self.state.attempt,
                "source_codex_turn": self.state.codex_turn,
            })
            known.add(key)
        _atomic_json(self._dockerfile_repair_ledger_path, payload)

    def _replay_dockerfile_repairs(self, dockerfile: pathlib.Path) -> dict[str, Any]:
        """Replay still-applicable exact fixes on a freshly assembled file."""
        report: dict[str, Any] = {"applied": [], "already_present": [], "skipped": []}
        if not self._dockerfile_repair_ledger_path.is_file():
            return report
        try:
            repairs = json.loads(self._dockerfile_repair_ledger_path.read_text()).get("repairs", [])
        except (OSError, ValueError, TypeError):
            return report
        baseline = dockerfile.read_text()
        updated = baseline
        for index, repair in enumerate(repairs):
            before = str(repair.get("before", ""))
            after = str(repair.get("after", ""))
            if not before:
                report["skipped"].append({"index": index, "reason": "empty-before"})
                continue
            occurrences = updated.count(before)
            if occurrences == 1:
                updated = updated.replace(before, after, 1)
                report["applied"].append(index)
            elif occurrences == 0 and after and after in updated:
                report["already_present"].append(index)
            else:
                report["skipped"].append({
                    "index": index,
                    "reason": f"before-occurrences={occurrences}",
                })
        _validate_dockerfile_security(updated, baseline)
        dockerfile.write_text(updated)
        return report

    def _reassemble(self, task_ids: list[str]) -> None:
        raw = tomllib.loads((self.stream_dir / "stream.toml").read_text())
        arrivals = raw.get("arrivals", [])
        times = [int(x.get("arrive_wall_time", 0)) for x in arrivals]
        meta, env, session = raw.get("metadata", {}), raw.get("environment", {}), raw.get("session", {})
        refs = [
            TaskRef(
                source=self.pool_by_id[task_id].task_dir,
                pool=self.pool_by_id[task_id].pool.lower(),
                task_id=task_id,
                arrive_wall_time=times[i],
            )
            for i, task_id in enumerate(task_ids)
        ]
        spec = StreamSpec(
            name=str(meta.get("name", self.stream_dir.name)),
            description=str(meta.get("description", "")),
            author=str(meta.get("author", "aos-bench refine loop")),
            cpus=int(env.get("cpus", 4)),
            memory_mb=int(env.get("memory_mb", 8192)),
            allow_internet=bool(env.get("allow_internet", True)),
            max_steps=int(session.get("max_steps", 360)),
            base_image="ubuntu:24.04",
            accept_mixed_from=True,
            extra_apt_packages=[
                "ca-certificates", "curl", "git", "python3", "python3-pip",
                "python-is-python3", "build-essential",
            ],
            tasks=refs,
            shared_prefixes=[],
            pool_v02=False,
        )
        tmp = self.stream_dir.with_name(f".{self.stream_dir.name}.refine-{uuid.uuid4().hex[:8]}")
        assemble(spec, tmp, mirror_profile=self.config.mirror_profile)
        replay_report = self._replay_dockerfile_repairs(tmp / "environment" / "Dockerfile")
        _atomic_json(
            self.logs_dir / f"attempt-{self.state.attempt:02d}" / "reassembly-repair-replay.json",
            replay_report,
        )
        old = self.stream_dir.with_name(f".{self.stream_dir.name}.old-{uuid.uuid4().hex[:8]}")
        self.stream_dir.rename(old)
        try:
            tmp.rename(self.stream_dir)
        except Exception:
            old.rename(self.stream_dir)
            raise
        shutil.rmtree(old)

    def apply_codex_changes(self) -> bool:
        changed_files = self._validate_workspace_edits()
        if not changed_files:
            raise CodexNoOpError(
                "Codex completed without changing an allowed stream/task file; "
                "candidate was not applied"
            )
        if self.config.stage == "solution":
            self._validate_task_overlay_equivalence(changed_files)
            self._write_edit_audit(changed_files)
            self._backup_stream()
            self._apply_solution_overlay()
            self._prepare_workspace()
            return False

        _validate_dockerfile_security(
            (self.workspace / "environment" / "Dockerfile").read_text(),
            (self.stream_dir / "environment" / "Dockerfile").read_text(),
        )
        plan = json.loads((self.workspace / "stream_plan.json").read_text())
        mandatory = sorted(self._current_explicit_blacklist())
        if mandatory and changed_files != {"stream_plan.json"}:
            raise ValueError(
                "current stream contains explicitly blacklisted tasks; edit only "
                f"stream_plan.json and replace all of them first: {mandatory}"
            )
        requested = self._validate_plan(plan)
        current = _task_ids(self.stream_dir)
        changed_tasks = requested != current
        non_plan_changes = changed_files - {"stream_plan.json"}
        if changed_tasks and non_plan_changes:
            raise ValueError(
                "Codex changed task composition and stream/task files in one turn; "
                "replace tasks first, then refine the regenerated task bundle next turn"
            )
        self._validate_task_overlay_equivalence(changed_files)
        self._write_edit_audit(changed_files)
        self._backup_stream()
        if changed_tasks:
            self._reassemble(requested)
        else:
            if "environment/Dockerfile" in changed_files:
                self._record_dockerfile_repairs(
                    (self.stream_dir / "environment" / "Dockerfile").read_text(),
                    (self.workspace / "environment" / "Dockerfile").read_text(),
                )
                shutil.copy2(
                    self.workspace / "environment" / "Dockerfile",
                    self.stream_dir / "environment" / "Dockerfile",
                )
            if any(path.startswith("tasks/") for path in changed_files):
                self._apply_task_overlay(changed_files)
        self._prepare_workspace()
        return changed_tasks

    def verify_build(self) -> VerifierResult:
        attempt_dir = self.logs_dir / f"attempt-{self.state.attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        log_path = attempt_dir / "build.log"
        metadata_path = attempt_dir / "build-metadata.json"
        explicitly_blocked = sorted(self._current_explicit_blacklist())
        if explicitly_blocked:
            log_path.write_text(
                "Explicit merged-stream blacklist gate rejected tasks:\n"
                + "\n".join(explicitly_blocked)
                + "\n"
            )
            return VerifierResult(
                "task-blacklist",
                False,
                f"Stream contains {len(explicitly_blocked)} explicitly blacklisted task(s)",
                {
                    "failed_tasks": explicitly_blocked,
                    "error_signals": [
                        "EXPLICIT_TASK_BLACKLIST: " + ", ".join(explicitly_blocked)
                    ],
                },
                str(log_path),
            )
        dockerfile = self.stream_dir / "environment" / "Dockerfile"
        dockerfile_text = dockerfile.read_text()
        normalized = normalize_domestic_install_sources(dockerfile_text)
        if normalized != dockerfile_text:
            dockerfile.write_text(normalized)
        image_prefix = "aos-v1-0-build-valid" if self.config.stage == "build" else "aos-refine"
        registry = self.config.image_registry.rstrip("/")
        # Docker Hub repositories are two-level names (namespace/repository),
        # so keep all stream images in the requested single repository and
        # encode the stream/image class in the tag.  Other registries get one
        # repository per stream.
        if _is_single_repository(registry):
            repository = registry
            tag = (
                f"{repository}:{image_prefix}-{self.stream_dir.name}-"
                f"a{self.state.attempt}-{uuid.uuid4().hex[:8]}"
            )
        else:
            repository = f"{registry}/{image_prefix}-{self.stream_dir.name}"
            tag = f"{repository}:a{self.state.attempt}-{uuid.uuid4().hex[:8]}"
        proxy = os.environ.get("AOS_BENCH_PROXY", os.environ.get("http_proxy", ""))
        cmd = [
            "docker", "buildx", "build", "--builder", self.config.buildx_builder,
            "--push", "--metadata-file", str(metadata_path),
        ]
        if proxy:
            no_proxy = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
            cmd += [
                "--build-arg", f"HTTP_PROXY={proxy}",
                "--build-arg", f"HTTPS_PROXY={proxy}",
                "--build-arg", f"NO_PROXY={no_proxy}",
            ]
        cmd += ["-t", tag, str(self.stream_dir / "environment")]
        max_build_attempts = max(1, self.config.build_retries + 1)
        transient_retries = 0
        with log_path.open("w") as out:
            for build_attempt in range(1, max_build_attempts + 1):
                out.write(
                    f"\n# AOS build attempt {build_attempt}/{max_build_attempts}\n"
                    + "$ " + " ".join(cmd) + "\n"
                )
                out.flush()
                attempt_log_offset = out.tell()
                with _buildx_concurrency_slot(out):
                    proc = subprocess.Popen(
                        cmd,
                        stdout=out,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    try:
                        returncode = proc.wait(timeout=self.config.build_timeout_sec)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            proc.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            proc.wait()
                        return VerifierResult("build", False, "Docker build timed out", {
                            "timeout_sec": self.config.build_timeout_sec,
                            "build_attempts": build_attempt,
                            "transient_retries": transient_retries,
                            "error_signals": ["BUILD_TIMEOUT"],
                            "infrastructure_failure": True,
                            "failure_class": "build-timeout",
                        }, str(log_path))
                if returncode == 0:
                    break
                out.flush()
                with log_path.open(errors="replace") as current_log:
                    current_log.seek(attempt_log_offset)
                    attempt_tail = current_log.read()[-16000:]
                transient = _is_transient_build_failure(attempt_tail)
                if not transient or build_attempt >= max_build_attempts:
                    return VerifierResult(
                        "build", False, f"Docker build failed (rc={returncode})", {
                            "returncode": returncode,
                            "build_attempts": build_attempt,
                            "transient_retries": transient_retries,
                            "transient_failure": transient,
                            "infrastructure_failure": transient,
                            "failure_class": "build-transport" if transient else "",
                            "error_signals": (
                                _error_signals(attempt_tail)
                                or attempt_tail.splitlines()[-12:]
                            ),
                        }, str(log_path),
                    )
                transient_retries += 1
                out.write(
                    "\n# AOS classified the failure as transport/integrity-only; "
                    "retrying without a Codex turn.\n"
                )
                out.flush()
                if self.config.build_retry_backoff_sec > 0:
                    time.sleep(self.config.build_retry_backoff_sec * build_attempt)
        digest = ""
        try:
            metadata = json.loads(metadata_path.read_text())
            descriptor = metadata.get("containerimage.descriptor") or {}
            digest = str(
                metadata.get("containerimage.digest") or descriptor.get("digest") or ""
            )
        except (OSError, ValueError, TypeError):
            metadata = {}
        if (
            self.config.stage == "build"
            and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            inspect = subprocess.run(
                [
                    "docker", "buildx", "imagetools", "inspect", tag,
                    "--format", "{{json .Manifest}}",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            try:
                digest = str(json.loads(inspect.stdout).get("digest") or "")
            except (ValueError, TypeError):
                digest = ""
        if (
            self.config.stage == "build"
            and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            return VerifierResult(
                "build",
                False,
                "Docker build pushed but immutable registry digest could not be resolved",
                {
                    "image_tag": tag,
                    "infrastructure_failure": True,
                    "failure_class": "registry-digest",
                    "error_signals": ["REGISTRY_DIGEST_UNAVAILABLE"],
                },
                str(log_path),
            )
        image_ref = (
            f"{repository}@{digest}"
            if re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            else tag
        )
        self._set_docker_image(image_ref)
        return VerifierResult(
            "build",
            True,
            f"Docker build passed and pinned: {image_ref}",
            {
                "image": image_ref,
                "image_tag": tag,
                "image_digest": digest or None,
                "metadata_path": str(metadata_path),
                "build_attempts": build_attempt,
                "transient_retries": transient_retries,
            },
            str(log_path),
        )

    def _register_build_valid(self, build: VerifierResult) -> VerifierResult:
        """Atomically apply the overlap gate and register an immutable build.

        Build success is useful evidence even when the candidate is too close
        to an accepted stream, so the immutable image metadata and snapshot are
        always retained below the run directory. Only candidates whose task
        overlap is strictly below the configured threshold enter the shared
        build-valid registry.
        """
        details = build.details or {}
        image_ref = str(details.get("image") or "")
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image_ref):
            raise ValueError(f"build result is not immutable: {image_ref!r}")

        snapshot = self.run_dir / "build-valid-snapshot"
        if snapshot.exists():
            shutil.rmtree(snapshot)
        snapshot.mkdir(parents=True)
        shutil.copy2(self.stream_dir / "stream.toml", snapshot / "stream.toml")
        shutil.copytree(
            self.stream_dir / "environment",
            snapshot / "environment",
            ignore=shutil.ignore_patterns("_sources"),
        )
        dockerfile = self.stream_dir / "environment" / "Dockerfile"
        stream_toml = self.stream_dir / "stream.toml"
        task_ids = _task_ids(self.stream_dir)
        entry = {
            "name": self.stream_dir.name,
            "bucket": self.run_dir.parent.name,
            "stream_dir": str(self.stream_dir),
            "state_path": str(self.state_path),
            "snapshot_dir": str(snapshot),
            "task_ids": task_ids,
            "task_count": len(task_ids),
            "attempt": self.state.attempt,
            "image": image_ref,
            "image_tag": str(details.get("image_tag") or ""),
            "image_digest": str(details.get("image_digest") or ""),
            "dockerfile_sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest(),
            "stream_toml_sha256": hashlib.sha256(stream_toml.read_bytes()).hexdigest(),
            "accepted_at": time.time(),
        }
        _atomic_json(self.run_dir / "build-evidence.json", entry)

        registry = self.build_registry_path
        historical_registry = self.success_registry_path
        registry.parent.mkdir(parents=True, exist_ok=True)
        historical_registry.parent.mkdir(parents=True, exist_ok=True)
        lock_paths = sorted({
            registry.with_suffix(registry.suffix + ".lock"),
            historical_registry.with_suffix(historical_registry.suffix + ".lock"),
        })
        lock_files = [path.open("a+") for path in lock_paths]
        try:
            for lock_file in lock_files:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                payload = json.loads(registry.read_text()) if registry.is_file() else {
                    "schema_version": 1,
                    "selection_note": "Build-valid only; Oracle validity is a separate stage.",
                    "overlap_metric": OVERLAP_METRIC,
                    "max_overlap_ratio_exclusive": self.config.max_overlap_ratio,
                    "streams": [],
                }
            except (OSError, ValueError, TypeError):
                payload = {
                    "schema_version": 1,
                    "selection_note": "Build-valid only; Oracle validity is a separate stage.",
                    "overlap_metric": OVERLAP_METRIC,
                    "max_overlap_ratio_exclusive": self.config.max_overlap_ratio,
                    "streams": [],
                }
            configured_metric = payload.get("overlap_metric", OVERLAP_METRIC)
            configured_threshold = float(
                payload.get("max_overlap_ratio_exclusive", self.config.max_overlap_ratio)
            )
            if configured_metric != OVERLAP_METRIC:
                raise ValueError(f"build registry uses a different overlap metric: {registry}")
            if configured_threshold != self.config.max_overlap_ratio:
                raise ValueError(
                    "build registry overlap threshold mismatch: "
                    f"{configured_threshold} != {self.config.max_overlap_ratio}"
                )
            existing = [
                row for row in payload.get("streams", [])
                if str(row.get("state_path") or "") != str(self.state_path)
            ]
            historical = _load_success_registry(
                historical_registry,
                self.config.max_overlap_ratio,
            )
            comparison_rows = [
                ("historical-valid", row) for row in historical["streams"]
                if str(row.get("state_path") or "") != str(self.state_path)
            ] + [("current-build-valid", row) for row in existing]
            conflicts: list[dict[str, Any]] = []
            for source, row in comparison_rows:
                other_tasks = row.get("task_ids")
                if not isinstance(other_tasks, list) or not other_tasks:
                    raise ValueError(
                        f"overlap registry contains invalid task IDs: {source} "
                        f"{row.get('name')!r}"
                    )
                overlap = _task_overlap(task_ids, [str(task) for task in other_tasks])
                if overlap["ratio"] >= self.config.max_overlap_ratio:
                    conflicts.append({
                        "source": source,
                        "name": str(row.get("name") or "unknown"),
                        "state_path": str(row.get("state_path") or ""),
                        **overlap,
                    })
            conflicts.sort(
                key=lambda row: (-float(row["ratio"]), row["source"], row["name"])
            )
            overlap_details: dict[str, Any] = {
                "overlap_metric": OVERLAP_METRIC,
                "max_overlap_ratio_exclusive": self.config.max_overlap_ratio,
                "current_task_ids": task_ids,
                "historical_registry_path": str(historical_registry),
                "build_registry_path": str(registry),
                "historical_stream_count": len(historical["streams"]),
                "current_build_valid_count": len(existing),
                "conflicts": conflicts,
                "build_evidence": entry,
            }
            if conflicts:
                result = VerifierResult(
                    "overlap",
                    False,
                    (
                        "Build overlap gate failed: "
                        f"{len(conflicts)} conflict(s); max={conflicts[0]['ratio']:.3f}, "
                        f"required < {self.config.max_overlap_ratio:.3f}"
                    ),
                    overlap_details,
                )
                _atomic_json(
                    self.logs_dir / f"attempt-{self.state.attempt:02d}" / "overlap.json",
                    asdict(result),
                )
                return result
            payload["overlap_metric"] = OVERLAP_METRIC
            payload["max_overlap_ratio_exclusive"] = self.config.max_overlap_ratio
            payload["streams"] = existing + [entry]
            payload["updated_at"] = time.time()
            _atomic_json(registry, payload)
            result = VerifierResult(
                "overlap",
                True,
                (
                    "Build overlap gate passed: max pairwise overlap is strictly "
                    f"below {self.config.max_overlap_ratio:.3f}"
                ),
                {**overlap_details, "registered_entry": entry},
            )
            _atomic_json(
                self.logs_dir / f"attempt-{self.state.attempt:02d}" / "overlap.json",
                asdict(result),
            )
        finally:
            for lock_file in reversed(lock_files):
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()
        _atomic_json(self.run_dir / "build-valid.json", entry)
        return result

    def _set_docker_image(self, image: str) -> None:
        path = self.stream_dir / "stream.toml"
        text = path.read_text()
        if re.search(r"^docker_image\s*=", text, re.MULTILINE):
            text = re.sub(r'^docker_image\s*=\s*"[^"]*"', f'docker_image   = "{image}"', text, flags=re.MULTILINE)
        else:
            text, count = re.subn(
                r"(allow_internet\s*=\s*[^\n]+\n)",
                rf'\1docker_image   = "{image}"\n', text, count=1,
            )
            if count != 1:
                raise ValueError("could not add docker_image to stream.toml")
        path.write_text(text)

    def verify_oracle(self) -> VerifierResult:
        attempt_dir = self.logs_dir / f"attempt-{self.state.attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        log_path = attempt_dir / "oracle.log"
        jobs = self.jobs_dir / f"attempt-{self.state.attempt:02d}"
        jobs.mkdir(parents=True, exist_ok=True)
        cmd = [
            "uv", "run", "--directory", str(REPO_ROOT / "harbor"), "harbor", "run",
            "--path", str(self.stream_dir), "--task-type", "stream",
            "--env", self.config.oracle_env,
        ]
        # v0.1 is harbor's default and takes no flag. v0.2/v0.3 are opt-in.
        if self.config.oracle_pool_version in ("v02", "v03"):
            cmd.append(f"--aos-pool-{self.config.oracle_pool_version}")
        cmd += [
            "--agent", "oracle", "--jobs-dir", str(jobs),
            "--job-name", f"refine-a{self.state.attempt}", "--n-attempts", "1",
            "--n-concurrent", "1", "--agent-setup-timeout-multiplier", "5.0",
            "--environment-build-timeout-multiplier", "6.0", "--yes",
        ]
        env = os.environ.copy()
        env["AOS_VERIFIER_CONCURRENCY"] = str(self.config.verifier_concurrency)
        # Only the v0.3 dispatcher writes to a host log path; setting it for the
        # other pool versions is harmless but misleading in the oracle log.
        if self.config.oracle_pool_version == "v03":
            env.setdefault(
                "HOST_DISPATCHER_LOGS_PATH",
                str(self.run_dir.parents[2] / "dispatcher"),
            )
        # v0.1/v0.2 upload the pool source from the AOS-Bench checkout, which
        # harbor locates by walking up from its own __file__ for a sibling
        # `aos_bench/pool`. Under an editable install that sibling does not
        # exist, so the path has to be passed explicitly.
        if self.config.oracle_pool_version in ("v01", "v02"):
            env.setdefault("AOS_POOL_DIR", str(REPO_ROOT / "aos_bench" / "pool"))
        if self.config.job_queue:
            env["HARBOR_JOB_QUEUE"] = self.config.job_queue
        with log_path.open("w") as out:
            out.write("$ " + " ".join(cmd) + "\n")
            out.flush()
            try:
                proc = subprocess.run(
                    cmd, env=env, stdout=out, stderr=subprocess.STDOUT,
                    timeout=self.config.oracle_timeout_sec,
                )
            except subprocess.TimeoutExpired:
                return VerifierResult("oracle", False, "Harbor oracle timed out", {
                    "timeout_sec": self.config.oracle_timeout_sec,
                    "failed_tasks": [],
                    "candidate_tasks": _task_ids(self.stream_dir),
                    "error_signals": ["ORACLE_TIMEOUT"],
                    "infrastructure_failure": True,
                    "failure_class": "oracle-timeout",
                }, str(log_path))
        rewards = sorted(jobs.rglob("verifier/reward.json"), key=lambda p: p.stat().st_mtime)
        if not rewards:
            signals = _error_signals(_tail(log_path))
            joined = "\n".join(signals).lower()
            if "healthz" in joined or "is not running" in joined:
                failure_class = "dispatcher-health"
            elif (
                "addtestsdirerror" in joined
                or "docker compose cp" in joined
                or "no such pod" in joined
                or "no such container" in joined
            ):
                failure_class = "docker-copy"
            else:
                failure_class = "oracle-no-reward"
            return VerifierResult("oracle", False, "Oracle produced no reward.json", {
                "returncode": proc.returncode,
                "failed_tasks": [],
                "candidate_tasks": _task_ids(self.stream_dir),
                "error_signals": signals,
                "infrastructure_failure": True,
                "failure_class": failure_class,
            }, str(log_path))
        reward_path = rewards[-1]
        values = json.loads(reward_path.read_text())
        expected = _task_ids(self.stream_dir)
        missing = [task_id for task_id in expected if task_id not in values]
        failed = [task_id for task_id in expected if float(values.get(task_id, 0)) < 1.0]
        signals: dict[str, list[str]] = {}
        verifier_root = reward_path.parent
        for task_id in failed:
            stdout = verifier_root / task_id / "stdout.txt"
            found = _error_signals(_tail(stdout, 12000))
            if found:
                signals[task_id] = found
        oracle_logs = sorted(reward_path.parents[1].glob("agent/oracle.txt"))
        oracle_signals = _error_signals(_tail(oracle_logs[-1], 20000)) if oracle_logs else []
        passed = (
            proc.returncode == 0
            and not failed
            and not missing
            and len(values) == len(expected)
        )
        details = {
            "reward_path": str(reward_path),
            "rewards": values,
            "failed_tasks": failed,
            "missing_tasks": missing,
            "task_error_signals": signals,
            "oracle_error_signals": oracle_signals,
            "returncode": proc.returncode,
        }
        summary = f"Oracle {'fully passed' if passed else 'failed'}: {len(expected) - len(failed)}/{len(expected)}"
        return VerifierResult("oracle", passed, summary, details, str(log_path))

    def verify_overlap(self, oracle: VerifierResult) -> VerifierResult:
        """Atomically accept a fully solved stream only below the overlap cap."""
        if not oracle.passed:
            raise ValueError("overlap verification requires a fully passed Oracle result")
        current = _task_ids(self.stream_dir)
        if not current or len(current) != len(set(current)):
            raise ValueError("overlap verification requires non-empty unique task IDs")
        registry = self.success_registry_path
        registry.parent.mkdir(parents=True, exist_ok=True)
        lock_path = registry.with_suffix(registry.suffix + ".lock")
        identity = str(self.state_path.resolve())
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            payload = _load_success_registry(
                registry,
                self.config.max_overlap_ratio,
                self.config.max_successful_streams,
            )
            existing = [
                row for row in payload["streams"]
                if str(row.get("state_path", "")) != identity
            ]
            conflicts: list[dict[str, Any]] = []
            for row in existing:
                overlap = _task_overlap(current, row["task_ids"])
                if overlap["ratio"] >= self.config.max_overlap_ratio:
                    conflicts.append({
                        "name": str(row.get("name") or "unknown"),
                        "state_path": str(row.get("state_path") or ""),
                        **overlap,
                    })
            conflicts.sort(key=lambda row: (-row["ratio"], row["name"]))
            details: dict[str, Any] = {
                "success_registry_path": str(registry),
                "overlap_metric": OVERLAP_METRIC,
                "max_overlap_ratio_exclusive": self.config.max_overlap_ratio,
                "max_successful_streams": self.config.max_successful_streams,
                "current_task_ids": current,
                "accepted_stream_count": len(existing),
                "conflicts": conflicts,
            }
            target_reached = (
                self.config.max_successful_streams is not None
                and len(existing) >= self.config.max_successful_streams
            )
            if target_reached:
                result = VerifierResult(
                    "success-target",
                    False,
                    (
                        "Success target already reached: "
                        f"{len(existing)}/{self.config.max_successful_streams}"
                    ),
                    details,
                )
            elif conflicts:
                result = VerifierResult(
                    "overlap",
                    False,
                    (
                        "Overlap gate failed: "
                        f"{len(conflicts)} accepted stream conflict(s); "
                        f"max={conflicts[0]['ratio']:.3f}, required "
                        f"<{self.config.max_overlap_ratio:.3f}"
                    ),
                    details,
                )
            else:
                entry = {
                    "name": self.stream_dir.name,
                    "bucket": self.run_dir.parent.name,
                    "stream_dir": str(self.stream_dir),
                    "state_path": identity,
                    "task_ids": current,
                    "task_count": len(current),
                    "attempt": self.state.attempt,
                    "accepted_at": time.time(),
                    "oracle_reward_path": str(
                        oracle.details.get("reward_path") or ""
                    ),
                }
                payload["streams"] = existing + [entry]
                payload["updated_at"] = time.time()
                _atomic_json(registry, payload)
                details["accepted_stream_count"] = len(payload["streams"])
                details["registry_entry"] = entry
                result = VerifierResult(
                    "overlap",
                    True,
                    (
                        "Overlap gate passed: "
                        f"accepted #{len(payload['streams'])}, required "
                        f"<{self.config.max_overlap_ratio:.3f}"
                    ),
                    details,
                )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        attempt_dir = self.logs_dir / f"attempt-{self.state.attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        log_path = attempt_dir / "overlap.json"
        result.log_path = str(log_path)
        _atomic_json(log_path, asdict(result))
        return result

    def _feedback_prompt(self, result: VerifierResult) -> str:
        infrastructure_failure = _infrastructure_failure_class(result)
        failed = [] if infrastructure_failure else result.details.get("failed_tasks", [])
        for task_id in failed:
            self.state.failure_counts[task_id] = self.state.failure_counts.get(task_id, 0) + 1
        replacement_recommended = [
            task_id for task_id in failed
            if self.state.failure_counts.get(task_id, 0) >= 2
            or any("/solution/" in line for line in result.details.get("oracle_error_signals", []))
        ]
        replacement_recommended = sorted(
            set(replacement_recommended)
            | self._current_explicit_blacklist()
        )
        evidence = json.dumps({
            "layer": result.layer,
            "summary": result.summary,
            "details": result.details,
            "infrastructure_failure": infrastructure_failure,
            "replacement_recommended": replacement_recommended,
        }, indent=2)
        if self.config.stage == "build":
            live_playbook = _load_refine_prompt_playbook()
            return f"""BUILD-VALID STAGE FEEDBACK - FAIL

Attempt: {self.state.attempt}
Sanitized evidence:
{evidence}

The only goal in this stage is to produce a reproducible runtime image that
builds and can be pushed to the configured registry. Inspect the current
logs/attempt-{self.state.attempt:02d}/build.log and make exactly one small,
evidence-backed logical repair. A path-alignment repair may touch multiple
files when that is necessary to keep the same task contract coherent.

Allowed edits are environment/Dockerfile, stream_plan.json, and the current
tasks' instruction.md, tests/**, and verifier/**. Solution files are hidden and
immutable in this stage. If Dockerfile changes a task-visible path, working
directory, command, or entrypoint, search instruction, tests, and verifier for
the old value and update every logically related reference in the SAME turn.
Those edits may align paths and invocation/packaging glue only: preserve task
meaning, all deliverables and constraints, expected semantic values, assertion
coverage, verifier strength, tolerances, and pass/fail behavior. Never delete,
skip, relax, or bypass a test or verifier check.

A task replacement must be the only change in the turn and must preserve task
count, L2/L3 mix, uniqueness, and the blacklist. Treat a pure timeout, registry
error, proxy failure, or transport failure as infrastructure: make no candidate
edit for it. Do not copy tests/verifier assets into the runtime image and never
add solutions, expected outputs, answer material, hidden fixtures, or verifier
logic to the image. Do not run Oracle and do not claim Oracle validity.

Apply these historical verifier-confirmed repair lessons only when the current
first-causal build evidence matches them:
{live_playbook}

Additional build-stage rules learned from prior attempts:
- Diagnose the first causal error, not downstream missing artifacts. Missing
  Python.h/compiler/runtime/header errors require the compatible final-image
  toolchain; Python 3.12 incompatibility may instead require a compatible
  version or isolated venv, without globally breaking other tasks.
- Prefer deterministic domestic HTTPS mirrors and finite retries. Do not use
  unbounded retry loops or interpret a mirror/registry outage as task failure.
- Preserve the base image and task runtime contract unless current evidence
  requires a change. Never mix Debian repositories into Ubuntu.
- Do not repeat the same unsuccessful repair hypothesis. Inspect prior attempt
  logs and the Dockerfile repair replay audit before editing.
- Before finishing, inspect git diff and confirm every changed file is allowed,
  path references are synchronized, and test/verifier semantics are unchanged.
"""
        if self.config.stage == "solution":
            return f"""SOLUTION-ONLY ORACLE STAGE FEEDBACK - FAIL

Attempt: {self.state.attempt}
Immutable image: {getattr(self, 'solution_image_identity', self._configured_stream_image())}
Queue runtime tag: {getattr(self, 'solution_runtime_image', self._configured_stream_image())}
Sanitized evidence:
{evidence}

The build-valid image and task contract are frozen. Inspect the current
logs/attempt-{self.state.attempt:02d}/oracle.log and Oracle artifacts, then make
one evidence-backed edit under the failing current task's solution/** only.

Do not edit environment/Dockerfile, stream_plan.json, instruction, tests, or
verifier, and do not replace tasks. You may make the solution self-contained by
using task-local temporary directories, isolated virtual environments, or
independent runtime dependency installation. Preserve the original algorithm
and expected result. Avoid shared /workspace build trees and shared /tmp output
names when tasks can run concurrently. Never fabricate expected output, weaken
verification, or copy tests/verifier material into the solution.
"""
        live_playbook = _load_refine_prompt_playbook()
        overlap_directive = ""
        if result.layer == "overlap":
            overlap_directive = f"""

OVERLAP GATE DIRECTIVE:
- Build and Oracle already passed. Do not edit the Dockerfile, instruction,
  tests, or solution in response to this failure.
- Edit only stream_plan.json and replace tasks so the next candidate has
  {OVERLAP_METRIC} strictly below {self.config.max_overlap_ratio:.3f} against
  every accepted stream listed in the verifier evidence and diversity section.
- Equality is a failure: for a 20-task stream at 0.60, 12 shared tasks fails and
  at most 11 shared tasks passes.
- Preserve task count, tier counts, uniqueness, blacklist, and task semantics.
- Prefer underused tasks with exact Oracle evidence. Do not remove a reliable
  task unless doing so reduces a named overlap conflict.
"""
        return f"""STREAM VERIFIER FEEDBACK - FAIL

Attempt: {self.state.attempt}
Sanitized evidence:
{evidence}
{overlap_directive}

Full redacted diagnostics are available read-only under logs/. For the current
attempt, inspect logs/attempt-{self.state.attempt:02d}/build.log or oracle.log
and, when present, oracle-artifacts/reward.json, oracle.txt, and per-task
verifier/*/stdout.txt. Use older attempts only to confirm that the same causal
signature repeated. Never edit logs or treat an old error as current evidence.
Also inspect reassembly-repair-replay.json when present. Do not recreate a repair
that the replay audit says is already applied, stale, ambiguous, or inapplicable.

You are repairing benchmark packaging, not changing what the tasks mean. Work
only from the sanitized evidence and the controlled task bundle in this
workspace. Make exactly one small, reversible, evidence-backed repair.

Decision procedure (follow in order):
0. First rule out infrastructure-only evidence. HTTP 401/402/429, upstream 5xx,
   quota/authentication errors, and model/proxy transport failures are not task
   faults. If no concrete build or Oracle failure precedes them, make no semantic
   task or Dockerfile change. The controller must retry or pause the loop.
1. Classify the FIRST causal failure, not every downstream symptom, as one of:
   (a) missing dependency/toolchain, (b) incompatible dependency versions,
   (c) merged workspace/path/output layout, (d) solution/test syntax or
   invocation glue damaged by packaging, (e) partial reward despite otherwise
   successful execution, (f) timeout/transient infrastructure, or (g) an
   individual task that repeatedly cannot run safely in the merged stream.
   In past merged streams, (c) and (d) have been more common than true
   cross-task version conflicts. Do not assume that two tasks conflict merely
   because a package, command, source file, or expected output is missing.
2. For (a) or (b), repair environment/Dockerfile. If that changes a path or
   runtime contract named by a task, update that task's instruction.md,
   tests/**, and solution/** in the SAME turn so they remain consistent.
3. For (c) or (d), repair the affected task bundle only when the change is a
   packaging/path correction and preserves the original behavior, expected
   result, test strength, and solution algorithm.
4. For (e), do not weaken the verifier or round/normalize the reward to 1.0.
   Fully passed requires an exact reward of 1.0 for every task. If the original
   task/verifier intrinsically produces a fractional reward and no equivalent
   packaging repair can restore 1.0, replace the task.
5. For (f), do not rewrite or replace a task based only on timeout,
   CancelledError, a missing reward.json, or a proxy/transport error. Look for
   a concrete preceding task or Docker error; without one, preserve task
   content and avoid speculative semantic changes.
6. Compare the current causal signature and repair class with prior attempts.
   If the same repair class has already failed twice for the same causal
   signature, do not repeat it: change the hypothesis or replace the task.
   For (g), for a current candidate whose row says replacement_status=avoid,
   or when replacement_recommended names a repeatedly failing task, replace it
   by editing only stream_plan.json's task_ids array. Replacement must be the
   only action in that turn and must keep the same tier mix.
7. If the evidence is insufficient, make the smallest Dockerfile fix that
   addresses the concrete error; do not guess, redesign, or make unrelated
   cleanup changes.

Observed failure signatures and the correct response:
The following verifier-confirmed playbook is loaded from disk on every REFINE
turn. Apply an item only when the current first-causal evidence matches it:
{live_playbook}

- "command not found", ModuleNotFoundError, missing headers such as Python.h,
  failed native-wheel builds, or an absent compiler/runtime normally mean a
  missing dependency or toolchain. Install it in the final execution image,
  not only in a temporary build stage. Do not blindly downgrade, uninstall, or
  globally replace packages used by other tasks. If two requirements are
  genuinely incompatible, prefer replacing one task over breaking the rest.
- For Ubuntu multiarch, arm64 package indexes must come from ubuntu-ports;
  never request binary-arm64 from a regular Ubuntu mirror. Guard rewrites of
  both /etc/apt/sources.list and deb822 .sources files by existence, and never
  mix Debian repositories or signing keys into an Ubuntu base image.
- Old Python packages on Python 3.12 can fail because compatible metadata or
  wheels do not exist. This is semantic compatibility, not mirror transport:
  do not repeat mirror retries or globally downgrade unrelated packages.
- Do not relocate, move, or symlink vllm before confirming it is installed and
  importable in the final runtime stage.
- For npm monorepos, inspect workspace scripts and dependency order. Do not
  blindly run every workspace's build script; use --if-present only when the
  skipped script is genuinely optional under the original task contract.
- A missing expected output is usually a downstream symptom. First find why
  the solution command did not run or where it wrote the file. Never satisfy
  the verifier by copying, fabricating, or embedding the expected output.
- Paths rooted at /workspace, /app, /output, relative source imports, and
  nested project roots often become invalid after merge. Choose one coherent
  task-local/runtime path mapping and update Dockerfile plus every affected
  instruction, test, solution, entrypoint, and import consistently.
- SyntaxError, Python future-import placement errors, test collection errors,
  or truncated scripts usually indicate unsafe concatenation/prepending or
  path-rewrite damage. Keep each task's scripts isolated; do not concatenate
  task Python/shell sources, and never place generated code before Python
  module docstrings or from __future__ imports.
- A solve script may exit successfully while writing to the wrong directory,
  and a verifier may then report many missing files. Repair the earliest
  invocation/path error once rather than patching each missing-file assertion.
- A timeout waiting for an artifact such as /tmp/frame.bmp is not permission
  to fabricate it. Repair the earliest real producer or invocation instead.
- Revideo TypeScript errors and browser "Target closed" errors require
  dependency, workspace, resource, or lifecycle diagnosis; never respond by
  changing expected output or weakening tests.
- Several unrelated failures in one oracle run do not justify a broad image or
  task rewrite. Pick one causal cluster and make one minimal repair this turn.

Repair checklist for Dockerfile changes:
- Preserve the base image, task entrypoints, environment variables, and oracle
  invocation unless the failure evidence directly requires a change.
- Prefer deterministic, pinned or already-used package sources and compatible
  versions; avoid network-heavy or non-reproducible installation steps.
- Fix the first causal error in the build/runtime log, not downstream symptoms.
- Confirm dependencies are available to the user, PATH, working directory, and
  runtime stage used by the oracle; a successful image build alone is not
  evidence that the oracle environment can import or execute them.
- Do not copy, generate, or embed task answers, expected outputs, solution
  files, tests, verifier code, oracle scripts, or hidden fixtures.

Task-bundle equivalence contract:
- instruction.md may clarify or synchronize paths/commands, but must preserve
  every deliverable, constraint, success condition, and prohibited behavior.
- tests/** may update paths, setup, imports, or packaging glue, but must not
  remove assertions, relax tolerances, broaden accepted outputs, skip cases,
  reduce coverage, or change expected semantic values.
- solution/** may update paths, dependencies, or invocation glue, but must
  preserve the original algorithm and produced result. Never copy solution
  content into the Docker image or instruction.
- Keep task-local instruction, tests, and solution mutually consistent. The
  driver mirrors task-local solution/** into the aggregate oracle bundle.
- Treat a changed absolute path as a task contract change: search all four
  surfaces (Dockerfile, instruction, tests, and solution) for the old path and
  update every logically related reference in the same turn.
- Preserve file boundaries, shebangs, executable bits, source encodings,
  module docstrings, and language ordering rules while rewriting scripts.
- Do not edit tasks unrelated to the concrete verifier evidence.

Repair checklist for task replacement:
- Replace only task_ids; do not edit task content or task logic.
- Use only IDs from candidate_pool, no duplicates, exactly the original task
  count, and exactly the original L2/L3 tier counts.
- Never introduce an ID in replacement_blocklist. Prefer eligible candidates
  with observed exact Oracle passes. Among candidates with comparable
  reliability, prefer the lowest accepted_usage from candidate_pool so fully
  solved streams do not converge on the same task set. Inspect
  diversity.current_overlap_by_accepted_stream before choosing. If a tier has
  only enough reliable tasks to fill its quota, prefer safely repairing an
  underused task over immediately recreating an already accepted stream.
  Use an unknown candidate only when no reliable same-tier candidate is
  available and the evidence supports exploring it.
- Prefer a task with a distinct failure signature and comparable resource
  requirements. Replacement is appropriate for an intrinsically fractional
  reward, a genuine irreconcilable dependency conflict, or a repeated unsafe
  packaging failure; it must not be used to conceal a solvable logic failure
  or to weaken benchmark coverage.
- After replacement, let the driver regenerate the merged Dockerfile; repair
  that regenerated Dockerfile in a later turn if the next evidence requires it.

Before finishing, inspect your diff and verify that edits are limited to
environment/Dockerfile, current tasks' instruction.md/tests/**/solution/**, or
stream_plan.json. Confirm that expected semantic values, verifier strength,
reward expectations, and task logic are unchanged.
Do not claim success; the driver will rebuild the image and rerun the oracle.

Do not combine replacement and Dockerfile edits in one turn. Replacement
regenerates the merged Dockerfile; refine that new Dockerfile on the next turn.

Task replacement rules are enforced by the driver: preserve task count and
L2/L3 counts, use candidate_pool IDs only, and do not use duplicates. Prefer
replacement when feedback is solution-packaging, verifier-contract, or a task
has failed repeatedly and cannot be fixed safely in Dockerfile.

Security contract:
- Task-local tests and solution are visible only for logically equivalent
  packaging repair. Do not expose them through the image, instruction, logs,
  generated fixtures, or new helper files.
- Never add expected outputs, answer files, solve helpers, test fixtures, or
  verifier/oracle material to the runtime image.
- Do not change task logic or weaken verification.
- logs/** is read-only diagnostic evidence and is never an allowed edit target.
- Only the explicitly allowed files described above may be edited.

Inspect the controlled workspace and make the smallest justified change.
"""

    def _codex_args(self) -> list[str]:
        return [
            "-c", "model_provider=glm",
            "-c", "model_providers.glm.name=glm",
            "-c", f"model_providers.glm.base_url={self.config.api_base}",
            "-c", "model_providers.glm.wire_api=responses",
            "-c", "model_providers.glm.env_key=GLM_API_KEY",
            "--model", self.config.model,
        ]

    def _container_exists(self) -> bool:
        proc = subprocess.run(
            ["docker", "container", "inspect", self.codex_container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0

    def _container_queue(self) -> str | None:
        proc = subprocess.run(
            [
                "docker", "container", "inspect", self.codex_container,
                "--format", '{{index .Config.Labels "' + _JOB_QUEUE_LABEL + '"}}',
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            return None
        value = proc.stdout.strip()
        return "" if value in {"", "<no value>"} else value

    def _container_uses_default_placement(self) -> bool:
        proc = subprocess.run(
            [
                "docker", "container", "inspect", self.codex_container,
                "--format", '{{index .Config.Labels "aos.refine.codex-placement"}}',
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "default"

    def _codex_queue(self) -> str:
        """Return the queue label for Codex's remote Docker container."""
        if self.config.codex_job_queue is None:
            return self.config.job_queue
        return self.config.codex_job_queue

    def _create_codex_container(self) -> None:
        """Create the proxy container with bounded serialized retries."""
        cmd = ["docker", "run", "-d", "--name", self.codex_container]
        codex_queue = self._codex_queue()
        if codex_queue:
            cmd += [
                "--label",
                f"{_JOB_QUEUE_LABEL}={codex_queue}",
            ]
        else:
            cmd += ["--label", "aos.refine.codex-placement=default"]
        cmd += [self.config.codex_image, "sleep", "infinity"]
        last_error = ""
        lock_path = pathlib.Path("/tmp/aos-refine-codex-start.lock")
        with lock_path.open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            for start_attempt in range(20):
                started = subprocess.run(
                    cmd,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if started.returncode == 0:
                    break
                last_error = (started.stderr or started.stdout or "").strip()[-800:]
                subprocess.run(
                    ["docker", "rm", "-f", self.codex_container],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if start_attempt == 19:
                    raise RuntimeError(
                        f"Codex container startup failed after 20 retries: {last_error}"
                    )
                time.sleep(1.0 + random.random() * 2.0)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _container_state_summary(self) -> str:
        proc = subprocess.run(
            [
                "docker", "container", "inspect", self.codex_container,
                "--format",
                "status={{.State.Status}} exit={{.State.ExitCode}} error={{.State.Error}}",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        return (proc.stderr or proc.stdout or "container inspect failed").strip()[-800:]

    def _ensure_codex_container(self) -> None:
        """Start an isolated Codex container and restore its persisted home."""
        codex_queue = self._codex_queue()
        existing_queue = self._container_queue()
        queue_mismatch = (
            not self._container_uses_default_placement()
            if not codex_queue
            else existing_queue != codex_queue
        )
        if self._container_exists() and queue_mismatch:
            subprocess.run(
                ["docker", "rm", "-f", self.codex_container],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        reused_container = self._container_exists()
        if reused_container:
            started = subprocess.run(
                ["docker", "start", self.codex_container],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if started.returncode:
                subprocess.run(
                    ["docker", "rm", "-f", self.codex_container],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                reused_container = False
                self._create_codex_container()
        else:
            self._create_codex_container()
        try:
            self._wait_for_codex_container()
        except RuntimeError as first_error:
            if not reused_container:
                raise
            # A proxy may acknowledge ``docker start`` while continuing to
            # expose the old terminal container state. Recreate that stale
            # identity once; the Codex home is persisted separately on host.
            subprocess.run(
                ["docker", "rm", "-f", self.codex_container],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            self._create_codex_container()
            try:
                self._wait_for_codex_container()
            except RuntimeError as second_error:
                raise RuntimeError(
                    "Codex container remained unavailable after stale-container "
                    f"recreation; initial={first_error}; recreated={second_error}"
                ) from second_error
        # The Docker proxy may restore the container identity without its
        # prior writable layer after an idle stop. Recreate and restore the
        # session directory on every start, not only after docker run.
        subprocess.run(
            ["docker", "exec", self.codex_container, "mkdir", "-p", "/refine/codex_home"],
            check=True,
        )
        if self.codex_home.is_dir() and any(self.codex_home.iterdir()):
            subprocess.run(
                ["docker", "cp", f"{self.codex_home}/.", f"{self.codex_container}:/refine/codex_home/"],
                check=True,
            )

    def _wait_for_codex_container(self, timeout_sec: int | None = None) -> None:
        """Wait for the remote Docker-proxy container to accept exec calls."""
        if timeout_sec is None:
            timeout_sec = self.config.codex_container_ready_timeout_sec
        deadline = time.monotonic() + timeout_sec
        terminal_probes = 0
        last_probe_error = ""
        state = "unknown"
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["docker", "exec", self.codex_container, "true"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if probe.returncode == 0:
                return
            last_probe_error = (probe.stderr or probe.stdout or "").strip()[-800:]
            state = self._container_state_summary()
            if state.startswith(("status=exited ", "status=dead ")):
                terminal_probes += 1
                if terminal_probes >= 5:
                    raise RuntimeError(
                        f"Codex container {self.codex_container} stayed terminal "
                        f"during readiness checks: {state}; exec={last_probe_error}"
                    )
            else:
                terminal_probes = 0
            time.sleep(1)
        raise RuntimeError(
            f"Codex container {self.codex_container} did not become ready "
            f"within {timeout_sec}s; {state}; exec={last_probe_error}"
        )

    def _stage_container_workspace(self) -> None:
        self._ensure_codex_container()
        def run_staging_step(
            cmd: list[str], label: str, timeout_sec: int = 600
        ) -> None:
            last_error = ""
            for attempt in range(5):
                try:
                    proc = subprocess.run(
                        cmd,
                        text=True,
                        capture_output=True,
                        timeout=timeout_sec,
                    )
                except subprocess.TimeoutExpired:
                    last_error = f"timed out after {timeout_sec}s"
                    if attempt < 4:
                        self._wait_for_codex_container()
                        time.sleep(1.0 + random.random() * 2.0)
                    continue
                if proc.returncode == 0:
                    return
                last_error = (proc.stderr or proc.stdout or "").strip()[-800:]
                if attempt < 4:
                    self._wait_for_codex_container()
                    time.sleep(1.0 + random.random() * 2.0)
            raise RuntimeError(
                f"Codex workspace {label} failed after 5 retries: {last_error}"
            )

        run_staging_step(
            [
                "docker", "exec", "-u", "0", self.codex_container, "sh", "-c",
                "rm -rf /refine/workspace && mkdir -p /refine/workspace",
            ],
            "cleanup",
        )
        # Docker-proxy ``docker cp`` sends an uncompressed tar stream. Large
        # task contracts (notably repository-backed verifier bundles) can make
        # that stream hundreds of megabytes and leave the proxy blocked with
        # no progress. Compress once on the host, transfer one archive, then
        # extract it in the remote container.
        archive = self.run_dir / f".codex-workspace-{uuid.uuid4().hex[:8]}.tar.gz"
        container_archive = "/refine/codex-workspace.tar.gz"
        parts_dir = self.run_dir / f".codex-workspace-parts-{uuid.uuid4().hex[:8]}"
        container_parts = "/refine/codex-workspace-parts"
        try:
            run_staging_step(
                [
                    "tar", "-C", str(self.workspace), "--exclude=./.git",
                    "-czf", str(archive), ".",
                ],
                "archive",
                timeout_sec=1200,
            )
            parts_dir.mkdir()
            subprocess.run(
                [
                    # The host Docker proxy reliably accepts <=1 MiB cp
                    # payloads; larger single copies can block after reading
                    # the complete source without returning.
                    "split", "-b", "1m", str(archive),
                    str(parts_dir / "part-"),
                ],
                check=True,
                timeout=1200,
            )
            run_staging_step(
                [
                    "docker", "exec", "-u", "0", self.codex_container,
                    "sh", "-c", f"rm -rf {container_parts} && mkdir -p {container_parts}",
                ],
                "prepare archive parts",
            )
            for part in sorted(parts_dir.glob("part-*")):
                run_staging_step(
                    [
                        "docker", "cp", str(part),
                        f"{self.codex_container}:{container_parts}/{part.name}",
                    ],
                    f"copy archive part {part.name}",
                    timeout_sec=180,
                )
            run_staging_step(
                [
                    "docker", "exec", "-u", "0", self.codex_container,
                    "sh", "-c",
                    f"cat {container_parts}/part-* > {container_archive} && "
                    f"rm -rf {container_parts} && "
                    f"tar -xzf {container_archive} -C /refine/workspace && "
                    f"rm -f {container_archive}",
                ],
                "assemble and extract archive",
                timeout_sec=1200,
            )
        finally:
            archive.unlink(missing_ok=True)
            if parts_dir.is_dir():
                for part in parts_dir.glob("part-*"):
                    part.unlink(missing_ok=True)
                parts_dir.rmdir()
        # ``docker cp`` preserves host ownership (normally root on this
        # runner), while Codex runs as the unprivileged ``agent`` user.
        # Normalize ownership so the agent can directly edit the allowlisted
        # files without renaming directories or creating escape-like links.
        run_staging_step(
            [
                "docker", "exec", "-u", "0", self.codex_container,
                "chown", "-R", "1001:1001", "/refine/workspace",
            ],
            "chown",
        )
        # Recreate a compact baseline repository in the container instead of
        # transferring the host repository's duplicated object database. The
        # authoritative host-side edit audit uses .refine-baseline.json; this
        # repository exists only to give Codex a useful `git diff` view.
        run_staging_step(
            [
                "docker", "exec", "-u", "1001:1001", self.codex_container,
                "sh", "-c",
                "cd /refine/workspace && git init -q && "
                "git config user.email refine@aos && "
                "git config user.name aos-refine && "
                "git add . && git commit -q -m baseline",
            ],
            "initialize baseline repository",
            timeout_sec=1200,
        )
        # Diagnostic logs are context, never an edit surface. Root ownership
        # plus read-only modes prevent the unprivileged Codex process from
        # rewriting evidence to bypass the workspace change audit.
        run_staging_step(
            [
                "docker", "exec", "-u", "0", self.codex_container, "sh", "-c",
                "if [ -d /refine/workspace/logs ]; then "
                "chown -R 0:0 /refine/workspace/logs && "
                "find /refine/workspace/logs -type d -exec chmod 0555 {} + && "
                "find /refine/workspace/logs -type f -exec chmod 0444 {} +; fi",
            ],
            "protect diagnostic logs",
        )

    def _collect_container_state(self) -> None:
        """Copy allowlisted stream/task edits and Codex session state to host."""
        editable = _editable_workspace_roots(
            _task_ids(self.stream_dir), self.workspace, self.config.stage
        )
        archive_name = f"agent-edits-{uuid.uuid4().hex[:8]}.tar.gz"
        container_edits = f"/refine/{archive_name}"
        host_edits = self.run_dir / f".{archive_name}"
        staged_edits = self.run_dir / f".agent-edits-{uuid.uuid4().hex[:8]}"
        staged_edits.mkdir()
        try:
            # Symlinks are unnecessary for the allowlisted task contracts and
            # could redirect extraction or later hashing outside the staged
            # tree. Reject them before creating the archive.
            links = subprocess.run(
                [
                    "docker", "exec", self.codex_container, "find",
                    *[f"/refine/workspace/{path}" for path in editable],
                    "-type", "l", "-print",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if links.returncode:
                raise RuntimeError(
                    "Codex editable workspace roots are missing or unreadable: "
                    + links.stderr.strip()[-800:]
                )
            if links.stdout.strip():
                raise ValueError(
                    "Codex created forbidden symlinks in editable files: "
                    + links.stdout.strip()[:800]
                )
            subprocess.run(
                [
                    "docker", "exec", self.codex_container, "tar",
                    "-C", "/refine/workspace", "-czf", container_edits,
                    *editable,
                ],
                check=True,
            )
            subprocess.run(
                ["docker", "cp", f"{self.codex_container}:{container_edits}", str(host_edits)],
                check=True,
            )
            subprocess.run(
                ["tar", "-xzf", str(host_edits), "-C", str(staged_edits)],
                check=True,
            )
            for relative in editable:
                source = staged_edits / relative
                if not source.exists():
                    raise ValueError(f"Codex removed required editable path: {relative}")
            for relative in editable:
                source = staged_edits / relative
                destination = self.workspace / relative
                if destination.is_dir():
                    shutil.rmtree(destination)
                elif destination.exists():
                    destination.unlink()
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
        finally:
            subprocess.run(
                ["docker", "exec", self.codex_container, "rm", "-f", container_edits],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            host_edits.unlink(missing_ok=True)
            if staged_edits.exists():
                shutil.rmtree(staged_edits)

        staged_home = self.codex_home.with_name(
            f".{self.codex_home.name}.copy-{uuid.uuid4().hex[:8]}"
        )
        archive_name = f"codex-home-{uuid.uuid4().hex[:8]}.tar.gz"
        container_archive = f"/refine/{archive_name}"
        host_archive = self.run_dir / f".{archive_name}"
        staged_home.mkdir(parents=True)
        try:
            subprocess.run(
                [
                    "docker", "exec", self.codex_container, "tar",
                    "--exclude=./.tmp", "-C", "/refine/codex_home",
                    "-czf", container_archive, ".",
                ],
                check=True,
            )
            subprocess.run(
                ["docker", "cp", f"{self.codex_container}:{container_archive}", str(host_archive)],
                check=True,
            )
            subprocess.run(
                ["tar", "-xzf", str(host_archive), "-C", str(staged_home)],
                check=True,
            )
            if self.codex_home.exists():
                shutil.rmtree(self.codex_home)
            staged_home.rename(self.codex_home)
        finally:
            subprocess.run(
                ["docker", "exec", self.codex_container, "rm", "-f", container_archive],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            host_archive.unlink(missing_ok=True)
            if staged_home.exists():
                shutil.rmtree(staged_home)

    def run_codex(self, prompt: str) -> tuple[int, str]:
        env = os.environ.copy()
        if "GLM_API_KEY" not in env and "OPENAI_API_KEY" in env:
            env["GLM_API_KEY"] = env["OPENAI_API_KEY"]
        if not env.get("GLM_API_KEY"):
            raise RuntimeError("GLM_API_KEY (or OPENAI_API_KEY) is required")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self._sync_diagnostic_logs()
        self._stage_container_workspace()
        self.state.codex_turn += 1
        turn = self.state.codex_turn
        prompt_path = self.logs_dir / f"turn-{turn:02d}-prompt.txt"
        prompt_path.write_text(prompt)
        # ``codex-refine`` is a durable transaction boundary.  Publish it
        # only after the exact prompt needed for recovery is on disk; a
        # worker killed between verifier completion and this point can then
        # safely rerun the verifier instead of entering an unrecoverable
        # prompt-less Codex state.
        self.state.status = "codex-refine"
        self._save_state()
        log_path = self.logs_dir / f"turn-{turn:02d}-codex.jsonl"
        container_codex_args = self._codex_args()

        def command(session_id: str | None) -> list[str]:
            if session_id:
                codex_cmd = ["codex", "exec", "resume"] + container_codex_args + [
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-c", 'projects."/refine/workspace".trust_level=trusted',
                    "--json", session_id, prompt,
                ]
            else:
                codex_cmd = ["codex", "exec"] + container_codex_args + [
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--skip-git-repo-check", "--json", prompt,
                ]
            cmd = ["docker", "exec", "-w", "/refine/workspace"]
            for key in (
                "GLM_API_KEY", "http_proxy", "https_proxy", "HTTP_PROXY",
                "HTTPS_PROXY", "NO_PROXY",
            ):
                if env.get(key):
                    # `docker exec -e KEY` forwards the value from this
                    # process environment without embedding credentials in
                    # argv, where they would be visible through `ps`.
                    cmd += ["-e", key]
            cmd += ["-e", "CODEX_HOME=/refine/codex_home", self.codex_container]
            return cmd + codex_cmd

        def execute(session_id: str | None) -> int | None:
            with log_path.open("w") as out:
                try:
                    proc = subprocess.run(
                        command(session_id), env=env, stdout=out,
                        stderr=subprocess.STDOUT,
                        timeout=self.config.codex_timeout_sec,
                    )
                except subprocess.TimeoutExpired:
                    return None
            return proc.returncode

        resumed_session = self.state.session_id
        returncode = execute(resumed_session)
        if returncode is None:
            return 124, str(prompt_path)
        text = log_path.read_text(errors="replace")
        if (
            returncode != 0
            and resumed_session
            and "no rollout found for thread id" in text.lower()
        ):
            # Gateway retention can expire while the durable local REFINE
            # state still contains a once-valid thread ID.  Retry this same
            # verifier turn as a fresh session instead of burning REFINE
            # rounds on a permanently unresumable ID.
            self.state.session_id = None
            self.state.final_reason = ""
            self._save_state()
            # The CLI may also consult its local SQLite/session cache when a
            # fresh command is started.  Clear that cache in both locations so
            # the retry cannot silently select the expired thread again.
            if self.codex_home.exists():
                shutil.rmtree(self.codex_home)
            self.codex_home.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "docker", "exec", "-u", "0", self.codex_container, "sh", "-c",
                    "rm -rf /refine/codex_home && mkdir -p /refine/codex_home",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            returncode = execute(None)
            if returncode is None:
                return 124, str(prompt_path)
            text = log_path.read_text(errors="replace")
        violations = _audit_codex_commands(text, pathlib.Path("/refine/workspace"))
        if violations:
            self.state.session_id = None
            self.state.status = "security-violation"
            self.state.final_reason = " | ".join(violations[:4])
            self._save_state()
            raise RuntimeError(
                "Codex attempted to leave the sanitized workspace: "
                + " | ".join(violations[:4])
            )
        self._collect_container_state()
        if not self.state.session_id:
            self.state.session_id = _extract_session_id(text)
            if not self.state.session_id:
                raise RuntimeError(f"Codex returned no thread_id; see {log_path}")
        self._save_state()
        return returncode, str(prompt_path)

    def _recover_pending_codex(self) -> int | None:
        """Resume/apply a Codex transaction interrupted between state writes."""
        if self.state.status == "codex-complete":
            try:
                self.apply_codex_changes()
            except Exception as exc:
                # A completed transaction can still contain a no-op or an
                # invalid candidate.  Recovery must not retry that same
                # durable boundary forever; give the validation error back to
                # the same Codex session exactly as the normal run path does.
                invalid = VerifierResult(
                    "candidate-validation",
                    False,
                    str(exc),
                    {"error_signals": [str(exc)]},
                )
                if self.state.codex_turn >= self.config.max_rounds:
                    self.state.status = "failed-max-rounds"
                    self.state.final_reason = str(exc)
                    self._save_state()
                    return 1
                try:
                    rc, _ = self.run_codex(self._feedback_prompt(invalid))
                except Exception as retry_exc:
                    self.state.status = "codex-error"
                    self.state.final_reason = str(retry_exc)
                    self._save_state()
                    return 2
                if rc:
                    self.state.status = "codex-error"
                    self.state.final_reason = (
                        f"Codex corrective turn {self.state.codex_turn} rc={rc}"
                    )
                    self._save_state()
                    return 2
                self.state.status = "codex-complete"
                self._save_state()
                try:
                    self.apply_codex_changes()
                except Exception as retry_exc:
                    self.state.status = "codex-error"
                    self.state.final_reason = (
                        "Recovered Codex candidate remained invalid after one "
                        f"corrective turn: {retry_exc}"
                    )
                    self._save_state()
                    return 2
            self.state.attempt += 1
            self.state.status = "candidate-applied"
            self._save_state()
            return None
        if self.state.status != "codex-refine":
            return None
        log_path = self.logs_dir / f"turn-{self.state.codex_turn:02d}-codex.jsonl"
        text = log_path.read_text(errors="replace") if log_path.is_file() else ""
        if not self.state.session_id:
            self.state.session_id = _extract_session_id(text)
        prompt_path = self.logs_dir / f"turn-{self.state.codex_turn:02d}-prompt.txt"
        if '"type":"turn.completed"' in text:
            self.state.status = "codex-complete"
            self._save_state()
            return self._recover_pending_codex()
        if "no rollout found for thread id" in text.lower():
            # Some gateways retain a thread ID from ``thread.started`` even
            # when the failed turn never created a resumable rollout.  Do not
            # replay that poisoned session forever: no completed candidate
            # exists, so start the next verifier-driven turn in a fresh
            # session.
            self.state.session_id = None
            self.state.status = "candidate-applied"
            self.state.final_reason = ""
            self._save_state()
            return None
        if not prompt_path.is_file():
            # Older workers wrote ``codex-refine`` before entering
            # ``run_codex``.  No prompt means no model turn began and no
            # candidate edits exist to recover, so fall back to a fresh
            # verifier pass without consuming a REFINE turn.
            self.state.status = "candidate-applied"
            self.state.final_reason = ""
            self._save_state()
            return None
        # The prior turn did not complete. Re-submit the same verifier feedback
        # to the recovered session (or start a new session when thread.started
        # was never emitted).
        rc, _ = self.run_codex(prompt_path.read_text())
        if rc:
            self.state.status = "codex-error"
            self.state.final_reason = (
                f"recovered Codex turn {self.state.codex_turn} rc={rc}"
            )
            self._save_state()
            return 2
        self.state.status = "codex-complete"
        self._save_state()
        return self._recover_pending_codex()

    def run(self) -> int:
        if self.config.dry_run:
            print(f"dry-run initialized: {self.run_dir}")
            print(f"workspace: {self.workspace}")
            return 0
        recovered = self._recover_pending_codex()
        if recovered is not None:
            return recovered
        while True:
            record = RoundRecord(attempt=self.state.attempt)
            if self.config.stage == "solution":
                image = getattr(
                    self, "solution_image_identity", self._configured_stream_image()
                )
                runtime_image = getattr(
                    self, "solution_runtime_image", self._configured_stream_image()
                )
                build = VerifierResult(
                    "build",
                    True,
                    f"Using immutable build-valid image: {image}",
                    {
                        "image": image,
                        "runtime_image": runtime_image,
                        "runtime_tag_digest_verified": True,
                        "build_skipped": True,
                    },
                )
            else:
                self.state.status = "verifying-build"
                self._save_state()
                build = self.verify_build()
            record.build = asdict(build)
            if build.passed:
                if self.config.stage == "build":
                    self.state.status = "verifying-overlap"
                    self._save_state()
                    overlap = self._register_build_valid(build)
                    record.overlap = asdict(overlap)
                    self.state.rounds.append(asdict(record))
                    self.state.infrastructure_failure_streak = 0
                    self.state.last_infrastructure_failure = ""
                    if not overlap.passed:
                        self.state.status = "failed-overlap-build"
                        self.state.final_reason = overlap.summary
                        self._save_state()
                        print(overlap.summary)
                        return 1
                    entry = overlap.details["registered_entry"]
                    self.state.status = "build-passed"
                    self.state.final_reason = (
                        f"Build-valid image frozen: {entry['image']}"
                    )
                    self._save_state()
                    print(self.state.final_reason)
                    return 0
                self.state.status = "verifying-oracle"
                self._save_state()
                oracle = self.verify_oracle()
                record.oracle = asdict(oracle)
                if oracle.passed:
                    self.state.status = "verifying-overlap"
                    self._save_state()
                    overlap = self.verify_overlap(oracle)
                    record.overlap = asdict(overlap)
                    verdict = overlap
                else:
                    verdict = oracle
            else:
                verdict = build
            infrastructure_failure = _infrastructure_failure_class(verdict)
            record.infrastructure_retry = infrastructure_failure
            self.state.rounds.append(asdict(record))
            self._save_state()
            print(verdict.summary)
            if verdict.passed:
                self.state.infrastructure_failure_streak = 0
                self.state.last_infrastructure_failure = ""
                self.state.status = "passed"
                self.state.final_reason = verdict.summary
                self._save_state()
                return 0
            if verdict.layer == "success-target":
                self.state.status = "stopped-target-reached"
                self.state.final_reason = verdict.summary
                self._save_state()
                return 0
            if self.config.verify_only:
                self.state.status = "failed"
                self.state.final_reason = verdict.summary
                self._save_state()
                return 1
            if self.config.stage == "solution" and verdict.layer == "overlap":
                self.state.status = "failed-overlap-fixed-image"
                self.state.final_reason = verdict.summary
                self._save_state()
                return 1
            if infrastructure_failure:
                self.state.infrastructure_failure_streak += 1
                self.state.infrastructure_failure_total += 1
                self.state.last_infrastructure_failure = infrastructure_failure
                if (
                    self.state.infrastructure_failure_streak
                    > self.config.max_infrastructure_retries
                ):
                    self.state.status = "paused-infrastructure"
                    self.state.final_reason = (
                        f"{infrastructure_failure} repeated "
                        f"{self.state.infrastructure_failure_streak} time(s): "
                        f"{verdict.summary}"
                    )
                    self._save_state()
                    return 75
                self.state.status = "retrying-infrastructure"
                self.state.final_reason = (
                    f"retrying {infrastructure_failure} "
                    f"({self.state.infrastructure_failure_streak}/"
                    f"{self.config.max_infrastructure_retries})"
                )
                self.state.attempt += 1
                self._save_state()
                if self.config.infrastructure_retry_backoff_sec:
                    time.sleep(
                        self.config.infrastructure_retry_backoff_sec
                        * self.state.infrastructure_failure_streak
                    )
                continue
            self.state.infrastructure_failure_streak = 0
            self.state.last_infrastructure_failure = ""
            self._save_state()
            if self.state.codex_turn >= self.config.max_rounds:
                self.state.status = "failed-max-rounds"
                self.state.final_reason = verdict.summary
                self._save_state()
                return 1
            prompt = self._feedback_prompt(verdict)
            try:
                rc, prompt_path = self.run_codex(prompt)
            except Exception as exc:
                self.state.status = "codex-error"
                self.state.final_reason = str(exc)
                self._save_state()
                return 2
            record.codex_turn = self.state.codex_turn
            record.codex_rc = rc
            record.prompt_path = prompt_path
            if rc != 0:
                self.state.status = "codex-error"
                self.state.final_reason = f"Codex turn {self.state.codex_turn} rc={rc}"
                self._save_state()
                return 2
            self.state.status = "codex-complete"
            self._save_state()
            try:
                record.changed_tasks = self.apply_codex_changes()
            except Exception as exc:
                # Invalid edits become structured feedback in the same session
                # instead of terminating the loop.
                invalid = VerifierResult("candidate-validation", False, str(exc), {
                    "error_signals": [str(exc)],
                })
                self.state.rounds[-1] = asdict(record)
                self._save_state()
                if self.state.codex_turn >= self.config.max_rounds:
                    return 1
                rc, _ = self.run_codex(self._feedback_prompt(invalid))
                if rc:
                    return 2
                self.state.status = "codex-complete"
                self._save_state()
                try:
                    record.changed_tasks = self.apply_codex_changes()
                except Exception as retry_exc:
                    self.state.status = "codex-error"
                    self.state.final_reason = (
                        "Codex candidate remained invalid after one corrective turn: "
                        f"{retry_exc}"
                    )
                    self.state.rounds[-1] = asdict(record)
                    self._save_state()
                    return 2
            self.state.rounds[-1] = asdict(record)
            self.state.attempt += 1
            self.state.status = "candidate-applied"
            self._save_state()
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream-dir", required=True, type=pathlib.Path)
    parser.add_argument("--run-dir", required=True, type=pathlib.Path)
    parser.add_argument("--max-rounds", type=int, default=8)
    parser.add_argument("--build-timeout-sec", type=int, default=3600)
    parser.add_argument(
        "--build-retries", type=int, default=2,
        help="retry transport/integrity-only Docker build failures before Codex",
    )
    parser.add_argument("--build-retry-backoff-sec", type=float, default=5.0)
    parser.add_argument(
        "--max-infrastructure-retries",
        type=int,
        default=2,
        help=(
            "retry infrastructure-only verifier failures without Codex before "
            "pausing the candidate"
        ),
    )
    parser.add_argument(
        "--infrastructure-retry-backoff-sec",
        type=float,
        default=30.0,
    )
    parser.add_argument("--oracle-timeout-sec", type=int, default=7200)
    parser.add_argument("--codex-timeout-sec", type=int, default=3600)
    parser.add_argument("--codex-container-ready-timeout-sec", type=int, default=120)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--codex-image", default=DEFAULT_CODEX_IMAGE)
    parser.add_argument("--job-queue", default=DEFAULT_JOB_QUEUE)
    parser.add_argument(
        "--codex-job-queue",
        default=None,
        help=(
            "queue label for Codex containers; omit to reuse --job-queue, or "
            "pass an empty value to use the proxy's default placement"
        ),
    )
    parser.add_argument("--builder", default=os.environ.get("AOS_BENCH_BUILDX_BUILDER", "cluster"))
    parser.add_argument("--image-registry", default=DEFAULT_IMAGE_REGISTRY)
    parser.add_argument("--summary", type=pathlib.Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--blacklist", type=pathlib.Path, default=DEFAULT_BLACKLIST)
    parser.add_argument("--whitelist", type=pathlib.Path)
    parser.add_argument("--mirror-profile", default="upstream", choices=["tuna", "tencent", "huawei", "aliyun", "upstream"])
    parser.add_argument("--verifier-concurrency", type=int, default=1)
    parser.add_argument(
        "--oracle-env",
        default=os.environ.get("AOS_REFINE_ORACLE_ENV", "docker"),
        choices=["docker"],
        help="harbor environment for the oracle verifier",
    )
    parser.add_argument(
        "--oracle-pool-version",
        default=os.environ.get("AOS_REFINE_ORACLE_POOL", "v03"),
        choices=["v01", "v02", "v03"],
        help=(
            "pool transport for the oracle; v03 needs a sibling dispatcher "
            "container on a docker network"
        ),
    )
    parser.add_argument(
        "--max-overlap-ratio",
        type=float,
        default=DEFAULT_MAX_OVERLAP_RATIO,
        help=(
            "exclusive intersection/min-task-count limit for registering a "
            "fully solved stream"
        ),
    )
    parser.add_argument(
        "--success-registry",
        type=pathlib.Path,
        help=(
            "shared accepted-stream registry; defaults to successful-streams.json "
            "under the sweep results root"
        ),
    )
    parser.add_argument(
        "--build-registry",
        type=pathlib.Path,
        help=(
            "shared immutable build-valid image registry; defaults to "
            "build-valid-streams.json under the run root"
        ),
    )
    parser.add_argument(
        "--stage",
        choices=["full", "build", "solution"],
        default="full",
        help=(
            "full runs build+Oracle+overlap; build stops after pushing and "
            "pinning an image; solution skips build and permits solution-only edits"
        ),
    )
    parser.add_argument(
        "--max-successful-streams",
        type=int,
        help=(
            "global registry capacity; candidates reaching the gate after the "
            "capacity is full stop without another Codex turn"
        ),
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = LoopConfig(
        stream_dir=args.stream_dir,
        run_dir=args.run_dir,
        max_rounds=args.max_rounds,
        build_timeout_sec=args.build_timeout_sec,
        build_retries=args.build_retries,
        build_retry_backoff_sec=args.build_retry_backoff_sec,
        max_infrastructure_retries=args.max_infrastructure_retries,
        infrastructure_retry_backoff_sec=args.infrastructure_retry_backoff_sec,
        oracle_timeout_sec=args.oracle_timeout_sec,
        codex_timeout_sec=args.codex_timeout_sec,
        codex_container_ready_timeout_sec=args.codex_container_ready_timeout_sec,
        model=args.model,
        api_base=args.api_base,
        codex_image=args.codex_image,
        job_queue=args.job_queue,
        codex_job_queue=args.codex_job_queue,
        buildx_builder=args.builder,
        image_registry=args.image_registry,
        summary_path=args.summary,
        blacklist_path=args.blacklist,
        whitelist_path=args.whitelist,
        mirror_profile=args.mirror_profile,
        verifier_concurrency=args.verifier_concurrency,
        oracle_env=args.oracle_env,
        oracle_pool_version=args.oracle_pool_version,
        max_overlap_ratio=args.max_overlap_ratio,
        max_successful_streams=args.max_successful_streams,
        success_registry_path=args.success_registry,
        build_registry_path=args.build_registry,
        stage=args.stage,
        resume=args.resume,
        dry_run=args.dry_run,
        verify_only=args.verify_only,
    )
    return StreamRefineLoop(config).run()
