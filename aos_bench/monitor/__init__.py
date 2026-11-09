"""AOSBench live Eval Monitor.

Supported agents: kimi-cli (``tail/kimi.py`` + ``tail/parser.py``) and
claude-code (``tail/claude.py`` + ``tail/claude_parser.py``). Both tails feed
one canonical event shape into ``MonitorState`` so the dashboard is
agent-agnostic. See ``docs/framework/monitor.md``.
"""

from aos_bench.monitor.server import create_app
from aos_bench.monitor.state import (
    ContainerSample,
    ErrorRecord,
    MonitorState,
    ResourceSample,
    RunSummary,
    TrialSummary,
)

__all__ = [
    "create_app",
    "MonitorState",
    "RunSummary",
    "TrialSummary",
    "ErrorRecord",
    "ResourceSample",
    "ContainerSample",
]
