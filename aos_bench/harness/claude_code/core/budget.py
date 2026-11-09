"""Shared resource budget for the team + live container-load readers.

The whole team runs inside ONE sandbox container capped at ``cpus``/``mem_mb``
(via harbor's --override-cpus/--override-memory-mb). ``ResourceBudget`` carries
those numbers and reads live usage from /proc so the lead can schedule
concurrency under contention. Engine-agnostic — no SDK imports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def parse_mem_used_mb(meminfo: str) -> int:
    """used = MemTotal - MemAvailable, in MB, parsed from /proc/meminfo text."""
    fields: dict[str, int] = {}
    for line in meminfo.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        kb = parts[1].strip().split()
        if kb and kb[0].isdigit():
            fields[parts[0].strip()] = int(kb[0])
    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable", fields.get("MemFree", 0))
    return round((total - available) / 1024)


def parse_load_avg_1m(loadavg: str) -> float:
    """1-minute load average, the first field of /proc/loadavg."""
    try:
        return float(loadavg.split()[0])
    except (IndexError, ValueError):
        return 0.0


def _read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


@dataclass
class ResourceBudget:
    """The team's shared CPU/memory budget for this container."""

    cpus: int
    mem_mb: int

    @classmethod
    def from_args(cls, cpus: int | None, mem_mb: int | None) -> "ResourceBudget":
        """Build from explicit values, falling back to the live container."""
        return cls(
            cpus=cpus if cpus and cpus > 0 else (os.cpu_count() or 1),
            mem_mb=mem_mb
            if mem_mb and mem_mb > 0
            else parse_mem_total_mb(_read("/proc/meminfo")),
        )

    def snapshot(
        self,
        *,
        meminfo: str | None = None,
        loadavg: str | None = None,
        live_workers: int,
    ) -> dict:
        """Budget + live load. /proc is read unless fixtures are passed (tests)."""
        meminfo = meminfo if meminfo is not None else _read("/proc/meminfo")
        loadavg = loadavg if loadavg is not None else _read("/proc/loadavg")
        return {
            "cpu_cores": self.cpus,
            "mem_total_mb": self.mem_mb,
            "mem_used_mb": parse_mem_used_mb(meminfo),
            "load_avg_1m": parse_load_avg_1m(loadavg),
            "live_workers": live_workers,
        }


def parse_mem_total_mb(meminfo: str) -> int:
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            kb = line.split(":")[1].strip().split()
            if kb and kb[0].isdigit():
                return round(int(kb[0]) / 1024)
    return 0
