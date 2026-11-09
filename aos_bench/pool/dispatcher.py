"""AOS-Bench task dispatcher — universal HTTP MCP front for many streams.

See ``docs/framework/task_assign.md`` (v0.3) for the architecture and
threat model. tl;dr:

    * One ``aos-bench/dispatcher`` container per trial (cheap; ~150 MB
      image, ~2 s startup, in-memory only).
    * Single HTTP listener on ``MCP_BIND`` (default ``0.0.0.0:8765``).
    * MCP traffic at ``/<stream_id>/mcp`` (FastMCP streamable-http).
    * Admin endpoints at ``/admin/*`` are gated by a bearer token —
      ``ADMIN_TOKEN`` env var. Harbor knows the token and sends it
      with each register/flush/unregister; the agent does not.
    * On SIGTERM (or ``/admin/flush``) each stream's events.jsonl +
      tasks.json are written to ``PERSIST_ROOT/<stream_id>/``.

The bearer-token gate is the v0.3 substitute for network separation —
simpler than running two listeners on two networks. ``ADMIN_TOKEN``
is generated fresh by harbor for each dispatcher container.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import secrets
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

try:
    from aos_bench.pool.pool import Arrival, TaskPool
    from aos_bench.pool.server import create_server
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pool import Arrival, TaskPool  # type: ignore
    from server import create_server  # type: ignore


logger = logging.getLogger(__name__)

# ── stream_id charset ──────────────────────────────────────────────────────
_VALID_STREAM_ID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)
_MAX_STREAM_ID_LEN = 128


def _validate_stream_id(stream_id: str) -> None:
    if not stream_id:
        raise ValueError("stream_id must be non-empty")
    if len(stream_id) > _MAX_STREAM_ID_LEN:
        raise ValueError(f"stream_id too long (max {_MAX_STREAM_ID_LEN})")
    bad = set(stream_id) - _VALID_STREAM_ID_CHARS
    if bad:
        raise ValueError(f"stream_id contains illegal chars: {sorted(bad)!r}")


# ── per-stream registry ────────────────────────────────────────────────────


@dataclass
class _StreamEntry:
    pool: TaskPool
    mount_path: str
    persist_dir: Path | None = None
    flushed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    # FastMCP's StreamableHTTPSessionManager needs session_manager.run()
    # to be entered as an async context for the lifetime of the mount.
    # We push that context into the dispatcher's AsyncExitStack at
    # register() time and pop it at unregister() time. Without this,
    # POSTs to /<sid>/mcp raise "Task group is not initialized."
    mcp: Any = None
    session_manager_cm: Any = None  # async context manager handle


class Dispatcher:
    """In-memory multi-stream pool registry + dynamic Starlette mounting."""

    def __init__(self, persist_root: Path) -> None:
        self.persist_root = persist_root
        self._streams: dict[str, _StreamEntry] = {}
        # The Starlette app whose router we mutate.
        self._app: Starlette | None = None
        # AsyncExitStack populated at app startup; each registered
        # stream pushes its session_manager.run() context here so the
        # task group is alive for the duration of the mount.
        self._stack: contextlib.AsyncExitStack | None = None

    def attach_app(self, app: Starlette) -> None:
        self._app = app

    def attach_stack(self, stack: contextlib.AsyncExitStack) -> None:
        self._stack = stack

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def register(
        self,
        stream_id: str,
        arrivals: list[Arrival],
        max_wall_sec: float,
        serial: bool,
        persist_dir: Path | None,
        meta: dict[str, Any] | None = None,
    ) -> _StreamEntry:
        _validate_stream_id(stream_id)
        if stream_id in self._streams:
            raise ValueError(f"stream_id already registered: {stream_id!r}")
        if self._app is None:
            raise RuntimeError("Dispatcher app not attached yet")

        state_dir = persist_dir or (self.persist_root / stream_id)
        mcp, pool = create_server(
            arrivals=arrivals,
            max_wall_sec=max_wall_sec,
            serial=serial,
            state_dir=state_dir,
            in_memory=True,
        )
        pool.start()

        sub_app = _build_streamable_http_app(mcp)
        mount_path = f"/{stream_id}"
        self._app.routes.append(Mount(mount_path, app=sub_app))
        # Starlette caches a compiled router; force rebuild.
        self._app.router.routes = self._app.routes

        # Start the FastMCP session_manager's task group. Without this,
        # the streamable-http handler raises "Task group is not
        # initialized." on the first POST.
        session_manager_cm = None
        if self._stack is not None and hasattr(mcp, "session_manager"):
            session_manager_cm = mcp.session_manager.run()
            await self._stack.enter_async_context(session_manager_cm)

        entry = _StreamEntry(
            pool=pool,
            mount_path=mount_path,
            persist_dir=state_dir,
            meta=meta or {},
            mcp=mcp,
            session_manager_cm=session_manager_cm,
        )
        self._streams[stream_id] = entry
        logger.info(
            "registered stream_id=%s arrivals=%d max_wall_sec=%.1f serial=%s",
            stream_id, len(arrivals), max_wall_sec, serial,
        )
        return entry

    def flush(self, stream_id: str) -> None:
        entry = self._streams.get(stream_id)
        if entry is None:
            raise KeyError(stream_id)
        entry.pool.flush()
        entry.flushed = True
        logger.info("flushed stream_id=%s persist_dir=%s",
                    stream_id, entry.persist_dir)

    def freeze(self, stream_id: str, reason: str = "execution_limit") -> None:
        """Close one stream's pool at the end of the execution phase."""
        entry = self._streams.get(stream_id)
        if entry is None:
            raise KeyError(stream_id)
        entry.pool.freeze(reason)
        logger.info("froze stream_id=%s reason=%s", stream_id, reason)

    def unregister(self, stream_id: str) -> None:
        entry = self._streams.pop(stream_id, None)
        if entry is None:
            return
        try:
            entry.pool.stop()
        except Exception as e:
            logger.warning("stop pool %s: %s", stream_id, e)
        if self._app is not None:
            self._app.routes[:] = [
                r for r in self._app.routes
                if not (isinstance(r, Mount) and r.path == entry.mount_path)
            ]
            self._app.router.routes = self._app.routes
        # NB: we intentionally do NOT exit the session_manager_cm here.
        # AsyncExitStack only supports LIFO unwinding; popping a
        # specific entry mid-stream would corrupt the stack ordering
        # and stop OTHER streams' session managers. The contexts are
        # all torn down together at app shutdown. The unmounted route
        # means new requests 404, and the in-memory pool is stopped,
        # so the dangling session manager is harmless until shutdown.

    def status(self) -> dict[str, Any]:
        # Intentionally NOT exposing instruction text here, even on the
        # token-gated admin endpoint — defense in depth in case the token
        # ever leaks.
        return {
            "streams": [
                {
                    "stream_id": sid,
                    "mount_path": e.mount_path,
                    "stream_done": e.pool.todo_status().get("stream_done"),
                    "counts": e.pool.todo_status().get("counts_by_status", {}),
                    "flushed": e.flushed,
                    "meta": e.meta,
                }
                for sid, e in self._streams.items()
            ],
        }

    def shutdown(self) -> None:
        """Best-effort flush + stop for every active stream. Idempotent."""
        for sid in list(self._streams.keys()):
            try:
                self.flush(sid)
            except Exception as e:
                logger.warning("shutdown flush %s: %s", sid, e)
            try:
                self.unregister(sid)
            except Exception as e:
                logger.warning("shutdown unregister %s: %s", sid, e)


