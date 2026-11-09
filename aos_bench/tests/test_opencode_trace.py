"""Tests for the opencode -> AgentTrace converter.

Uses a fabricated JSONL fixture that mirrors what the patched
``opencode run --format=json`` (see ``aos_bench/harness/opencode``'s
``cli/cmd/run.ts``) emits when the prompt forces the model to delegate
to a subagent via the ``task`` tool.
"""

from __future__ import annotations

import json
from pathlib import Path

from aos_bench.trace import collect_opencode_agent_trace


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


def _make_subagent_jsonl(path: Path) -> None:
    """Mimic a real opencode session that fires off a subagent.

    Timeline (sessionID prefix → readable label):

      ses_root_a  → root session
      ses_sub_b   → child spawned via the task tool

    Layout (timestamps in ms, opencode emits them via ``Date.now()``):

      t=0      session_created(ses_root_a, parent=null)
      t=10     session_status(ses_root_a, running)
      t=20     step_start(ses_root_a)
      t=30     tool_use(ses_root_a, task, status=running, child=ses_sub_b)
               -- emitted as subagent_spawn separately --
      t=30     subagent_spawn(parent=ses_root_a, tool_call_id=tc_root_1,
                              child_session_id=ses_sub_b)
      t=40     session_created(ses_sub_b, parent=ses_root_a)
      t=50     step_start(ses_sub_b)
      t=60     tool_use(ses_sub_b, read, completed)
      t=70     text(ses_sub_b, "found the bug at line 42")
      t=80     step_finish(ses_sub_b, tokens={input:200, output:50}, cost=0.001)
      t=90     session_status(ses_sub_b, idle)
      t=100    tool_use(ses_root_a, task, completed)   # subagent returned
      t=110    text(ses_root_a, "Subagent says line 42; here's the patch")
      t=120    step_finish(ses_root_a, tokens={input:1000, output:200}, cost=0.005)
      t=130    session_status(ses_root_a, idle)
    """
    root = "ses_root_a"
    child = "ses_sub_b"
    rows: list[dict] = [
        {
            "type": "session_created",
            "timestamp": 0,
            "sessionID": root,
            "parentID": None,
            "info": {
                "id": root,
                "parentID": None,
                "title": "Fix the auth bug",
                "agent": "build",
                "modelID": "z-ai/glm-4.7",
                "providerID": "openrouter",
            },
        },
        {
            "type": "session_status",
            "timestamp": 10,
            "sessionID": root,
            "status": {"type": "running"},
        },
        {
            "type": "step_start",
            "timestamp": 20,
            "sessionID": root,
            "part": {"type": "step-start", "sessionID": root},
        },
        {
            "type": "tool_use",
            "timestamp": 30,
            "sessionID": root,
            "part": {
                "id": "tc_root_1",
                "callID": "tc_root_1",
                "type": "tool",
                "tool": "task",
                "sessionID": root,
                "state": {
                    "status": "running",
                    "input": {
                        "subagent_type": "explore",
                        "description": "find the bug",
                        "prompt": "search for the auth bug in src/",
                    },
                    "metadata": {"sessionId": child},
                },
            },
        },
        {
            "type": "subagent_spawn",
            "timestamp": 30,
            "sessionID": root,
            "tool_call_id": "tc_root_1",
            "child_session_id": child,
            "input": {
                "subagent_type": "explore",
                "description": "find the bug",
                "prompt": "search for the auth bug in src/",
            },
        },
        {
            "type": "session_created",
            "timestamp": 40,
            "sessionID": child,
            "parentID": root,
            "info": {
                "id": child,
                "parentID": root,
                "title": "find the bug (@explore subagent)",
                "agent": "explore",
                "modelID": "z-ai/glm-4.7",
                "providerID": "openrouter",
            },
        },
        {
            "type": "step_start",
            "timestamp": 50,
            "sessionID": child,
            "part": {"type": "step-start", "sessionID": child},
        },
        {
            "type": "tool_use",
            "timestamp": 60,
            "sessionID": child,
            "part": {
                "id": "tc_child_1",
                "callID": "tc_child_1",
                "type": "tool",
                "tool": "read",
                "sessionID": child,
                "state": {
                    "status": "completed",
                    "input": {"path": "/workspace/src/auth.py"},
                    "output": "# auth.py\n... line 42 has a tenant check missing ...",
                },
            },
        },
        {
            "type": "text",
            "timestamp": 70,
            "sessionID": child,
            "part": {
                "id": "txt_child_1",
                "type": "text",
                "sessionID": child,
                "text": "found the bug at line 42",
                "time": {"end": 70},
            },
        },
        {
            "type": "step_finish",
            "timestamp": 80,
            "sessionID": child,
            "part": {
                "type": "step-finish",
                "sessionID": child,
                "tokens": {"input": 200, "output": 50, "reasoning": 0},
                "cost": 0.001,
            },
        },
        {
            "type": "session_status",
            "timestamp": 90,
            "sessionID": child,
            "status": {"type": "idle"},
        },
        {
            "type": "tool_use",
            "timestamp": 100,
            "sessionID": root,
            "part": {
                "id": "tc_root_1",
                "callID": "tc_root_1",
                "type": "tool",
                "tool": "task",
                "sessionID": root,
                "state": {
                    "status": "completed",
                    "input": {
                        "subagent_type": "explore",
                        "description": "find the bug",
                        "prompt": "search for the auth bug in src/",
                    },
                    "output": "task_id: ses_sub_b\n\n<task_result>\nfound the bug at line 42\n</task_result>",
                    "metadata": {"sessionId": child},
                },
            },
        },
        {
            "type": "text",
            "timestamp": 110,
            "sessionID": root,
            "part": {
                "id": "txt_root_1",
                "type": "text",
                "sessionID": root,
                "text": "Subagent says line 42; here's the patch",
                "time": {"end": 110},
            },
        },
        {
            "type": "step_finish",
            "timestamp": 120,
            "sessionID": root,
            "part": {
                "type": "step-finish",
                "sessionID": root,
                "tokens": {"input": 1000, "output": 200, "reasoning": 0},
                "cost": 0.005,
            },
        },
        {
            "type": "session_status",
            "timestamp": 130,
            "sessionID": root,
            "status": {"type": "idle"},
        },
    ]
    _write_jsonl(path, rows)


