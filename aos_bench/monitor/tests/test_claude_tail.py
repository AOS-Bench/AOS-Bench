from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aos_bench.monitor.discover import TrialDiscoverer, _classify_trial_agent
from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.claude import ClaudeSessionTail


def _materialize_cc_trial(jobs_dir: Path, trial_id: str) -> Path:
    """Create a minimal Claude Code session tree under a fake trial dir.

    Mirrors harbor's layout (CLAUDE_CONFIG_DIR = <trial>/agent/sessions) closely
    enough that ``ClaudeSessionTail`` resolves the session dir and tails it. The
    transcript contains a root thread + a sidechain subagent turn so we exercise
    the agentId routing path.
    """
    trial_dir = jobs_dir / trial_id
    sess_dir = (
        trial_dir
        / trial_id
        / "swe-task-10-7__abcd"
        / "agent"
        / "sessions"
        / "projects"
        / "-app"
        / "11111111-2222-3333-4444-555555555555"
    )
    sess_dir.mkdir(parents=True)
    lines = [
        # root user prompt
        {
            "type": "user",
            "timestamp": "2026-05-23T10:00:00.000Z",
            "sessionId": "sess-1",
            "message": {"role": "user", "content": "solve the task"},
        },
        # root assistant turn with a tool call + a Task spawn
        {
            "type": "assistant",
            "timestamp": "2026-05-23T10:00:01.000Z",
            "version": "2.1.88",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 120, "output_tokens": 40},
                "content": [
                    {"type": "text", "text": "I'll inspect then delegate review."},
                    {"type": "tool_use", "id": "tc_1", "name": "Bash", "input": {"command": "ls"}},
                    {
                        "type": "tool_use",
                        "id": "spawn_1",
                        "name": "Task",
                        "input": {"subagent_type": "reviewer", "description": "review diff"},
                    },
                ],
            },
        },
        # root tool result for the Bash call
        {
            "type": "user",
            "timestamp": "2026-05-23T10:00:02.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tc_1", "content": "a.py\n"}],
            },
        },
        # sidechain (subagent) assistant turn — distinct agentId, isSidechain
        {
            "type": "assistant",
            "timestamp": "2026-05-23T10:00:03.000Z",
            "isSidechain": True,
            "agentId": "sub-reviewer-1",
            "message": {
                "id": "ms1",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 50, "output_tokens": 10},
                "content": [{"type": "text", "text": "diff looks correct"}],
            },
        },
    ]
    transcript = sess_dir / "11111111-2222-3333-4444-555555555555.jsonl"
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return trial_dir


def test_classify_cc_vs_kimi(tmp_path: Path) -> None:
    # claude-code requires a REAL transcript .jsonl under projects/, not just
    # the dir — an empty projects/ (which the SDK harness also creates from its
    # CLAUDE_CONFIG_DIR) must NOT be misread as claude-code.
    cc_empty = tmp_path / "cc_empty"
    (cc_empty / "agent" / "sessions" / "projects").mkdir(parents=True)
    assert _classify_trial_agent(cc_empty) is None

    cc = tmp_path / "cc"
    proj = cc / "agent" / "sessions" / "projects" / "-app" / "sess"
    proj.mkdir(parents=True)
    (proj / "sess.jsonl").write_text('{"type":"assistant","message":{}}\n')
    assert _classify_trial_agent(cc) == "claude-code"

    # SDK harness — events.jsonl is the signal (and wins even if an empty
    # sessions/<role>/projects/ also exists).
    sdk = tmp_path / "sdk"
    (sdk / "agent" / "sessions" / "solo" / "projects").mkdir(parents=True)
    (sdk / "agent" / "events.jsonl").write_text('{"kind":"solo_start"}\n')
    assert _classify_trial_agent(sdk) == "claude-agent-sdk"

    kimi = tmp_path / "kimi"
    (kimi / "agent" / "kimi-share").mkdir(parents=True)
    assert _classify_trial_agent(kimi) == "kimi-cli"

    # Undetermined (agent dir exists but no trace materialised yet) → None,
    # so the discoverer defers building a tail until the trace appears.
    bare = tmp_path / "bare"
    (bare / "agent").mkdir(parents=True)
    assert _classify_trial_agent(bare) is None


def test_cc_tail_routes_root_and_subagent(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    trial_id = "swe-task-10-7__abcd"
    trial_dir = _materialize_cc_trial(jobs_dir, trial_id)

    state = MonitorState(jobs_dir=str(jobs_dir))
    detector = ErrorDetector(state)
    tail = ClaudeSessionTail(
        trial_id=trial_id,
        trial_dir=trial_dir,
        state=state,
        error_detector=detector,
        interval_s=0.2,
    )

    async def run() -> None:
        await tail.start()
        await asyncio.sleep(1.0)
        await tail.stop()

    asyncio.run(run())

    trial = state.trials[trial_id]
    assert trial.agent == "claude-code"
    # Root run + one subagent run, routed by agentId/isSidechain.
    assert "run_root_001" in trial.runs
    assert "run_sub_sub-reviewer-1" in trial.runs

    root = trial.runs["run_root_001"]
    assert root.n_tool_calls == 2  # Bash + Task
    assert root.n_llm_calls == 1  # one assistant turn
    assert root.prompt_tokens == 120
    assert root.completion_tokens == 40

    sub = trial.runs["run_sub_sub-reviewer-1"]
    assert sub.role == "subagent"
    assert sub.parent_run_id == "run_root_001"
    assert sub.n_llm_calls == 1
    assert sub.completion_tokens == 10

    # The Task tool_use produced a spawn marker on the root lane skeleton.
    spawn_kinds = [e.get("kind") for e in root.structural_events]
    assert "subagent_spawn" in spawn_kinds


def test_cc_trial_discovered_via_discoverer(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    # Outer batch wrapper — the walker must dig through to the leaf trial dir.
    outer_id = "claude-code__swe-task-10-7__individual__att1"
    leaf_trial_id = "swe-task-10-7__abcd"
    _materialize_cc_trial(jobs_dir, outer_id)

    state = MonitorState(jobs_dir=str(jobs_dir))
    detector = ErrorDetector(state)
    disc = TrialDiscoverer(
        jobs_dir=jobs_dir,
        state=state,
        error_detector=detector,
        scan_interval_s=0.2,
        tail_interval_s=0.2,
    )

    async def run() -> None:
        await disc.start()
        await asyncio.sleep(1.5)
        await disc.stop()

    asyncio.run(run())

    assert leaf_trial_id in state.trials
    trial = state.trials[leaf_trial_id]
    assert trial.agent == "claude-code"
    assert "run_root_001" in trial.runs
    assert trial.runs["run_root_001"].n_tool_calls == 2
