"""AOS-Bench pool MCP server.

Wraps `TaskPool` and exposes 5 tools to the agent:

  todo_next, todo_list, todo_update, todo_get, todo_status

v0.1 (legacy, stdio transport, schedule on filesystem):

    python3 -m aos_bench.pool.server \
        --schedule /sandbox/aos/schedule.json \
        --state-dir /sandbox/aos/state

v0.2 (sidecar mode — see docs/framework/task_assign.md):

    python3 -m aos_bench.pool.server \
        --schedule-env AOS_SCHEDULE_B64 \
        --listen-uds /run/aos_pool.sock \
        --state-dir /logs/agent \
        --workspace-stage /aos_internal/sources \
        --workspace-target /workspace \
        --skills-stage /aos_internal/skills \
        --skills-target /skills \
        --agent-uid 1000

In v0.2 the schedule is passed as a base64-encoded JSON env var
(deleted from os.environ after read so /proc/<pid>/environ doesn't
leak it). Server writes nothing to disk during operation; on SIGTERM
or atexit it flushes events.jsonl + tasks.json to ``--state-dir``.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import base64
import json
import logging
import os
import shutil
import signal
import socket
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# Importable two ways:
#   - as part of the aos_bench package (repo / unit tests)
#   - as a flat directory uploaded to a container under /opt/aos_bench/pool/,
#     where neither aos_bench nor pool live on sys.path. The fallback adds
#     this file's directory and imports `pool` as a sibling module.
try:
    from aos_bench.pool.pool import (
        DEFAULT_TICK_SEC,
        Arrival,
        TaskPool,
        load_schedule,
    )
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pool import (  # type: ignore
        DEFAULT_TICK_SEC,
        Arrival,
        TaskPool,
        load_schedule,
    )


logger = logging.getLogger(__name__)


def _make_staging_hook(
    workspace_stage: Path | None,
    workspace_target: Path | None,
    skills_stage: Path | None,
    skills_target: Path | None,
    agent_uid: int | None,
):
    """Build a staging callable for TaskPool.

    On READY transition, move the *contents* of
    ``<workspace_stage>/<task_id>/`` into ``<workspace_target>/`` and chown to
    ``agent_uid``. Skills follow the same pattern. Any of the four paths being
    None skips that half. If all four are None, returns None (no hook).

    The workspace half merges contents rather than moving the directory to
    ``<workspace_target>/<task_id>/`` because the assembler rewrites every
    task-local path to the *flat* target: `tests/test_outputs.py` opens
    ``/workspace/model.xml`` and instructions name ``/workspace/eval.py``. A
    per-task subdirectory would put the payload one level below where the
    task's own instructions, tests, and oracle all look for it. This is the
    same shared-workspace model the collision-rename machinery assumes, so
    the two stay consistent: cross-task name clashes are resolved at assembly
    time by suffixing, not by nesting at runtime.
    """
    if not (workspace_stage or skills_stage):
        return None

    def hook(task_id: str) -> None:
        # Workspace
        if workspace_stage and workspace_target:
            src = workspace_stage / task_id
            if src.is_dir():
                workspace_target.mkdir(parents=True, exist_ok=True)
                for entry in src.iterdir():
                    dst = workspace_target / entry.name
                    if dst.exists():
                        # Assembly-time collision renaming should make this
                        # unreachable; if it happens anyway, keep whatever is
                        # already revealed rather than clobbering a live task.
                        logger.warning(
                            "staging: %s already exists, not overwriting "
                            "while revealing %s",
                            dst,
                            task_id,
                        )
                        continue
                    shutil.move(str(entry), str(dst))
                    if agent_uid is not None:
                        _chown_recursive(dst, agent_uid)
                # Only empty once every entry moved; a skipped collision
                # leaves the stage dir behind on purpose (root-owned, so
                # still not agent-readable) rather than losing the file.
                try:
                    src.rmdir()
                except OSError as e:
                    logger.warning("staging: could not remove %s: %s", src, e)
        # Skills
        if skills_stage and skills_target:
            src = skills_stage / task_id
            if src.exists():
                # Archive any existing /skills/ contents first
                if skills_target.exists():
                    archive_root = skills_target.parent / "skills_archive"
                    archive_root.mkdir(parents=True, exist_ok=True)
                    archive = archive_root / f"prev_{task_id}"
                    # Move the *contents* into archive — keep skills_target
                    archive.mkdir(exist_ok=True)
                    for entry in skills_target.iterdir():
                        shutil.move(str(entry), str(archive / entry.name))
                else:
                    skills_target.mkdir(parents=True, exist_ok=True)
                # Move new task's skills *contents* into skills_target
                for entry in src.iterdir():
                    shutil.move(str(entry), str(skills_target / entry.name))
                src.rmdir()
                if agent_uid is not None:
                    _chown_recursive(skills_target, agent_uid)

    return hook


def _chown_recursive(root: Path, uid: int) -> None:
    """chown -R uid:uid <root>. Best-effort; ignores files we can't chown."""
    try:
        os.chown(root, uid, uid)
    except OSError as e:
        logger.debug("chown root %s -> %d failed: %s", root, uid, e)
    for path in root.rglob("*"):
        try:
            os.chown(path, uid, uid)
        except OSError as e:
            logger.debug("chown %s -> %d failed: %s", path, uid, e)