# ── Tests ────────────────────────────────────────────────────────────────


class TestEmptyStream:
    def test_no_file(self, tmp_path: Path) -> None:
        # Calling on a missing path should yield a degenerate-but-valid trace
        # rather than crashing — useful when the agent never started.
        trace = collect_opencode_agent_trace(
            tmp_path / "missing.jsonl",
            model_name="openrouter/z-ai/glm-4.7",
        )
        assert trace["schema_version"] == "AgentTrace-v0.1"
        assert trace["root_run_id"]
        assert len(trace["runs"]) == 1
        assert trace["runs"][0]["role"] == "root"
        assert trace["runs"][0]["events"] == []

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        trace = collect_opencode_agent_trace(path)
        assert trace["summary"]["total_events"] == 0


class TestSingleRoot:
    def test_root_only_session(self, tmp_path: Path) -> None:
        """No subagent — single root run with step + text + step_finish."""
        root = "ses_root_x"
        path = tmp_path / "single.jsonl"
        _write_jsonl(
            path,
            [
                {
                    "type": "session_created",
                    "timestamp": 0,
                    "sessionID": root,
                    "parentID": None,
                    "info": {"id": root, "agent": "build", "modelID": "m"},
                },
                {
                    "type": "step_start",
                    "timestamp": 10,
                    "sessionID": root,
                    "part": {"type": "step-start"},
                },
                {
                    "type": "text",
                    "timestamp": 20,
                    "sessionID": root,
                    "part": {"id": "t1", "type": "text", "text": "hello", "time": {"end": 20}},
                },
                {
                    "type": "step_finish",
                    "timestamp": 30,
                    "sessionID": root,
                    "part": {"tokens": {"input": 10, "output": 5}, "cost": 0.0001},
                },
                {
                    "type": "session_status",
                    "timestamp": 40,
                    "sessionID": root,
                    "status": {"type": "idle"},
                },
            ],
        )
        trace = collect_opencode_agent_trace(path)
        assert len(trace["runs"]) == 1
        run = trace["runs"][0]
        assert run["role"] == "root"
        assert run["status"] == "completed"
        # Step / message / step_end / status events present in order.
        kinds = [e["kind"] for e in run["events"]]
        assert kinds == ["step_begin", "message", "step_end", "status"]
        # Token totals.
        assert run["metrics"]["prompt_tokens"] == 10
        assert run["metrics"]["completion_tokens"] == 5
        assert run["metrics"]["cost_usd"] > 0


