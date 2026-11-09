"""Engine-agnostic team coordination: shared in-process state + message router.

``Coordinator`` mirrors the official inbox envelope
``{from,to,summary,text,timestamp,read}`` and supports dynamic worker spawning.
Nothing here imports an agent engine — it is pure coordination state.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .prompts import LEAD_NAME

INBOX_POLL_SEC = 90.0
SHUTDOWN_TEXT = "SHUTDOWN"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


@dataclass
class Coordinator:
    """Shared state + message router for one team run."""

    members: list[str]  # teammate names (excludes the lead); grows on spawn
    log_dir: Path
    max_workers: int = 0  # 0 = unlimited; hard cap on total spawned teammates
    inboxes: dict[str, asyncio.Queue] = field(default_factory=dict)
    shutdown_requested: set[str] = field(default_factory=set)
    lead_done: bool = False
    # wfsdk only: flipped True once any single-task worker observes the pool's
    # `stream_done`. The top-up guard stops launching replacement workers once
    # this is set (the stream is drained; no more work to claim).
    stream_drained: bool = False
    _events_fh: Any = None
    # Dynamic spawning: the lead creates teammates on demand. run_team wires the
    # live TaskGroup + a factory that builds a run_worker() coroutine for a name.
    _tg: Any = field(default=None, init=False)
    _worker_factory: Any = field(default=None, init=False)
    _worker_tasks: dict[str, Any] = field(default_factory=dict, init=False)
    _worker_seq: int = field(default=0, init=False)
    # wfsdk only: a width-N semaphore that gates INSTANTANEOUS worker concurrency
    # (matching the official Workflow runtime's `min(16, max(2, cores-2))` cap),
    # so the planner can fan out MANY single-task workers that queue behind it —
    # decoupling "total agents launched" from "how many run at once". None in
    # team/solo modes (no gating). Set by run_wfsdk via set_concurrency().
    _concurrency: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        for name in [LEAD_NAME, *self.members]:
            self.inboxes[name] = asyncio.Queue()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._events_fh = open(self.log_dir / "events.jsonl", "a")

    def set_concurrency(self, width: int) -> None:
        """Install a width-N semaphore gating instantaneous worker concurrency.

        wfsdk uses this so MANY single-task workers can be launched (one per
        ready task) while only ``width`` run at once — mirroring the official
        Workflow executor, where ``parallel()`` enqueues every item but a
        ``min(16, max(2, cores-2))`` semaphore caps concurrency. A worker that
        stalls on one hard task then blocks only ``1/N`` of throughput instead
        of a fixed fraction of a tiny worker set.
        """
        self._concurrency = asyncio.Semaphore(max(1, int(width)))

    def spawn_worker(self) -> dict[str, Any]:
        """Create + launch one new teammate session, returning its name.

        Called by the lead's ``spawn_worker`` tool (and by run_team for any
        initial pre-spawn). The inbox is registered synchronously before the
        task is created, so the lead may ``send_message`` to the new name
        immediately without racing the worker's startup.
        """
        if self._tg is None or self._worker_factory is None:
            return {"ok": False, "error": "dynamic spawning unavailable"}
        if self.max_workers and len(self.members) >= self.max_workers:
            return {
                "ok": False,
                "error": (
                    f"worker limit {self.max_workers} reached; reuse an idle "
                    "teammate instead of spawning a new one"
                ),
            }
        self._worker_seq += 1
        name = f"worker{self._worker_seq}"
        self.inboxes[name] = asyncio.Queue()
        self.members.append(name)
        self._worker_tasks[name] = self._tg.create_task(self._worker_factory(name))
        self._log("spawn_worker", name=name, total=len(self.members))
        return {"ok": True, "name": name}

    # ---- routing -------------------------------------------------------
    def _log(self, kind: str, **fields: Any) -> None:
        if self._events_fh is None:
            return
        rec = {"ts": time.time(), "iso": _now_iso(), "kind": kind, **fields}
        self._events_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._events_fh.flush()

    def close(self) -> None:
        """Close the events.jsonl handle. Idempotent; safe to call in a finally."""
        fh = self._events_fh
        self._events_fh = None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def known(self, name: str) -> bool:
        return name in self.inboxes

    def send(self, frm: str, to: str, summary: str, text: str) -> dict[str, Any]:
        if not self.known(to):
            return {
                "ok": False,
                "error": f"unknown recipient '{to}'. Known: {list(self.inboxes)}",
            }
        envelope = {
            "from": frm,
            "to": to,
            "summary": summary,
            "text": text,
            "timestamp": _now_iso(),
            "read": False,
        }
        self.inboxes[to].put_nowait(envelope)
        if text.strip().upper().startswith(SHUTDOWN_TEXT):
            self.shutdown_requested.add(to)
            if (
                to in self.members
                and self.members
                and all(m in self.shutdown_requested for m in self.members)
            ):
                self.lead_done = True
        self._log(
            "send_message",
            **{k: envelope[k] for k in ("from", "to", "summary", "text")},
        )
        return {"ok": True}

    async def recv(self, me: str, timeout: float) -> list[dict[str, Any]]:
        q = self.inboxes[me]
        msgs: list[dict[str, Any]] = []
        try:
            first = await asyncio.wait_for(q.get(), timeout=timeout)
            msgs.append(first)
        except asyncio.TimeoutError:
            return []
        # drain anything else already queued
        while not q.empty():
            msgs.append(q.get_nowait())
        for m in msgs:
            m["read"] = True
            self._log(
                "deliver", to=me, **{k: m[k] for k in ("from", "summary", "text")}
            )
        return msgs

    def broadcast_shutdown(self, frm: str = LEAD_NAME) -> None:
        for name in self.members:
            if name not in self.shutdown_requested:
                self.send(frm, name, "shutdown", SHUTDOWN_TEXT)