# ── FastMCP HTTP adapter ───────────────────────────────────────────────────


def _build_streamable_http_app(mcp: Any) -> Starlette:
    """Return a Starlette ASGI subapp that serves this FastMCP over HTTP."""
    for attr in ("streamable_http_app", "sse_app"):
        builder = getattr(mcp, attr, None)
        if builder is not None:
            sub = builder()
            if isinstance(sub, Starlette):
                return sub
            return _wrap_asgi(sub)
    raise RuntimeError(
        "mcp SDK has neither streamable_http_app() nor sse_app(); "
        "upgrade `mcp[server]` to >=1.2"
    )


def _wrap_asgi(asgi_app: Any) -> Starlette:
    star = Starlette()

    async def proxy(scope, receive, send):  # type: ignore[no-untyped-def]
        await asgi_app(scope, receive, send)

    star.router.routes.append(Mount("/", app=proxy))
    return star


# ── HTTP handlers ──────────────────────────────────────────────────────────


def _check_admin_token(request: Request, expected: str) -> Response | None:
    """Return None if request carries the right Bearer token, else 401."""
    if not expected:
        # Token not configured = admin endpoints are anonymous. Used only
        # in unit tests; production ALWAYS sets ADMIN_TOKEN.
        return None
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse(
            {"ok": False, "error": "missing Authorization: Bearer <token>"},
            401,
        )
    presented = auth.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(presented, expected):
        return JSONResponse({"ok": False, "error": "bad token"}, 403)
    return None


