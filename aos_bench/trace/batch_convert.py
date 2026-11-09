"""Walk a jobs/run dir and write AgentTrace v0.1 from harness-native logs.

Supports kimi-cli, kimi-code, opencode, claude-code and codex; trials with no
captured runtime data, or an agent kind the detector cannot type, are skipped.

Usage:
    python -m aos_bench.trace.batch_convert <root_dir> [--force]

``<root_dir>`` is typically a harbor jobs-dir or a single job-dir. Every
sub-directory that contains an ``agent/`` is treated as one trial. The
converter is best-effort per trial: failures are logged and counted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

from aos_bench.trace.claude_code import collect_claude_code_agent_trace
from aos_bench.trace.codex import collect_codex_agent_trace
from aos_bench.trace.kimi_cli import (
    collect_kimi_cli_agent_trace,
    find_kimi_cli_latest_session_dir,
)
from aos_bench.trace.kimi_code import (
    collect_kimi_code_agent_trace,
    find_kimi_code_latest_session_dir,
)
from aos_bench.trace.opencode import collect_opencode_agent_trace
from aos_bench.visualization.agent_trace import write_agent_trace_svg


AgentKind = Literal[
    "kimi-cli", "kimi-code", "opencode", "claude-code", "codex", "unknown"
]

# `KIMI_CODE_HOME` inside the container, relative to the mounted agent dir —
# must stay in step with `_HOME_DIRNAME` in
# `harbor/src/harbor/agents/installed/kimi_code.py` and `HOME_DIRNAME` in
# `aos_bench/monitor/tail/kimi_code.py`.
_KIMI_CODE_HOME_DIRNAME = "kimi-code-home"


def detect_agent_kind(agent_dir: Path) -> AgentKind:
    # kimi-code before kimi-cli, "most specific first" (the two markers cannot
    # actually collide: `kimi-code-home` vs `kimi-share`). Unlike the monitor's
    # classifier — which types a trial on the bare home dir so an install
    # failure still gets reported — the backfill requires `sessions/`, because
    # with no session there is nothing to convert.
    if (agent_dir / _KIMI_CODE_HOME_DIRNAME / "sessions").is_dir():
        return "kimi-code"
    if (agent_dir / "kimi-share" / "sessions").is_dir():
        return "kimi-cli"
    if (agent_dir / "opencode.jsonl").is_file() or (
        agent_dir / "opencode.txt"
    ).is_file():
        return "opencode"
    if (agent_dir / "claude-code.txt").is_file() or any(
        (agent_dir / "sessions" / "projects").glob("*/*.jsonl")
    ):
        return "claude-code"
    if any((agent_dir / "sessions").glob("[0-9][0-9][0-9][0-9]/*/*/rollout-*.jsonl")):
        return "codex"
    return "unknown"


def convert_trial(
    trial_dir: Path, *, force: bool = False, render_svg: bool = False
) -> tuple[bool, str]:
    agent_dir = trial_dir / "agent"
    if not agent_dir.is_dir():
        return False, "no agent/ dir"

    out_path = agent_dir / "agent_trace.json"
    if out_path.exists() and not force:
        return False, "agent_trace.json already exists (use --force)"

    kind = detect_agent_kind(agent_dir)
    if kind == "unknown":
        return False, "unknown agent kind"
    trace_id = f"backfill_{trial_dir.parent.name}_{trial_dir.name}"

    try:
        if kind == "kimi-cli":
            session_dir = find_kimi_cli_latest_session_dir(
                agent_dir / "kimi-share", work_dir=Path("/workspace")
            )
            trace = collect_kimi_cli_agent_trace(
                session_dir,
                trace_id=trace_id,
                model_name=None,
                agent_version="backfill",
            )
        elif kind == "kimi-code":
            # Resolution is by `session_index.jsonl` (falling back to a glob of
            # `sessions/wd_*/session_*`) rather than by work-dir key: the key
            # hashes the *agent's* cwd, which a backfill of someone else's job
            # dir does not reliably know.
            kc_session_dir = find_kimi_code_latest_session_dir(
                agent_dir / _KIMI_CODE_HOME_DIRNAME
            )
            if kc_session_dir is None:
                return False, "kimi-code: no session dir under kimi-code-home"
            trace = collect_kimi_code_agent_trace(
                kc_session_dir, trace_id=trace_id, model_name=None, agent_version="backfill"
            )
        elif kind == "opencode":
            jsonl = agent_dir / "opencode.jsonl"
            if not jsonl.is_file():
                jsonl = agent_dir / "opencode.txt"
            trace = collect_opencode_agent_trace(
                jsonl, trace_id=trace_id, model_name=None, agent_version="backfill"
            )
        elif kind == "claude-code":
            trace = collect_claude_code_agent_trace(
                trial_dir, trace_id=trace_id, model_name=None, agent_version="backfill"
            )
        else:  # codex
            trace = collect_codex_agent_trace(
                trial_dir, trace_id=trace_id, model_name=None, agent_version="backfill"
            )
    except Exception as exc:
        return False, f"{kind} converter raised: {type(exc).__name__}: {exc}"

    try:
        out_path.write_text(
            json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if render_svg:
            write_agent_trace_svg(trace, agent_dir / "agent_trace.svg")
    except OSError as exc:
        return False, f"write failed: {exc}"

    n_runs = len(trace.get("runs", []))
    n_events = sum(len(r.get("events", [])) for r in trace.get("runs", []))
    suffix = " + SVG" if render_svg else ""
    return True, f"{kind} → {n_runs} runs, {n_events} events{suffix}"


def find_trial_dirs(root: Path) -> list[Path]:
    """Return trial dirs at any Harbor job nesting depth."""
    if (root / "agent").is_dir():
        return [root]
    return sorted(
        {agent_dir.parent for agent_dir in root.rglob("agent") if agent_dir.is_dir()}
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path)
    p.add_argument(
        "--force", action="store_true", help="overwrite existing agent_trace.json"
    )
    p.add_argument("--svg", action="store_true", help="also write agent_trace.svg")
    p.add_argument(
        "--limit", type=int, default=0, help="only process first N (0 = all)"
    )
    args = p.parse_args()

    if not args.root.is_dir():
        print(f"ERROR: {args.root} is not a directory", file=sys.stderr)
        return 2

    trials = find_trial_dirs(args.root)
    if args.limit:
        trials = trials[: args.limit]
    print(f"discovered {len(trials)} trial dirs under {args.root}")

    counts = {"ok": 0, "skipped": 0, "failed": 0}
    by_kind: dict[str, int] = {}
    for trial in trials:
        agent_dir = trial / "agent"
        kind = detect_agent_kind(agent_dir) if agent_dir.is_dir() else "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        ok, msg = convert_trial(trial, force=args.force, render_svg=args.svg)
        if ok:
            counts["ok"] += 1
            print(f"  OK  {trial.relative_to(args.root)} :: {msg}")
        elif (
            "already exists" in msg
            or "skipped" in msg
            or "unknown" in msg
            or "no agent/" in msg
        ):
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
            print(f"  FAIL {trial.relative_to(args.root)} :: {msg}")

    print(
        f"\nDone: ok={counts['ok']} skipped={counts['skipped']} failed={counts['failed']}"
    )
    print(f"By kind: {by_kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
