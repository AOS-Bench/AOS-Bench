"""Unit tests for the AOS-Bench dispatcher (v0.3 universal HTTP MCP)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from aos_bench.pool.dispatcher import (
    Dispatcher,
    _make_app,
    _validate_stream_id,
)


def _build_client(
    tmp_path: Path, admin_token: str = "",
) -> tuple[Dispatcher, TestClient]:
    persist = tmp_path / "logs" / "dispatcher"
    persist.mkdir(parents=True, exist_ok=True)
    dispatcher = Dispatcher(persist_root=persist)
    app = _make_app(dispatcher, admin_token)
    return dispatcher, TestClient(app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


class TestStreamIdValidation:
    @pytest.mark.parametrize("sid", [
        "tb2-swe-3-42",
        "job_2026__trial_001",
        "x.y.z",
        "A1B2",
    ])
    def test_accepts_safe(self, sid: str) -> None:
        _validate_stream_id(sid)

    @pytest.mark.parametrize("sid", [
        "",
        "a/b",
        "x" * 200,
        "with space",
        "$(rm -rf /)",
    ])
    def test_rejects_bad(self, sid: str) -> None:
        with pytest.raises(ValueError):
            _validate_stream_id(sid)


class TestAdminEndpoints:
    def test_register_then_status(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path)
        body = {
            "stream_id": "s1",
            "max_wall_sec": 60,
            "serial": False,
            "arrivals": [
                {"task_id": "t1", "at_sec": 0, "instruction": "do thing"},
            ],
        }
        r = c.post("/admin/register", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["stream_id"] == "s1"
        assert data["mount_path"] == "/s1"
        assert data["mcp_url_path"] == "/s1/mcp"

        r = c.get("/admin/status")
        assert r.status_code == 200
        streams = r.json()["streams"]
        assert len(streams) == 1
        assert streams[0]["stream_id"] == "s1"

    def test_status_does_not_leak_instruction(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path)
        c.post("/admin/register", json={
            "stream_id": "secret",
            "arrivals": [{
                "task_id": "t1",
                "at_sec": 0,
                "instruction": "MY_SECRET_TASK_INSTRUCTION_DO_NOT_LEAK",
            }],
        })
        r = c.get("/admin/status")
        assert "MY_SECRET_TASK_INSTRUCTION_DO_NOT_LEAK" not in r.text

    def test_duplicate_register_returns_409(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path)
        body = {
            "stream_id": "dup",
            "arrivals": [{"task_id": "t1", "at_sec": 0, "instruction": ""}],
        }
        c.post("/admin/register", json=body)
        r = c.post("/admin/register", json=body)
        assert r.status_code == 409, r.text

    def test_bad_stream_id_returns_400(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path)
        r = c.post("/admin/register", json={
            "stream_id": "bad/id",
            "arrivals": [],
        })
        assert r.status_code == 400, r.text

    def test_bad_arrivals_returns_400(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path)
        r = c.post("/admin/register", json={
            "stream_id": "s2",
            "arrivals": [{"missing_task_id": True}],
        })
        assert r.status_code == 400, r.text

    def test_freeze_unknown_returns_404(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path)
        r = c.post("/admin/freeze?stream_id=nope")
        assert r.status_code == 404, r.text

    def test_freeze_closes_the_stream(self, tmp_path: Path) -> None:
        dispatcher, c = _build_client(tmp_path)
        c.post("/admin/register", json={
            "stream_id": "fz",
            "max_wall_sec": 60,
            "arrivals": [{"task_id": "t1", "at_sec": 0, "instruction": "x"}],
        })
        r = c.post("/admin/freeze?stream_id=fz")
        assert r.status_code == 200, r.text
        assert r.json()["frozen"] is True
        pool = dispatcher._streams["fz"].pool
        assert pool.todo_next(timeout_sec=0.1)["task_id"] == "<FINISH>"

    def test_freeze_requires_admin_token(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path, admin_token="s3cret")
        r = c.post("/admin/freeze?stream_id=fz")
        assert r.status_code in (401, 403), r.text

    def test_flush_unknown_returns_404(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path)
        r = c.post("/admin/flush?stream_id=nope")
        assert r.status_code == 404, r.text

    def test_flush_writes_state(self, tmp_path: Path) -> None:
        dispatcher, c = _build_client(tmp_path)
        c.post("/admin/register", json={
            "stream_id": "fl",
            "max_wall_sec": 60,
            "arrivals": [{"task_id": "t1", "at_sec": 0, "instruction": "x"}],
        })
        r = c.post("/admin/flush?stream_id=fl")
        assert r.status_code == 200, r.text
        persist_dir = dispatcher._streams["fl"].persist_dir
        assert persist_dir is not None
        assert (persist_dir / "tasks.json").exists()
        body = json.loads((persist_dir / "tasks.json").read_text())
        assert any(t["task_id"] == "t1" for t in body["tasks"])

    def test_unregister_removes_stream(self, tmp_path: Path) -> None:
        dispatcher, c = _build_client(tmp_path)
        c.post("/admin/register", json={
            "stream_id": "u1",
            "arrivals": [{"task_id": "t1", "at_sec": 0, "instruction": ""}],
        })
        assert "u1" in dispatcher._streams
        r = c.post("/admin/unregister?stream_id=u1")
        assert r.status_code == 200, r.text
        assert "u1" not in dispatcher._streams

    def test_healthz_anonymous(self, tmp_path: Path) -> None:
        # healthz must work even with token configured (HEALTHCHECK has no
        # token).
        _, c = _build_client(tmp_path, admin_token="any-token")
        assert c.get("/healthz").json() == {"ok": True}


class TestAdminTokenAuth:
    def test_register_requires_bearer(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path, admin_token="my-token")
        r = c.post("/admin/register", json={
            "stream_id": "tok1",
            "arrivals": [{"task_id": "t1", "at_sec": 0, "instruction": ""}],
        })
        assert r.status_code == 401, r.text

    def test_register_rejects_wrong_token(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path, admin_token="my-token")
        r = c.post(
            "/admin/register",
            json={
                "stream_id": "tok2",
                "arrivals": [{"task_id": "t1", "at_sec": 0, "instruction": ""}],
            },
            headers=_auth("WRONG"),
        )
        assert r.status_code == 403, r.text

    def test_register_accepts_correct_token(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path, admin_token="my-token")
        r = c.post(
            "/admin/register",
            json={
                "stream_id": "tok3",
                "arrivals": [{"task_id": "t1", "at_sec": 0, "instruction": ""}],
            },
            headers=_auth("my-token"),
        )
        assert r.status_code == 200, r.text

    def test_status_requires_token_too(self, tmp_path: Path) -> None:
        _, c = _build_client(tmp_path, admin_token="my-token")
        r = c.get("/admin/status")
        assert r.status_code == 401, r.text


class TestMcpMounting:
    def test_register_mounts_mcp_subapp(self, tmp_path: Path) -> None:
        dispatcher, c = _build_client(tmp_path)
        c.post("/admin/register", json={
            "stream_id": "m1",
            "arrivals": [{"task_id": "t1", "at_sec": 0, "instruction": ""}],
        })
        assert any(
            getattr(r, "path", None) == "/m1"
            for r in dispatcher._app.routes
        )

    def test_unregister_unmounts(self, tmp_path: Path) -> None:
        dispatcher, c = _build_client(tmp_path)
        c.post("/admin/register", json={
            "stream_id": "m2",
            "arrivals": [{"task_id": "t1", "at_sec": 0, "instruction": ""}],
        })
        c.post("/admin/unregister?stream_id=m2")
        assert not any(
            getattr(r, "path", None) == "/m2"
            for r in dispatcher._app.routes
        )

    def test_two_streams_isolated(self, tmp_path: Path) -> None:
        dispatcher, c = _build_client(tmp_path)
        for sid in ("alpha", "beta"):
            c.post("/admin/register", json={
                "stream_id": sid,
                "arrivals": [{
                    "task_id": f"{sid}-only-task",
                    "at_sec": 0,
                    "instruction": f"task for {sid}",
                }],
            })
        assert set(dispatcher._streams.keys()) == {"alpha", "beta"}
        assert dispatcher._streams["alpha"].pool is not dispatcher._streams["beta"].pool

    def test_shutdown_flushes_all(self, tmp_path: Path) -> None:
        dispatcher, c = _build_client(tmp_path)
        for sid in ("a", "b"):
            c.post("/admin/register", json={
                "stream_id": sid,
                "arrivals": [{"task_id": "t", "at_sec": 0, "instruction": ""}],
            })
        persist_dirs = {
            sid: e.persist_dir
            for sid, e in dispatcher._streams.items()
        }
        dispatcher.shutdown()
        for sid, persist_dir in persist_dirs.items():
            assert persist_dir is not None
            assert (persist_dir / "tasks.json").exists(), (
                f"shutdown should have flushed {sid}"
            )
        assert dispatcher._streams == {}
