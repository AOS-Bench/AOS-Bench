"""Claude-Agent-SDK solo harness for AOS-Bench stream trials.

``core/`` is engine-agnostic coordination; ``engines/claude`` holds the
Claude-specific session execution. Entry point: ``python -m
aos_bench.harness.claude_code --solo``.
"""

from .core.coordinator import Coordinator
from .core.orchestrator import run_solo

__all__ = ["Coordinator", "run_solo"]
