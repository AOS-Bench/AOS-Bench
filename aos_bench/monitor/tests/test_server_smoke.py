from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aos_bench.monitor.server import create_app
from aos_bench.monitor.state import ProcessInfo, ProcessSnapshot


def test_server_health(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    app = create_app(jobs_dir=jobs_dir, scan_interval_s=10.0, resource_interval_s=10.0)
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_server_jobs_endpoint(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    app = create_app(jobs_dir=jobs_dir, scan_interval_s=10.0, resource_interval_s=10.0)
    with TestClient(app) as client:
        r = client.get("/api/jobs")
        assert r.status_code == 200
        data = r.json()
        assert data["jobs_dir"] == str(jobs_dir)


def test_server_trials_initially_empty(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    app = create_app(jobs_dir=jobs_dir, scan_interval_s=10.0, resource_interval_s=10.0)
    with TestClient(app) as client:
        r = client.get("/api/trials")
        assert r.status_code == 200
        assert r.json() == []


def test_server_trial_detail_404(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    app = create_app(jobs_dir=jobs_dir, scan_interval_s=10.0, resource_interval_s=10.0)
    with TestClient(app) as client:
        r = client.get("/api/trials/does-not-exist")
        assert r.status_code == 404


def test_static_index_served(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    app = create_app(jobs_dir=jobs_dir, scan_interval_s=10.0, resource_interval_s=10.0)
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "AOSBench Eval Monitor" in r.text


def test_large_static_asset_is_gzipped(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    app = create_app(jobs_dir=jobs_dir, scan_interval_s=10.0, resource_interval_s=10.0)
    with TestClient(app) as client:
        r = client.get("/static/app.js", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert r.headers["content-encoding"] == "gzip"


def test_trial_process_endpoint_returns_latest_only(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    app = create_app(jobs_dir=jobs_dir, scan_interval_s=10.0, resource_interval_s=10.0)
    state = app.state.monitor_state
    state.ensure_trial("t1", str(jobs_dir / "t1"), agent="claude-code")
    state.add_trial_process_snapshot(
        "t1",
        ProcessSnapshot(
            ts=1.0,
            container_name="t1-main-1",
            attribution_mode="shared_runtime",
            processes=[
                ProcessInfo(1, 0, 2.0, 10.0, 5, "node", "node claude")
            ],
        ),
    )
    with TestClient(app) as client:
        data = client.get("/api/trials/t1/processes").json()
        assert data["sample_count"] == 1
        assert data["latest"]["attribution_mode"] == "shared_runtime"
        assert data["peaks"][0]["peak_cpu_percent"] == 2.0
        assert data["peaks"][0]["peak_mem_rss_mb"] == 10.0
        assert data["samples"] == []
