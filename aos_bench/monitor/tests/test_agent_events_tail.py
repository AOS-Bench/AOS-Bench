from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aos_bench.monitor.discover import TrialDiscoverer
from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.agent_events import (
    AgentEventsTail,
    IncrementalAgentEventsParser,
)


def _events(who: str = "solo") -> list[dict]:
    """A minimal solo events.jsonl stream, shaped like the real
    Coordinator log (solo_start / assistant_text / tool_use / tool_result /
    result / solo_end)."""
    return [
        {"ts": 1.0, "kind": "solo_start", "model": "claude-haiku-4-5", "pool": "http"},
        {"ts": 2.0, "kind": "assistant_text", "who": who, "text": "Claiming first task."},
        {"ts": 3.0, "kind": "tool_use", "who": who, "name": "mcp__aos_pool__claim", "input": "{}"},
        {"ts": 4.0, "kind": "tool_result", "who": who, "tool_use_id": "tu_1"},
        {"ts": 5.0, "kind": "assistant_text", "who": who, "text": "Running tests."},
        {"ts": 6.0, "kind": "tool_use", "who": who, "name": "Bash", "input": {"command": "pytest"}},
        {
            "ts": 7.0,
            "kind": "result",
            "who": who,
            "num_turns": 12,
            "duration_ms": 4200,
            "total_cost_usd": 0.03,
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 50},
        },
        {"ts": 8.0, "kind": "solo_end", "wall_time_sec": 7.0},
    ]


def test_parser_maps_solo_events() -> None:
    p = IncrementalAgentEventsParser(run_id="run_root_001", synthesize_subagent_spawns=True)
    out = p.feed(_events())
    kinds = [e["kind"] for e in out]
    # assistant_text opens a step then emits the message; tool_use→tool_call;
    # tool_result→tool_result; result→status; solo_start/end→status.
    assert kinds == [
        "status",        # solo_start
        "step_begin",    # assistant_text #1 opens a turn
        "message",
        "tool_call",
        "tool_result",
        "step_begin",    # assistant_text #2
        "message",
        "tool_call",
        "status",        # result
        "status",        # solo_end
    ]
    # two assistant turns → two steps (n_llm_calls proxy)
    assert p.step_n == 2
    # tool_call payload carries function.name the frontend reads
    tc = next(e for e in out if e["kind"] == "tool_call" and e["payload"]["name"] == "Bash")
    assert tc["payload"]["function"]["name"] == "Bash"
    assert tc["payload"]["function"]["arguments"] == {"command": "pytest"}
    # tokens from the result event: 100 + 900 cache_read = 1000 prompt, 50 completion
    assert p.prompt_tokens == 1000
    assert p.completion_tokens == 50


def test_tail_routes_solo_to_root(tmp_path: Path) -> None:
    trial_dir = tmp_path / "jobs" / "solo-x" / "mixed-5-1__abcd"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "agent" / "events.jsonl").write_text(
        "\n".join(json.dumps(x) for x in _events()) + "\n"
    )

    state = MonitorState(jobs_dir=str(tmp_path))
    detector = ErrorDetector(state)
    tail = AgentEventsTail(
        trial_id="mixed-5-1__abcd",
        trial_dir=trial_dir,
        state=state,
        error_detector=detector,
        interval_s=0.2,
    )

    async def run() -> None:
        await tail.start()
        await asyncio.sleep(0.8)
        await tail.stop()

    asyncio.run(run())

    trial = state.trials["mixed-5-1__abcd"]
    assert trial.agent == "claude-agent-sdk"
    root = trial.runs["run_root_001"]
    assert root.role == "root"
    assert root.n_tool_calls == 2
    assert root.n_llm_calls == 2  # two assistant turns
    assert root.completion_tokens == 50


def test_tail_routes_team_workers_to_subagents(tmp_path: Path) -> None:
    """A team run interleaves lead + workerN under one events.jsonl; each
    distinct `who` becomes its own run, lead → root."""
    trial_dir = tmp_path / "jobs" / "team-x" / "mixed-5-1__teamd"
    (trial_dir / "agent").mkdir(parents=True)
    recs = [
        {"ts": 1.0, "kind": "solo_start", "model": "m"},
        {"ts": 2.0, "kind": "assistant_text", "who": "lead", "text": "spawning a worker"},
        {"ts": 3.0, "kind": "tool_use", "who": "lead", "name": "spawn_worker", "input": "{}"},
        {"ts": 4.0, "kind": "assistant_text", "who": "worker1", "text": "on it"},
        {"ts": 5.0, "kind": "tool_use", "who": "worker1", "name": "Bash", "input": {}},
    ]
    (trial_dir / "agent" / "events.jsonl").write_text(
        "\n".join(json.dumps(x) for x in recs) + "\n"
    )

    state = MonitorState(jobs_dir=str(tmp_path))
    tail = AgentEventsTail(
        trial_id="mixed-5-1__teamd",
        trial_dir=trial_dir,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=0.2,
    )

    async def run() -> None:
        await tail.start()
        await asyncio.sleep(0.8)
        await tail.stop()

    asyncio.run(run())

    trial = state.trials["mixed-5-1__teamd"]
    assert "run_root_001" in trial.runs  # lead
    assert "run_sub_worker1" in trial.runs
    assert trial.runs["run_sub_worker1"].parent_run_id == "run_root_001"
    # spawn_worker tool_use synthesized a spawn marker on the lead lane
    spawn_kinds = [e.get("kind") for e in trial.runs["run_root_001"].structural_events]
    assert "subagent_spawn" in spawn_kinds


def test_sdk_trial_discovered_via_discoverer(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    # batch wrapper → leaf trial dir with agent/events.jsonl
    leaf = jobs_dir / "claude-agent-team__mixed-5-1__att1" / "mixed-5-1__abcd"
    (leaf / "agent").mkdir(parents=True)
    (leaf / "agent" / "events.jsonl").write_text(
        "\n".join(json.dumps(x) for x in _events()) + "\n"
    )

    state = MonitorState(jobs_dir=str(jobs_dir))
    disc = TrialDiscoverer(
        jobs_dir=jobs_dir,
        state=state,
        error_detector=ErrorDetector(state),
        scan_interval_s=0.2,
        tail_interval_s=0.2,
    )

    async def run() -> None:
        await disc.start()
        await asyncio.sleep(1.2)
        await disc.stop()

    asyncio.run(run())

    assert "mixed-5-1__abcd" in state.trials
    trial = state.trials["mixed-5-1__abcd"]
    assert trial.agent == "claude-agent-sdk"
    assert trial.runs["run_root_001"].n_tool_calls == 2