def _load_schedule_from_env(env_var: str) -> tuple[list[Arrival], float, bool]:
    raw = os.environ.pop(env_var, None)  # pop = remove from /proc/<pid>/environ
    if not raw:
        raise SystemExit(
            f"--schedule-env: env var {env_var!r} is empty or missing"
        )
    try:
        decoded = base64.b64decode(raw).decode()
        data = json.loads(decoded)
    except Exception as e:
        raise SystemExit(
            f"--schedule-env: failed to decode/parse {env_var!r}: {e}"
        ) from e

    max_wall_sec = float(data.get("max_wall_sec", 600))
    serial = bool(data.get("serial", False))
    arrivals: list[Arrival] = []
    for a in data.get("arrivals", []):
        arrivals.append(
            Arrival(
                task_id=str(a["task_id"]),
                at_sec=float(a["at_sec"]),
                instruction=str(a.get("instruction", "")),
                deadline_sec=(
                    float(a["deadline_sec"]) if "deadline_sec" in a else None
                ),
            )
        )
    return arrivals, max_wall_sec, serial


def create_server(
    *,
    arrivals: list[Arrival],
    max_wall_sec: float,
    serial: bool,
    state_dir: Path,
    tick_sec: float = DEFAULT_TICK_SEC,
    in_memory: bool = False,
    staging_hook=None,
) -> tuple[FastMCP, TaskPool]:
    pool = TaskPool(
        arrivals=arrivals,
        max_wall_sec=max_wall_sec,
        state_dir=state_dir,
        tick_sec=tick_sec,
        serial=serial,
        in_memory=in_memory,
        staging_hook=staging_hook,
    )
    # FastMCP enables DNS rebinding protection by default, which only
    # accepts requests with Host: 127.0.0.1. The v0.3 dispatcher serves
    # over a docker bridge where clients connect via the DNS alias
    # `aos_dispatcher:8765` — Host header doesn't match, server returns
    # 421 "Invalid Host header". The threat model that protection is
    # designed for (browser-based DNS rebinding) does not apply: this
    # endpoint is reachable only from inside a private docker network,
    # there is no browser, and /admin/* already has bearer-token auth.
    # Allow any Host header.
    from mcp.server.fastmcp.server import TransportSecuritySettings

    mcp = FastMCP(
        "aos-pool",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    @mcp.tool()
    async def todo_next(timeout_sec: int = 60) -> str:
        """Block up to `timeout_sec` seconds for the next ready task.

        Returns a JSON object. Three possible shapes:

          {"task_id": "...", "instruction": "...", "stream_done": false}
              A real task is ready. Work on it, then report the final
              outcome with `todo_update(task_id, "completed")` or
              `todo_update(task_id, "failed")`. The pool already marks
              the task as in-flight on your behalf — you do not need to
              report any intermediate status.

          {"task_id": "<FINISH>", "instruction": "<FINISH>",
           "stream_done": true}
              The task stream is complete. Exit gracefully. Do NOT
              call todo_update on this sentinel — it isn't a real task.

          {"task_id": null, "stream_done": false, "timed_out": true}
              No task ready yet but more may arrive. Call `todo_next`
              again.

        You MUST keep calling todo_next until you receive `<FINISH>`.
        Stopping earlier forfeits any unclaimed tasks.
        """
        # Offloaded to a worker thread, and this is load-bearing, not tidiness.
        #
        # `pool.todo_next` blocks on a `threading.Condition` for up to
        # timeout_sec (default 60s). FastMCP invokes a *sync* tool function
        # directly on the event loop -- `call_fn_with_arg_validation` does
        # `return fn(**kwargs)` with no `to_thread` -- so as a plain `def`
        # this froze the entire uvicorn loop for the whole wait. Measured on
        # the v0.3 host dispatcher during the 2026-09-18 smoke: /healthz did
        # not answer for >20s and the listen queue backed up (Recv-Q 4) while
        # the process sat in futex_wait at 0% CPU.
        #
        # That is fatal for the main experiment rather than merely untidy: one
        # dispatcher serves an entire harbor job (design D7), so at 200-way
        # concurrency a single agent waiting in todo_next would stall every
        # other trial's pool calls behind it. Under the `stream` setting
        # waiting for the next arrival is the *normal* state, not an edge
        # case, so this would have inflated completion time -- a headline
        # metric -- across the whole sweep, while still producing plausible,
        # plottable numbers.
        #
        # Safe to run off-loop: TaskPool guards all state with self._lock and
        # signals via self._cv, both ordinary thread primitives.
        return await asyncio.to_thread(
            lambda: json.dumps(pool.todo_next(timeout_sec=float(timeout_sec)))
        )

    @mcp.tool()
    def todo_list() -> str:
        """Snapshot of tasks currently visible to you.

        Includes only tasks that are already ready, in flight, or
        terminal (completed / failed). Future arrivals are intentionally
        hidden — you only see a task once it's claimable.

        Returns a JSON array of objects with keys:
          task_id, status, ready_at, claimed_at, completed_at, note
        """
        return json.dumps(pool.todo_list_agent_view(), indent=2)

    @mcp.tool()
    def todo_update(task_id: str, status: str, note: str = "") -> str:
        """Report the final outcome of a claimed task.

        Args:
          task_id: id returned by an earlier `todo_next`.
          status: `completed` if the task succeeded, `failed` if not.
          note: optional free-form context (especially for `failed`).

        Call this exactly once per task — the pool does not track
        intermediate progress. Returns {"ok": true, "task_id", "status"}.
        """
        return json.dumps(pool.todo_update(task_id, status, note))

    @mcp.tool()
    def todo_get(task_id: str) -> str:
        """Re-fetch a known task's instruction and current status."""
        return json.dumps(pool.todo_get(task_id))

    @mcp.tool()
    def todo_status() -> str:
        """High-level overview of work currently visible to you.

        Returns now_sec, counts_by_status (excluding pending future
        arrivals), and stream_done. The total task count is intentionally
        not exposed — the stream ends when stream_done flips to true,
        not when you hit some pre-known number.
        """
        return json.dumps(pool.todo_status_agent_view())

    return mcp, pool


def _install_signal_flush(pool: TaskPool) -> None:
    """Make sure events.jsonl + tasks.json are flushed on SIGTERM / SIGINT
    and on normal interpreter exit. Idempotent — pool.flush() is a no-op
    in non-in-memory mode and only writes once otherwise.
    """

    def _handler(signum, frame):
        try:
            pool.stop()
        finally:
            # Re-raise default behaviour
            sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    atexit.register(pool.flush)


def _serve_uds(mcp: FastMCP, uds_path: Path) -> None:
    """Bind a Unix domain socket and serve MCP over it.

    The MCP SDK exposes `stdio_server()` — for UDS we accept a single
    connection (the agent's `socat` bridge) and pipe its bytes through
    the same stdio_server contract by replacing sys.stdin/sys.stdout.

    This is intentionally single-connection: each trial has exactly one
    agent CLI, which spawns one socat. Multi-conn is a future extension
    if we need parallel bridges (e.g., post-run trace collector).
    """
    try:
        uds_path.unlink()
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(uds_path))
    # Group writeable: only members of the socket-file group can connect.
    # Caller is responsible for chown'ing the socket file appropriately
    # (e.g., chown root:aos_agent + chmod 0660) AFTER server starts.
    os.chmod(str(uds_path), 0o660)
    sock.listen(1)
    logger.info("aos_pool: listening on %s", uds_path)
    try:
        conn, _ = sock.accept()
        # Replace stdio with the connection so FastMCP.run(transport="stdio")
        # reads/writes the socket transparently.
        sys.stdin = os.fdopen(conn.fileno(), "r", closefd=False)
        sys.stdout = os.fdopen(
            os.dup(conn.fileno()), "w", closefd=False
        )
        mcp.run(transport="stdio")
    finally:
        try:
            sock.close()
        except Exception:
            pass
        try:
            uds_path.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    p = argparse.ArgumentParser(description="AOS-Bench pool MCP server")
    p.add_argument("--schedule", type=Path, help="path to schedule.json (v0.1)")
    p.add_argument(
        "--schedule-env",
        type=str,
        help="env var name carrying base64(json) schedule (v0.2 sidecar)",
    )
    p.add_argument("--state-dir", required=True, type=Path)
    p.add_argument("--tick-sec", type=float, default=DEFAULT_TICK_SEC)
    p.add_argument(
        "--listen-uds",
        type=Path,
        help="Unix socket path to listen on (v0.2 sidecar). Otherwise stdio.",
    )
    p.add_argument(
        "--workspace-stage",
        type=Path,
        help="Source dir holding per-task workspace (mv on READY).",
    )
    p.add_argument(
        "--workspace-target",
        type=Path,
        help="Destination dir agent sees as /workspace.",
    )
    p.add_argument(
        "--skills-stage",
        type=Path,
        help="Source dir holding per-task skills (mv on READY).",
    )
    p.add_argument(
        "--skills-target",
        type=Path,
        help="Destination dir agent sees as /skills (or wherever).",
    )
    p.add_argument(
        "--agent-uid",
        type=int,
        help="uid to chown staged files to (typically the agent user).",
    )
    args = p.parse_args()

    if args.schedule and args.schedule_env:
        raise SystemExit("Specify either --schedule or --schedule-env, not both")
    if args.schedule:
        arrivals, max_wall_sec, serial = load_schedule(args.schedule)
    elif args.schedule_env:
        arrivals, max_wall_sec, serial = _load_schedule_from_env(
            args.schedule_env
        )
    else:
        raise SystemExit("Either --schedule or --schedule-env is required")

    in_memory = bool(args.listen_uds)  # sidecar implies in-memory state
    # Staging is only meaningful if the image was assembled with --pool-v02:
    # without it every task's payload is COPY'd straight into the workspace
    # target and the stage dir does not exist, so the reveal hook silently
    # does nothing while all tasks are visible from t0 — the exact leak
    # staging exists to prevent. Fail loudly instead.
    if args.workspace_stage is not None and not args.workspace_stage.is_dir():
        raise SystemExit(
            f"--workspace-stage {args.workspace_stage} does not exist. The "
            f"stream image was almost certainly assembled without "
            f"--pool-v02, so task payloads are already in "
            f"{args.workspace_target} and nothing would be staged: every "
            f"task would be visible to the agent from the start. Re-assemble "
            f"the stream with --pool-v02 and rebuild the image."
        )
    staging_hook = _make_staging_hook(
        args.workspace_stage,
        args.workspace_target,
        args.skills_stage,
        args.skills_target,
        args.agent_uid,
    )

    mcp, pool = create_server(
        arrivals=arrivals,
        max_wall_sec=max_wall_sec,
        serial=serial,
        state_dir=args.state_dir,
        tick_sec=args.tick_sec,
        in_memory=in_memory,
        staging_hook=staging_hook,
    )
    pool.start()
    _install_signal_flush(pool)
    try:
        if args.listen_uds:
            _serve_uds(mcp, args.listen_uds)
        else:
            mcp.run(transport="stdio")
    finally:
        pool.stop()


# Keep the legacy entry point for backwards-compatible callers.
def run_server(
    schedule_path: Path,
    state_dir: Path,
    tick_sec: float = DEFAULT_TICK_SEC,
) -> None:
    arrivals, max_wall_sec, serial = load_schedule(schedule_path)
    mcp, pool = create_server(
        arrivals=arrivals,
        max_wall_sec=max_wall_sec,
        serial=serial,
        state_dir=state_dir,
        tick_sec=tick_sec,
    )
    pool.start()
    try:
        mcp.run(transport="stdio")
    finally:
        pool.stop()


if __name__ == "__main__":
    main()
