"""Lightweight in-process resource sampler.

Samples the Octopus process plus its direct children (chiefly the
``Bash`` tool's subprocesses) at a fixed interval. On stop, aggregates:

  - ``memory_peak_mb``: max RSS observed across the process + its
    children at any single sample
  - ``cpu_time_ms``: cumulative user + system CPU including reaped
    children (via ``os.times()``)
  - ``samples_count``: number of successful samples

Stdlib only. RSS reads from ``/proc/<pid>/status`` (Linux); on platforms
without ``/proc`` the sampler degrades to CPU-only and ``memory_peak_mb``
stays at 0.

Known limitations (documented for honesty, not fixed in M3):
  - Only one level of children is walked. Grandchildren spawned by
    Bash via setsid / detach are missed for RSS, though their CPU is
    captured if Python ends up reaping them.
  - This is a *single-Run sampler* attached to the root run only.
    Per-subagent CPU/RSS attribution would need cgroup or pidns
    delegation; out of scope.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .trace import Run, TraceWriter

logger = logging.getLogger("octopus.resource_monitor")

_PROC = Path("/proc")


def _read_self_rss_kb() -> int:
    try:
        with (_PROC / "self" / "status").open() as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except FileNotFoundError:
        pass
    return 0


def _read_children_rss_kb(parent_pid: int) -> int:
    """Return summed VmRSS (kB) of processes whose PPid == parent_pid."""
    if not _PROC.exists():
        return 0
    total = 0
    try:
        entries = os.listdir(_PROC)
    except OSError:
        return 0
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with (_PROC / entry / "status").open() as f:
                ppid = None
                rss = 0
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                    elif line.startswith("VmRSS:"):
                        rss = int(line.split()[1])
                    if ppid is not None and rss:
                        break
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if ppid == parent_pid:
            total += rss
    return total


def _sample_total_cpu_seconds() -> float:
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


class ProcessSampler:
    """Periodic asyncio sampler. Owns one background task."""

    def __init__(
        self,
        *,
        trace: TraceWriter,
        run: Run,
        interval_s: float = 1.0,
    ) -> None:
        self._trace = trace
        self._run = run
        self._interval_s = max(0.1, float(interval_s))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._self_pid = os.getpid()
        self._cpu_start_s = _sample_total_cpu_seconds()
        self._max_rss_kb = 0
        self._samples = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._loop(), name="octopus-resource-sampler"
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._sample_once()  # one final read so a fast run still records data
        self._flush_to_run()

    def _sample_once(self) -> None:
        rss_total_kb = _read_self_rss_kb() + _read_children_rss_kb(self._self_pid)
        if rss_total_kb > self._max_rss_kb:
            self._max_rss_kb = rss_total_kb
        self._samples += 1

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._sample_once()
            except Exception:
                logger.debug("resource sampler tick failed", exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_s
                )
            except asyncio.TimeoutError:
                continue

    def _flush_to_run(self) -> None:
        cpu_total_s = _sample_total_cpu_seconds() - self._cpu_start_s
        usage = {
            "cpu_time_ms": int(cpu_total_s * 1000),
            "memory_peak_mb": int(round(self._max_rss_kb / 1024)),
            "resource_samples": self._samples,
            "resource_scope": "process_group",
        }
        # finish_run already filled wall_time_ms; merge ours alongside.
        self._run.resource_usage.update(usage)
