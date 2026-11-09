from __future__ import annotations

from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState


def _make_state() -> MonitorState:
    s = MonitorState(jobs_dir="/tmp/x")
    s.ensure_trial("t1", "/tmp/x/t1")
    s.ensure_run("t1", "run_root_001", role="root", agent_name="orchestrator")
    return s


def test_gateway_404_rule_matches() -> None:
    s = _make_state()
    d = ErrorDetector(s)
    ev = {
        "timestamp": 1.0,
        "kind": "tool_result",
        "payload": {
            "return_value": {
                "extras": {},
                "stdout": "RPC error -32003: 'url.not_found', 'method': 'POST', "
                          "'url': '/anthropic/v1/chat/completions'",
            },
        },
    }
    d.scan(trial_id="t1", run_id="run_root_001", events=[ev])
    errs = s.errors_snapshot()
    assert len(errs) == 1
    assert errs[0]["rule_id"] == "gateway_404"


def test_notification_severity_warning() -> None:
    s = _make_state()
    d = ErrorDetector(s)
    ev = {
        "timestamp": 2.0,
        "kind": "notification",
        "payload": {"severity": "warning", "title": "kv-store killed", "body": "background subagent terminated"},
    }
    d.scan(trial_id="t1", run_id="run_root_001", events=[ev])
    errs = s.errors_snapshot()
    assert len(errs) == 1
    assert errs[0]["rule_id"] == "notification_severe"
    assert "kv-store killed" in errs[0]["snippet"]


def test_normal_status_does_not_flag() -> None:
    s = _make_state()
    d = ErrorDetector(s)
    ev = {
        "timestamp": 3.0,
        "kind": "status",
        "payload": {"context_tokens": 5000, "token_usage": {"input_other": 100, "output": 30}},
    }
    d.scan(trial_id="t1", run_id="run_root_001", events=[ev])
    assert s.errors_snapshot() == []


def test_event_marked_error_flag() -> None:
    s = _make_state()
    d = ErrorDetector(s)
    ev = {"timestamp": 4.0, "kind": "tool_result", "payload": {"error": True, "msg": "boom"}}
    d.scan(trial_id="t1", run_id="run_root_001", events=[ev])
    errs = s.errors_snapshot()
    assert len(errs) == 1
    assert errs[0]["rule_id"] == "event_marked_error"