def _make_app(dispatcher: Dispatcher, admin_token: str) -> Starlette:

    async def register(request: Request) -> Response:
        if (resp := _check_admin_token(request, admin_token)) is not None:
            return resp
        try:
            body = await request.json()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"bad json: {e}"}, 400)

        stream_id = body.get("stream_id")
        max_wall_sec = float(body.get("max_wall_sec", 600))
        serial = bool(body.get("serial", False))
        arrivals_raw = body.get("arrivals") or []
        persist_dir = body.get("persist_dir")
        meta = body.get("meta") or {}

        try:
            arrivals = [
                Arrival(
                    task_id=str(a["task_id"]),
                    at_sec=float(a["at_sec"]),
                    instruction=str(a.get("instruction", "")),
                    deadline_sec=(
                        float(a["deadline_sec"])
                        if "deadline_sec" in a else None
                    ),
                )
                for a in arrivals_raw
            ]
        except (KeyError, TypeError, ValueError) as e:
            return JSONResponse(
                {"ok": False, "error": f"bad arrivals: {e}"}, 400
            )

        try:
            entry = await dispatcher.register(
                stream_id=stream_id,
                arrivals=arrivals,
                max_wall_sec=max_wall_sec,
                serial=serial,
                persist_dir=Path(persist_dir) if persist_dir else None,
                meta=meta,
            )
        except ValueError as e:
            code = 409 if "already" in str(e) else 400
            return JSONResponse({"ok": False, "error": str(e)}, code)

        return JSONResponse({
            "ok": True,
            "stream_id": stream_id,
            "mount_path": entry.mount_path,
            "mcp_url_path": f"{entry.mount_path}/mcp",
            "persist_dir": str(entry.persist_dir),
        })

    async def flush(request: Request) -> Response:
        if (resp := _check_admin_token(request, admin_token)) is not None:
            return resp
        stream_id = request.query_params.get("stream_id")
        if not stream_id:
            return JSONResponse(
                {"ok": False, "error": "missing stream_id"}, 400
            )
        try:
            dispatcher.flush(stream_id)
        except KeyError:
            return JSONResponse(
                {"ok": False, "error": f"unknown stream_id {stream_id!r}"},
                404,
            )
        return JSONResponse({"ok": True, "stream_id": stream_id})

    async def freeze(request: Request) -> Response:
        """Close a stream to claims and status updates. Harbor calls this
        once the agent's execution phase ends, before verification."""
        if (resp := _check_admin_token(request, admin_token)) is not None:
            return resp
        stream_id = request.query_params.get("stream_id")
        if not stream_id:
            return JSONResponse(
                {"ok": False, "error": "missing stream_id"}, 400
            )
        reason = request.query_params.get("reason") or "execution_limit"
        try:
            dispatcher.freeze(stream_id, reason)
        except KeyError:
            return JSONResponse(
                {"ok": False, "error": f"unknown stream_id {stream_id!r}"},
                404,
            )
        return JSONResponse({"ok": True, "stream_id": stream_id, "frozen": True})

    async def unregister(request: Request) -> Response:
        if (resp := _check_admin_token(request, admin_token)) is not None:
            return resp
        stream_id = request.query_params.get("stream_id")
        if not stream_id:
            return JSONResponse(
                {"ok": False, "error": "missing stream_id"}, 400
            )
        dispatcher.unregister(stream_id)
        return JSONResponse({"ok": True, "stream_id": stream_id})

    async def status(request: Request) -> Response:
        if (resp := _check_admin_token(request, admin_token)) is not None:
            return resp
        return JSONResponse({"ok": True, **dispatcher.status()})

    async def dump(request: Request) -> Response:
        """Per-task lifecycle dump for one stream — used by harbor's
        verifier to compute the strict pass-rate (only count tasks the
        agent explicitly claimed `completed`). Returns the dispatcher
        view (includes PENDING / READY tasks too, since "agent never
        claimed me" is itself a measurable signal). Token-gated."""
        if (resp := _check_admin_token(request, admin_token)) is not None:
            return resp
        stream_id = request.query_params.get("stream_id")
        if not stream_id:
            return JSONResponse(
                {"ok": False, "error": "missing stream_id"}, status_code=400
            )
        entry = dispatcher._streams.get(stream_id)
        if entry is None:
            return JSONResponse(
                {"ok": False, "error": f"unknown stream_id={stream_id}"},
                status_code=404,
            )
        return JSONResponse(
            {
                "ok": True,
                "stream_id": stream_id,
                "stream_done": entry.pool.todo_status().get("stream_done"),
                "tasks": entry.pool.todo_list(),  # internal/full view
            }
        )

    async def healthz(_: Request) -> Response:
        # Anonymous on purpose — used by docker HEALTHCHECK and harbor's
        # "is the dispatcher up yet?" wait loop. Discloses no state.
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        # Hold an AsyncExitStack open for the lifetime of the app.
        # Each registered stream's FastMCP session_manager.run() context
        # is pushed onto this stack so its task group lives as long as
        # uvicorn does.
        async with contextlib.AsyncExitStack() as stack:
            dispatcher.attach_stack(stack)
            try:
                yield
            finally:
                # Best-effort flush before stack unwinds + task groups
                # cancel. The actual unmounting happens in shutdown().
                dispatcher.shutdown()

    app = Starlette(
        routes=[
            Route("/admin/register", register, methods=["POST"]),
            Route("/admin/flush", flush, methods=["POST"]),
            Route("/admin/freeze", freeze, methods=["POST"]),
            Route("/admin/unregister", unregister, methods=["POST"]),
            Route("/admin/status", status, methods=["GET"]),
            Route("/admin/dump", dump, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    dispatcher.attach_app(app)
    return app


# ── entrypoint ─────────────────────────────────────────────────────────────


def _parse_bind(s: str) -> tuple[str, int]:
    if ":" not in s:
        raise ValueError(f"bind must be HOST:PORT, got {s!r}")
    host, port_s = s.rsplit(":", 1)
    return host, int(port_s)


async def _run(
    bind: str,
    persist_root: Path,
    admin_token: str,
) -> None:
    persist_root.mkdir(parents=True, exist_ok=True)

    # Size the default thread pool for the number of streams we multiplex.
    #
    # `todo_next` blocks for up to timeout_sec (default 60s) waiting for the
    # next arrival, and it runs via asyncio.to_thread so it does not freeze
    # the event loop (see server.py). But to_thread shares the loop's default
    # executor, whose cap is min(32, cpu_count+4) -- 32 here. One dispatcher
    # serves an entire harbor job (design D7) and the main sweep runs 200
    # concurrent trials, so with the default cap the 33rd waiting agent would
    # queue behind a thread instead of waiting on its own condition. The
    # symptom would not be an error: pool calls would just get slower the more
    # trials wait, i.e. exactly the completion-time metric the experiment
    # reports would be corrupted by harness backpressure.
    #
    # These threads are almost always parked on a condition variable, not
    # burning CPU, so a high cap costs little (~8 MB stack reserve each,
    # lazily allocated). AOS_DISPATCHER_MAX_THREADS raises it further for
    # sweeps wider than 256.
    max_threads = int(os.environ.get("AOS_DISPATCHER_MAX_THREADS", "256"))
    loop_for_executor = asyncio.get_running_loop()
    loop_for_executor.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=max_threads, thread_name_prefix="aos-pool"
        )
    )
    logger.info("dispatcher thread pool: max_workers=%d", max_threads)

    dispatcher = Dispatcher(persist_root=persist_root)
    app = _make_app(dispatcher, admin_token)

    host, port = _parse_bind(bind)
    cfg = uvicorn.Config(
        app, host=host, port=port,
        log_level="info", lifespan="on", access_log=False,
    )
    server = uvicorn.Server(cfg)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(signum: int) -> None:
        logger.info("dispatcher received signal=%d, shutting down", signum)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig)

    async def _watch_stop() -> None:
        await stop_event.wait()
        server.should_exit = True

    try:
        await asyncio.gather(server.serve(), _watch_stop())
    finally:
        dispatcher.shutdown()


def main() -> None:
    p = argparse.ArgumentParser(description="AOS-Bench dispatcher")
    p.add_argument(
        "--bind",
        default=os.environ.get("DISPATCHER_BIND", "0.0.0.0:8765"),
        help="HOST:PORT for HTTP listener (both MCP and admin paths)",
    )
    p.add_argument(
        "--persist-root",
        type=Path,
        default=Path(os.environ.get("PERSIST_ROOT", "/logs/dispatcher")),
        help="Where each stream's events.jsonl + tasks.json land on flush",
    )
    args = p.parse_args()

    admin_token = os.environ.get("ADMIN_TOKEN", "")
    if not admin_token:
        logger.warning(
            "ADMIN_TOKEN not set — /admin/* endpoints are unauthenticated. "
            "DO NOT run without ADMIN_TOKEN in production."
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    asyncio.run(_run(
        bind=args.bind,
        persist_root=args.persist_root,
        admin_token=admin_token,
    ))


if __name__ == "__main__":
    main()