class TestSubagentSpawn:
    def test_subagent_run_built_and_linked(self, tmp_path: Path) -> None:
        path = tmp_path / "subagent.jsonl"
        _make_subagent_jsonl(path)
        trace = collect_opencode_agent_trace(
            path, model_name="openrouter/z-ai/glm-4.7"
        )

        # Two runs — one root + one subagent.
        assert len(trace["runs"]) == 2
        roles = {r["role"] for r in trace["runs"]}
        assert roles == {"root", "subagent"}

        root_run = next(r for r in trace["runs"] if r["role"] == "root")
        sub_run = next(r for r in trace["runs"] if r["role"] == "subagent")

        # Root is first in the list (ordering aids analysis tools).
        assert trace["runs"][0]["role"] == "root"
        assert trace["root_run_id"] == root_run["run_id"]

        # Tree edges.
        assert sub_run["parent_run_id"] == root_run["run_id"]
        assert root_run["parent_run_id"] is None
        assert sub_run["session_mode"] == "child_session"
        assert root_run["session_mode"] == "same_session"

    def test_subagent_spawn_event_carries_child_run_id(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "subagent.jsonl"
        _make_subagent_jsonl(path)
        trace = collect_opencode_agent_trace(path)
        root_run = next(r for r in trace["runs"] if r["role"] == "root")
        sub_run = next(r for r in trace["runs"] if r["role"] == "subagent")
        spawn = next(
            e for e in root_run["events"] if e["kind"] == "subagent_spawn"
        )
        assert spawn["payload"]["child_run_id"] == sub_run["run_id"]
        assert spawn["payload"]["tool_call_id"] == "tc_root_1"
        # source_event_id on the child run points back at the spawn event.
        assert sub_run["source_event_id"] == spawn["event_id"]

    def test_tool_use_split_into_call_and_result(self, tmp_path: Path) -> None:
        path = tmp_path / "subagent.jsonl"
        _make_subagent_jsonl(path)
        trace = collect_opencode_agent_trace(path)
        sub_run = next(r for r in trace["runs"] if r["role"] == "subagent")
        kinds = [e["kind"] for e in sub_run["events"]]
        # The child's `read` tool_use should split into call + result.
        idx = kinds.index("tool_call")
        assert kinds[idx + 1] == "tool_result"
        call = sub_run["events"][idx]
        result = sub_run["events"][idx + 1]
        assert call["payload"]["tool_name"] == "read"
        assert result["payload"]["tool_call_id"] == call["payload"]["tool_call_id"]
        assert result["parent_event_id"] == call["event_id"]

    def test_metrics_aggregated_per_run(self, tmp_path: Path) -> None:
        path = tmp_path / "subagent.jsonl"
        _make_subagent_jsonl(path)
        trace = collect_opencode_agent_trace(path)
        root_run = next(r for r in trace["runs"] if r["role"] == "root")
        sub_run = next(r for r in trace["runs"] if r["role"] == "subagent")
        # Per-run tokens reflect each session in isolation.
        assert root_run["metrics"]["prompt_tokens"] == 1000
        assert root_run["metrics"]["completion_tokens"] == 200
        assert sub_run["metrics"]["prompt_tokens"] == 200
        assert sub_run["metrics"]["completion_tokens"] == 50
        # Summary aggregates across runs.
        summ = trace["summary"]
        assert summ["total_runs"] == 2
        assert summ["subagent_count_total"] == 1
        assert summ["total_prompt_tokens"] == 1200
        assert summ["total_completion_tokens"] == 250
        assert abs((summ["total_cost_usd"] or 0) - 0.006) < 1e-9

    def test_artifacts_reference_source_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "subagent.jsonl"
        _make_subagent_jsonl(path)
        trace = collect_opencode_agent_trace(path)
        kinds = [a["kind"] for a in trace["artifacts"]]
        assert "native_jsonl" in kinds
        for art in trace["artifacts"]:
            if art["kind"] == "native_jsonl":
                assert art["path"] == str(path)
