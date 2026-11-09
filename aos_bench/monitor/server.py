"""FastAPI app factory for the Eval Monitor.

The factory wires up all background tasks (discoverer, resource sampler) into
the app's lifespan and registers REST + SSE endpoints. Static assets are
served from the package's ``static/`` directory.
"""

from __future__ import annotations

import asyncio
import json
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from aos_bench.monitor.discover import TrialDiscoverer
from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.resources import HostResourceSampler
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tasks import attribute_task_executors, extract_task_lifecycle

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    *,
    jobs_dir: Path,
    scan_interval_s: float = 2.0,
    tail_interval_s: float = 1.5,
    resource_interval_s: float = 2.0,
) -> FastAPI:
    state = MonitorState(jobs_dir=str(jobs_dir))
    detector = ErrorDetector(state)
    discoverer = TrialDiscoverer(
        jobs_dir=jobs_dir,
        state=state,
        error_detector=detector,
        scan_interval_s=scan_interval_s,
        tail_interval_s=tail_interval_s,
    )
    sampler = HostResourceSampler(state=state, interval_s=resource_interval_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await discoverer.start()
        await sampler.start()
        try:
            yield
        finally:
            await sampler.stop()
            await discoverer.stop()

    app = FastAPI(title="AOSBench Eval Monitor", version="0.1", lifespan=lifespan)
    # Trial-detail payloads contain normalized event tails from every run.
    # Finished fan-out trials can exceed 3 MiB uncompressed, which makes the
    # dashboard feel stuck over a remote connection. JSON compresses well
    # (typically 5-8x here), and Starlette handles Accept-Encoding negotiation.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.state.monitor_state = state  # expose for tests

    # ── REST ──

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/jobs")
    def jobs_info() -> dict[str, object]:
        return {
            "jobs_dir": state.jobs_dir,
            "scan_interval_s": scan_interval_s,
            "tail_interval_s": tail_interval_s,
            "resource_interval_s": resource_interval_s,
            "started_at": state.started_at,
        }

    @app.get("/api/trials")
    def list_trials() -> list[dict[str, object]]:
        return state.trials_summary()

    @app.get("/api/trials/{trial_id}")
    def trial_detail(trial_id: str) -> dict[str, object]:
        detail = state.trial_detail(trial_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown trial: {trial_id}")
        return detail

    @app.get("/api/resources")
    def resources() -> dict[str, object]:
        latest = state.latest_resource()
        return {
            "latest": latest.to_dict() if latest else None,
            "samples": [s.to_dict() for s in state.resources],
        }

    @app.get("/api/trials/{trial_id}/resources")
    def trial_resources(trial_id: str) -> dict[str, object]:
        if trial_id not in state.trials:
            raise HTTPException(status_code=404, detail=f"unknown trial: {trial_id}")
        samples = state.trial_resource_samples(trial_id)
        return {
            "trial_id": trial_id,
            "samples": samples,
            "latest": samples[-1] if samples else None,
        }

    @app.get("/api/trials/{trial_id}/processes")
    def trial_processes(trial_id: str, history: bool = False) -> dict[str, object]:
        """Process-level resource snapshots captured inside the main container.

        The dashboard only needs the latest table. ``history=true`` is kept
        for offline attribution/debugging; the complete source also remains
        in ``.monitor-processes.jsonl`` beside the trial.
        """
        if trial_id not in state.trials:
            raise HTTPException(status_code=404, detail=f"unknown trial: {trial_id}")
        samples = state.trial_process_snapshots(trial_id)
        peaks: dict[tuple[int, str, str], dict[str, object]] = {}
        for snapshot in samples:
            ts = float(snapshot.get("ts") or 0)
            for row in snapshot.get("processes") or []:
                key = (
                    int(row.get("pid") or 0),
                    str(row.get("command") or ""),
                    str(row.get("args") or ""),
                )
                current = peaks.get(key)
                if current is None:
                    current = dict(row)
                    current["first_seen_at"] = ts
                    current["last_seen_at"] = ts
                    current["peak_cpu_percent"] = float(
                        row.get("cpu_percent") or 0
                    )
                    current["peak_mem_rss_mb"] = float(
                        row.get("mem_rss_mb") or 0
                    )
                    peaks[key] = current
                else:
                    current["last_seen_at"] = ts
                    current["peak_cpu_percent"] = max(
                        float(current["peak_cpu_percent"]),
                        float(row.get("cpu_percent") or 0),
                    )
                    current["peak_mem_rss_mb"] = max(
                        float(current["peak_mem_rss_mb"]),
                        float(row.get("mem_rss_mb") or 0),
                    )
                    if current.get("run_id") is None and row.get("run_id"):
                        current["run_id"] = row["run_id"]
                        current["attribution"] = row.get("attribution")
        peak_rows = sorted(
            peaks.values(),
            key=lambda row: (
                float(row.get("peak_mem_rss_mb") or 0),
                float(row.get("peak_cpu_percent") or 0),
            ),
            reverse=True,
        )
        return {
            "trial_id": trial_id,
            "sample_count": len(samples),
            "latest": samples[-1] if samples else None,
            "peaks": peak_rows[:256],
            "samples": samples if history else [],
        }

    @app.get("/api/trials/{trial_id}/tasks")
    def trial_tasks(trial_id: str) -> dict[str, object]:
        """Per-task dispatcher lifecycle (dispatch / claim / complete).

        Primary source: ``state.trials[id].latest_task_snapshot``, updated
        incrementally by ``KimiSessionTail`` whenever it sees a fresh
        ``todo_list`` response. This survives ``events_tail`` rotation.

        Fallback: re-parse from events_tail (only useful for short runs
        where todo_list responses are still in the tail).
        """
        trial = state.trials.get(trial_id)
        if trial is None:
            raise HTTPException(status_code=404, detail=f"unknown trial: {trial_id}")
        # Source preference:
        #   1. <trial>/dispatcher/tasks.json — the dispatcher's own flushed
        #      state, copied next to the trial by harbor at teardown. Exact
        #      ready/claim/terminal offsets; available post-hoc / on replay.
        #   2. live wire view — latest todo_list snapshot, else todo_next/
        #      todo_update reconstruction.
        source = "dispatcher"
        tasks = _load_dispatcher_snapshot_for_trial(Path(trial.trial_dir))
        if not tasks:
            source = "wire"
            tasks = trial.latest_task_snapshot
            if not tasks:
                # Scan EVERY run's events, not just the root: under workflow /
                # team fan-out the todo_next/todo_update calls happen in the
                # child lanes (each workflow agent claims its own tasks).
                events = [
                    ev for r in trial.runs.values() for ev in r.events_tail
                ]
                events.sort(
                    key=lambda e: e.get("timestamp")
                    if isinstance(e.get("timestamp"), (int, float))
                    else float("inf")
                )
                tasks = extract_task_lifecycle(events)
        # Anchor the dispatcher timeline to the root agent's first event
        # if known. Dispatcher offsets (ready_at, claimed_at, completed_at)
        # are seconds since harbor's trial-start, which lines up with the
        # agent's first wire event, NOT with monitor discovery time
        # (`trial.started_at` is `time.time()` when the monitor first saw
        # the trial dir — could be hours after the trial actually ran).
        anchor = trial.started_at
        root = trial.runs.get("run_root_001")
        if root is not None and root.started_at is not None:
            anchor = root.started_at
        else:
            # Root lane may carry no timestamped events (e.g. workflow runs
            # where all activity lives in child lanes) — fall back to the
            # earliest run start rather than monitor discovery time.
            starts = [
                r.started_at for r in trial.runs.values() if r.started_at is not None
            ]
            if starts:
                anchor = min(starts)
        if source == "dispatcher":
            # The dispatcher's own pool_start ts is the exact zero of the
            # ready/claim/terminal offsets — prefer it over the agent's
            # first-event approximation when the flushed log is present.
            pool_start = _load_dispatcher_pool_start(Path(trial.trial_dir))
            if pool_start is not None:
                anchor = pool_start
        # Enrich with the full arrivals list from stream.toml so the
        # dispatcher panel can show ALL tasks (including pending /
        # ready-but-unclaimed) — NOT just the ones the agent has
        # interacted with. The agent's MCP view correctly hides
        # pending tasks; the monitor (human-facing) should not be
        # similarly blinded.
        arrivals = _load_stream_arrivals_for_trial(Path(trial.trial_dir))
        if arrivals:
            tasks = _merge_arrivals_into_lifecycle(
                arrivals=arrivals,
                wire_lifecycle=tasks or [],
                anchor=anchor,
                now=time.time(),
            )
        # Executor attribution: which run (root / sidechain / workflow agent)
        # claimed + reported each task — colors the Gantt by executor.
        executors = attribute_task_executors(
            {rid: list(r.events_tail) for rid, r in trial.runs.items()}
        )
        run_names = {rid: r.agent_name for rid, r in trial.runs.items()}
        for row in tasks or []:
            ex = executors.get(row.get("task_id") or "")
            if not ex:
                continue
            run_id = ex.get("reported_by") or ex.get("claimed_by")
            row["executor_run_id"] = run_id
            row["executor"] = run_names.get(run_id, run_id)
            if "claimed_by" in ex:
                row["claimed_by_run_id"] = ex["claimed_by"]
        return {
            "trial_id": trial_id,
            "trial_started_at": anchor,
            "source": source,
            "tasks": tasks or [],
        }

    @app.get("/api/errors")
    def errors() -> list[dict[str, object]]:
        return state.errors_snapshot()

    # ── SSE ──

    @app.get("/api/events")
    async def sse_events() -> StreamingResponse:
        async def event_stream() -> AsyncIterator[bytes]:
            queue = state.subscribe()
            try:
                # Send a hello so the client connection completes immediately.
                yield _sse_format({"type": "hello"})
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield _sse_format(payload)
                    except asyncio.TimeoutError:
                        # Heartbeat so proxies don't close the stream.
                        yield b": ping\n\n"
            finally:
                state.unsubscribe(queue)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # ── Static frontend ──

    if STATIC_DIR.is_dir():
        # mount /static/ for assets, with no-cache headers so browsers always
        # pick up the latest js/css/html (we iterate fast; etag-based revalidation
        # on a re-deploy is too easy to get wrong).
        class _NoCacheStatic(StaticFiles):
            async def get_response(self, path: str, scope):  # type: ignore[override]
                resp = await super().get_response(path, scope)
                if resp.status_code == 200:
                    resp.headers["cache-control"] = "no-store, must-revalidate"
                    resp.headers["pragma"] = "no-cache"
                    resp.headers["expires"] = "0"
                return resp

        app.mount("/static", _NoCacheStatic(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(
                str(STATIC_DIR / "index.html"),
                headers={"cache-control": "no-store, must-revalidate"},
            )
    else:

        @app.get("/")
        def index_missing() -> dict[str, str]:  # pragma: no cover
            return {"warning": "static/ not found", "static_dir": str(STATIC_DIR)}

    return app


def _sse_format(payload: dict[str, object]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# ── flushed dispatcher state (harbor copies it to <trial>/dispatcher/) ──


def _find_dispatcher_dir(trial_dir: Path) -> Path | None:
    candidates = [
        trial_dir / "dispatcher",
        *trial_dir.glob("*/dispatcher"),
        *trial_dir.glob("*/*/dispatcher"),
    ]
    return next((c for c in candidates if (c / "tasks.json").is_file()), None)


def _load_dispatcher_snapshot_for_trial(trial_dir: Path) -> list[dict[str, Any]]:
    """Rows from the dispatcher's flushed ``tasks.json``, in the same shape
    the wire-derived lifecycle uses. ``[]`` when absent (live trials —
    harbor only copies the state at teardown)."""
    disp = _find_dispatcher_dir(trial_dir)
    if disp is None:
        return []
    try:
        data = json.loads((disp / "tasks.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("task_id"), str):
            continue
        out.append(
            {
                "task_id": row["task_id"],
                "at_sec": row.get("at_sec"),
                "ready_at": row.get("ready_at"),
                "claimed_at": row.get("claimed_at"),
                "completed_at": row.get("completed_at"),
                "status": row.get("status"),
                "note": row.get("note", ""),
            }
        )
    out.sort(
        key=lambda r: r.get("at_sec")
        if isinstance(r.get("at_sec"), (int, float))
        else 1e18
    )
    return out


def _load_dispatcher_pool_start(trial_dir: Path) -> float | None:
    """Epoch ts of the pool_start event in the flushed events.jsonl."""
    disp = _find_dispatcher_dir(trial_dir)
    if disp is None:
        return None
    events_path = disp / "events.jsonl"
    if not events_path.is_file():
        return None
    try:
        with events_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict) and ev.get("event") == "pool_start":
                    ts = ev.get("ts")
                    return float(ts) if isinstance(ts, (int, float)) else None
    except OSError:
        return None
    return None


# ── stream.toml-aware dispatcher view ─────────────────────────────────


def _load_stream_arrivals_for_trial(trial_dir: Path) -> list[dict[str, Any]]:
    """Read the source stream.toml's [[arrivals]] for the given trial.

    Path chain:
      <trial_dir>/config.json  →  task.path  →  <task.path>/stream.toml

    Returns a list of ``{"task_id": str, "at_sec": float}`` entries
    (one per [[arrivals]] in the toml), or ``[]`` if any step fails
    (missing config, unreachable path, malformed toml). All failures
    are silent — the API endpoint falls back to wire-reconstruction
    only.
    """
    try:
        cfg = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    task = cfg.get("task") or {}
    src_path = task.get("path")
    if not isinstance(src_path, str):
        return []
    toml_path = Path(src_path) / "stream.toml"
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    arrivals_raw = data.get("arrivals")
    if not isinstance(arrivals_raw, list):
        return []
    out: list[dict[str, Any]] = []
    for a in arrivals_raw:
        if not isinstance(a, dict):
            continue
        tid = a.get("task_id")
        at = a.get("arrive_wall_time", 0)
        if not isinstance(tid, str):
            continue
        try:
            at_sec = float(at)
        except (TypeError, ValueError):
            at_sec = 0.0
        out.append({"task_id": tid, "at_sec": at_sec})
    return out


def _merge_arrivals_into_lifecycle(
    *,
    arrivals: list[dict[str, Any]],
    wire_lifecycle: list[dict[str, Any]],
    anchor: float | None,
    now: float,
) -> list[dict[str, Any]]:
    """Build the full dispatcher view by overlaying wire-derived state
    on the stream.toml arrival skeleton.

    Status state machine, evaluated per task:
      - terminal status from wire (completed / failed)  → use as-is
      - claimed via todo_next, no terminal yet          → in_progress
      - not claimed AND now − anchor ≥ at_sec           → ready
      - not claimed AND now − anchor <  at_sec          → pending

    Tasks present only in the wire (sentinels, mis-spawned task_ids)
    are still surfaced — they appear after the arrivals list, ordered
    by claimed_at. Tasks present only in stream.toml are filled in
    with ``status: pending|ready`` based on the current wall clock.
    """
    wire_by_id = {t["task_id"]: t for t in wire_lifecycle if isinstance(t.get("task_id"), str)}
    elapsed = (now - anchor) if (anchor is not None and now >= anchor) else 0.0
    out: list[dict[str, Any]] = []
    for arr in arrivals:
        tid = arr["task_id"]
        at_sec = arr["at_sec"]
        wire = wire_by_id.pop(tid, None)
        if wire is not None:
            # Wire wins for everything it carries — but preserve
            # at_sec from stream.toml (wire often has at_sec = None
            # under the reconstruction path).
            merged = dict(wire)
            if merged.get("at_sec") is None:
                merged["at_sec"] = at_sec
            # If wire said in_progress but actually finished, leave
            # wire's status alone — don't overwrite terminal states.
            out.append(merged)
        else:
            status = "ready" if elapsed >= at_sec else "pending"
            out.append(
                {
                    "task_id": tid,
                    "at_sec": at_sec,
                    "ready_at": at_sec if status == "ready" else None,
                    "claimed_at": None,
                    "completed_at": None,
                    "status": status,
                    "note": "",
                }
            )
    # Any leftover wire-only tasks (shouldn't happen normally, defensive)
    for tid, wire in wire_by_id.items():
        out.append(wire)
    return out
