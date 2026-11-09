"""Run the pinned public AutomationBench task set through the GLM gateway.

AutomationBench is an in-process stateful tool benchmark rather than a Harbor
filesystem task.  This adapter deliberately preserves the upstream runner and
assertion rubric while standardizing source pinning, credentials, and result
layout for AOS-Bench experiments.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from aos_bench.task_construction.scripts.download_sources import SOURCES, prepare_source


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_NAME = "automationbench-public"
SOURCE_REVISION = SOURCES[SOURCE_NAME]["revision"]
DEFAULT_SOURCE_DIR = REPO_ROOT / "data/harbor/automationbench-public"
DEFAULT_LOG_ROOT = Path(
    os.environ.get(
        "AUTOMATIONBENCH_LOG_ROOT",
        str(Path.home() / "aos-bench-logs/automationbench-public"),
    )
)


def _source_revision(source_dir: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip()


def prepare_public_source(source_dir: Path, *, refresh: bool = False) -> None:
    """Clone the pinned public source when using the canonical cache path."""
    if refresh or not source_dir.is_dir():
        if source_dir != DEFAULT_SOURCE_DIR:
            raise ValueError(
                f"custom --source-dir does not exist or cannot be refreshed: {source_dir}"
            )
        prepare_source(SOURCE_NAME, source_dir.parent, force_refresh=refresh)
    revision = _source_revision(source_dir)
    if revision != SOURCE_REVISION:
        raise ValueError(
            f"AutomationBench source revision mismatch: expected {SOURCE_REVISION}, "
            f"found {revision}; use --refresh-source"
        )


def build_upstream_command(args: argparse.Namespace, run_dir: Path) -> list[str]:
    """Build a secret-free upstream CLI command."""
    num_examples = 1 if args.smoke else args.num_examples
    max_concurrent = 1 if args.smoke else args.max_concurrent
    command = [
        "uv",
        "run",
        "--project",
        str(args.source_dir),
        "--frozen",
        "auto-bench",
        "--model",
        args.model,
        "--base-url",
        args.base_url,
        "--api-key-var",
        args.api_key_var,
        "--api",
        args.api,
        "--domains",
        args.domains,
        "--toolset",
        args.toolset,
        "--num-examples",
        str(num_examples),
        "--max-steps",
        str(args.max_steps),
        "--max-concurrent",
        str(max_concurrent),
        "--save-every",
        str(args.save_every),
        "--export-json",
        str(run_dir / "results.json"),
    ]
    if args.reasoning_effort:
        command.extend(["--reasoning-effort", args.reasoning_effort])
    if args.tasks:
        command.extend(["--tasks", args.tasks])
    if args.skip:
        command.extend(["--skip", str(args.skip)])
    if args.search_top_k is not None:
        command.extend(["--search-top-k", str(args.search_top_k)])
    if args.smoke or not args.ensure_complete:
        command.append("--no-ensure-complete")
    return command


def _default_run_dir(model: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    model_slug = model.replace("/", "-")
    return DEFAULT_LOG_ROOT / f"{timestamp}-{model_slug}"


def _runtime_env(api_key_var: str) -> dict[str, str]:
    env = os.environ.copy()
    if not env.get(api_key_var) and env.get("GLM_API_KEY"):
        env[api_key_var] = env["GLM_API_KEY"]
    if not env.get(api_key_var):
        raise RuntimeError(
            f"missing {api_key_var}; set it directly or provide GLM_API_KEY"
        )
    return env


def _write_metadata(
    path: Path,
    *,
    args: argparse.Namespace,
    command: list[str],
    started_at: float,
) -> None:
    metadata = {
        "schema_version": 1,
        "benchmark": "zapier/AutomationBench",
        "task_set": "public",
        "source_revision": SOURCE_REVISION,
        "started_at": started_at,
        "started_at_utc": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
        "model": args.model,
        "base_url": args.base_url,
        "api": args.api,
        "domains": args.domains,
        "toolset": args.toolset,
        "reasoning_effort": args.reasoning_effort,
        "smoke": args.smoke,
        "command": command,
    }
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def _run_and_tee(command: list[str], *, cwd: Path, env: dict[str, str], log: Path) -> int:
    with log.open("w") as output:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            output.write(line)
            output.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
        return proc.wait()


def validate_results(path: Path) -> list[str]:
    """Return infrastructure/completeness problems, independent of task score."""
    if not path.is_file():
        return [f"missing result export: {path}"]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return [f"invalid result export: {exc}"]
    summary = data.get("summary") or {}
    problems: list[str] = []
    if int(summary.get("tasks_with_errors") or 0):
        problems.append(f"tasks_with_errors={summary['tasks_with_errors']}")
    if int(summary.get("tasks_with_zero_output") or 0):
        problems.append(f"tasks_with_zero_output={summary['tasks_with_zero_output']}")
    if not data.get("tasks"):
        problems.append("result export contains no tasks")
    return problems


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--refresh-source", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--model", default=os.environ.get("AUTOMATIONBENCH_MODEL", "gpt-5.6-sol"))
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "AUTOMATIONBENCH_BASE_URL",
            os.environ.get("SYNTH_BASE_URL", "https://llm-gateway.example.com/v1"),
        ),
    )
    parser.add_argument("--api-key-var", default="OPENAI_API_KEY")
    parser.add_argument(
        "--api",
        choices=["auto", "anthropic", "chat_completions", "responses"],
        default="responses",
    )
    parser.add_argument("--domains", default="public")
    parser.add_argument("--toolset", choices=["api", "zapier", "limited_zapier"], default="api")
    parser.add_argument("--num-examples", type=int, default=-1)
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--max-concurrent", type=int, default=40)
    parser.add_argument("--save-every", type=int, default=1)
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        default="xhigh",
    )
    parser.add_argument("--tasks")
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--search-top-k", type=int)
    parser.add_argument(
        "--ensure-complete",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--smoke", action="store_true", help="run one public task serially")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prepare_public_source(args.source_dir, refresh=args.refresh_source)
    subprocess.run(
        ["uv", "sync", "--project", str(args.source_dir), "--locked"],
        check=True,
        cwd=args.source_dir,
    )
    if args.prepare_only:
        print(f"AutomationBench public source ready: {args.source_dir}@{SOURCE_REVISION}")
        return 0

    run_dir = args.run_dir or _default_run_dir(args.model)
    run_dir.mkdir(parents=True, exist_ok=False)
    command = build_upstream_command(args, run_dir)
    started_at = time.time()
    _write_metadata(run_dir / "run.json", args=args, command=command, started_at=started_at)
    print(f"AutomationBench public run: {run_dir}")
    print("command:", " ".join(command))
    if args.dry_run:
        return 0

    env = _runtime_env(args.api_key_var)
    upstream_rc = _run_and_tee(
        command,
        cwd=args.source_dir,
        env=env,
        log=run_dir / "runner.log",
    )
    health_problems = validate_results(run_dir / "results.json")
    rc = upstream_rc if upstream_rc else (3 if health_problems else 0)
    if health_problems:
        print("AutomationBench result health check failed:", "; ".join(health_problems))
    finished_at = time.time()
    metadata_path = run_dir / "run.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.update(
        {
            "finished_at": finished_at,
            "finished_at_utc": datetime.fromtimestamp(finished_at, timezone.utc).isoformat(),
            "duration_seconds": finished_at - started_at,
            "upstream_returncode": upstream_rc,
            "returncode": rc,
            "health_problems": health_problems,
        }
    )
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
