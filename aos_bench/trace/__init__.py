from .claude_code import collect_claude_code_agent_trace
from .codex import collect_codex_agent_trace
from .opencode import collect_opencode_agent_trace

__all__ = [
    "collect_claude_code_agent_trace",
    "collect_codex_agent_trace",
    "collect_opencode_agent_trace",
]

# kimi-cli converter ships in this package too but isn't yet committed; let
# tooling import the module path directly when the file is on disk and skip
# silently when it isn't (e.g. a fresh clone before the kimi-cli adapter
# lands).
try:  # pragma: no cover — covered by kimi-cli's own tests when present
    from .kimi_cli import (  # noqa: F401
        collect_kimi_cli_agent_trace,
        find_kimi_cli_latest_session_dir,
        find_kimi_cli_session_dir,
    )

    __all__ += [
        "collect_kimi_cli_agent_trace",
        "find_kimi_cli_latest_session_dir",
        "find_kimi_cli_session_dir",
    ]
except ImportError:
    pass

# Same guard for the kimi-code converter (a different product from kimi-cli —
# see docs/tasks/kimi_code_integration.md §1.1), so a tree without it still
# imports.
try:  # pragma: no cover — covered by kimi-code's own tests when present
    from .kimi_code import (  # noqa: F401
        KimiCodeSession,
        collect_kimi_code_agent_trace,
        find_kimi_code_latest_session_dir,
        find_kimi_code_session_dir,
        normalize_kimi_code_records,
        parse_kimi_code_wire_line,
        read_kimi_code_session,
    )

    __all__ += [
        "KimiCodeSession",
        "collect_kimi_code_agent_trace",
        "find_kimi_code_latest_session_dir",
        "find_kimi_code_session_dir",
        "normalize_kimi_code_records",
        "parse_kimi_code_wire_line",
        "read_kimi_code_session",
    ]
except ImportError:
    pass
