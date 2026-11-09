"""AOS-Bench task pool — wall-clock-driven stream task dispenser.

See `docs/aos_bench.md#v260426` for design.
"""

from aos_bench.pool.pool import (
    AGENT_SETTABLE_STATUSES,
    COMPLETED,
    FAILED,
    IN_PROGRESS,
    PENDING,
    READY,
    TERMINAL_STATUSES,
    TIMEOUT,
    Arrival,
    TaskPool,
    load_schedule,
)

__all__ = [
    "AGENT_SETTABLE_STATUSES",
    "Arrival",
    "COMPLETED",
    "FAILED",
    "IN_PROGRESS",
    "PENDING",
    "READY",
    "TERMINAL_STATUSES",
    "TIMEOUT",
    "TaskPool",
    "load_schedule",
]
