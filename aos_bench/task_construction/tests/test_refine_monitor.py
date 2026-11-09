import json
import re
import shutil
import subprocess
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from aos_bench.task_construction.refine_monitor import _render_page, collect_state, make_handler


def test_collect_state_reports_progress_registry_and_errors(tmp_path: Path, monkeypatch) -> None:
    candidate = tmp_path / "results" / "medium" / "medium-001"
    logs = candidate / "logs" / "attempt-02"
    logs.mkdir(parents=True)
    (logs / "build.log").write_text("step\nERROR package unavailable\n")
    (candidate / "state.json").write_text(
        json.dumps(
            {
                "attempt": 2,
                "codex_turn": 7,
                "status": "verifying-build",
                "session_id": "session-1",
                "rounds": [
                    {
                        "build": {
                            "summary": "Docker build failed (rc=1)",
                            "details": {"error_signals": ["E: package unavailable"]},
                        }
                    }
                ],
            }
        )
    )
    (tmp_path / "build-valid-streams.json").write_text(
        json.dumps({"streams": [{"bucket": "medium", "name": "medium-001"}]})
    )
    monkeypatch.setattr(
        "aos_bench.task_construction.refine_monitor.active_run_dirs",
        lambda: {str(candidate.resolve()): [123]},
    )

    state = collect_state(tmp_path, max_rounds=48)

    assert state["summary"]["total"] == 1
    assert state["summary"]["active"] == 1
    assert state["summary"]["build_valid"] == 1
    loop = state["loops"][0]
    assert loop["codex_turn"] == 7
    assert loop["round_count"] == 1
    assert loop["progress_percent"] == 100 * 7 / 48
    assert loop["build_valid"] is True
    assert loop["pids"] == [123]
    assert "package unavailable" in loop["errors"][0]


def test_collect_state_ignores_malformed_json(tmp_path: Path, monkeypatch) -> None:
    candidate = tmp_path / "results" / "heavy" / "heavy-001"
    candidate.mkdir(parents=True)
    (candidate / "state.json").write_text("not json")
    monkeypatch.setattr("aos_bench.task_construction.refine_monitor.active_run_dirs", lambda: {})

    state = collect_state(tmp_path)

    assert state["summary"]["total"] == 1
    assert state["loops"][0]["status"] == "unknown"


def test_monitor_health_supports_get_and_head(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path, 48, 5))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base}/healthz", timeout=2) as response:
            assert json.load(response) == {"ok": True}
        request = urllib.request.Request(f"{base}/", method="HEAD")
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_rendered_monitor_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    page = _render_page(tmp_path, 48, 5).decode()
    match = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    assert match is not None

    result = subprocess.run(
        [node, "--check"],
        input=match.group(1),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
