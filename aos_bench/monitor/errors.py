"""Rule-based error detection over normalized wire events.

Each ``ErrorRule`` inspects a single event and returns a snippet if matched,
or ``None``. ``ErrorDetector.scan`` feeds events to all rules and emits an
``ErrorRecord`` per match.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from aos_bench.monitor.state import ErrorRecord, MonitorState

# ── Rule helpers ─────────────────────────────────────────────────────────

_GATEWAY_404_PAT = re.compile(r"anthropic/v1/chat/completions", re.IGNORECASE)
_GENERIC_ERR_PAT = re.compile(r"\b(exception|traceback|fatal)\b", re.IGNORECASE)


def _flatten(value: object) -> str:
    """Coerce arbitrary payload value to a string for substring matching."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            import json

            return json.dumps(value, ensure_ascii=False)[:4000]
        except (TypeError, ValueError):
            return str(value)[:4000]
    return str(value)


@dataclass
class ErrorRule:
    rule_id: str
    apply: Callable[[dict[str, object]], str | None]

    def check(self, event: dict[str, object]) -> str | None:
        try:
            return self.apply(event)
        except Exception:
            return None


def _rule_event_marked_error(event: dict[str, object]) -> str | None:
    payload = event.get("payload") or {}
    if isinstance(payload, dict) and payload.get("error") is True:
        return _flatten(payload)[:200]
    if event.get("kind") == "error":
        return _flatten(payload)[:200]
    return None


def _rule_status_failed_count(event: dict[str, object]) -> str | None:
    if event.get("kind") != "status":
        return None
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    tu = payload.get("token_usage")
    if isinstance(tu, dict):
        failed = tu.get("failed") or tu.get("errors")
        if isinstance(failed, int) and failed > 0:
            return f"token_usage.failed={failed}"
    return None


def _rule_tool_result_error_text(event: dict[str, object]) -> str | None:
    if event.get("kind") != "tool_result":
        return None
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    return_value = payload.get("return_value")
    body = _flatten(return_value)
    if not body:
        return None
    if _GENERIC_ERR_PAT.search(body):
        m = _GENERIC_ERR_PAT.search(body)
        # Snippet around the match.
        start = max(0, (m.start() if m else 0) - 40)
        return body[start : start + 200]
    return None


def _rule_gateway_404(event: dict[str, object]) -> str | None:
    """A gateway 404 signature: an Anthropic-format request misrouted to a chat-completions path."""
    payload = event.get("payload") or {}
    body = _flatten(payload)
    if _GATEWAY_404_PAT.search(body):
        m = _GATEWAY_404_PAT.search(body)
        start = max(0, (m.start() if m else 0) - 40)
        return body[start : start + 200]
    return None


def _rule_notification_severe(event: dict[str, object]) -> str | None:
    if event.get("kind") != "notification":
        return None
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    severity = (payload.get("severity") or "").lower()
    if severity in {"warning", "error"}:
        title = payload.get("title") or ""
        body = payload.get("body") or ""
        snippet = f"{severity}: {title} — {body}"[:200]
        return snippet
    return None


DEFAULT_RULES: list[ErrorRule] = [
    ErrorRule("event_marked_error", _rule_event_marked_error),
    ErrorRule("status_failed_count", _rule_status_failed_count),
    ErrorRule("tool_result_error_text", _rule_tool_result_error_text),
    ErrorRule("gateway_404", _rule_gateway_404),
    ErrorRule("notification_severe", _rule_notification_severe),
]


class ErrorDetector:
    def __init__(self, state: MonitorState, rules: list[ErrorRule] | None = None) -> None:
        self.state = state
        self.rules = rules or DEFAULT_RULES

    def scan(
        self,
        *,
        trial_id: str,
        run_id: str | None,
        events: Iterable[dict[str, object]],
    ) -> None:
        for ev in events:
            for rule in self.rules:
                snippet = rule.check(ev)
                if snippet is None:
                    continue
                ts = ev.get("timestamp")
                self.state.add_error(
                    ErrorRecord(
                        trial_id=trial_id,
                        run_id=run_id,
                        ts=float(ts) if isinstance(ts, (int, float)) else time.time(),
                        rule_id=rule.rule_id,
                        snippet=snippet,
                    )
                )
                # One rule match per event; the loudest signal wins.
                break
