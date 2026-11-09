from __future__ import annotations

import asyncio

from aos_bench.monitor.state import (
    ErrorRecord,
    MonitorState,
    ProcessInfo,
    ProcessSnapshot,
    ResourceSample,
)


def test_ensure_trial_publishes_to_subscriber() -> None:
    async def run() -> None:
        state = MonitorState(jobs_dir="/tmp/x")
        q = state.subscribe()
        state.ensure_trial("kimi-cli__abc__individual__att1", "/tmp/x/kimi-cli__abc__individual__att1")
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        assert event["type"] == "trial_discovered"
        assert event["trial_id"] == "kimi-cli__abc__individual__att1"
    asyncio.run(run())


def test_trial_display_name_uses_full_harness_model_and_stream() -> None:
    state = MonitorState(jobs_dir="/tmp/x")
    trial = state.ensure_trial(
        "claude-glm52--light-5-scan-007--light-5-scan-007__abc",
        "/tmp/x/trial",
        agent="claude-code",
        harness_name="claude-code",
        model_name="glm-5.2",
        stream_name="light-5-scan-007",
        attempt_id="abc",
    )
    summary = trial.to_summary_dict()
    assert summary["display_name"] == "claude-code-glm-5.2-light-5-scan-007"
    assert summary["setting_name"] == "claude-code-glm-5.2"
    assert summary["attempt_id"] == "abc"


def test_trial_display_name_normalizes_provider_separator() -> None:
    state = MonitorState(jobs_dir="/tmp/x")
    trial = state.ensure_trial(
        "stream__abc",
        "/tmp/x/trial",
        agent="codex",
        harness_name="codex",
        model_name="openai/gpt-5.5",
        stream_name="heavy-20-scan-001",
    )
    assert trial.display_name == "codex-openai-gpt-5.5-heavy-20-scan-001"


def test_push_events_counts_tools_and_llm() -> None:
    state = MonitorState(jobs_dir="/tmp/x")
    state.ensure_trial("t1", "/tmp/x/t1")
    state.ensure_run("t1", "run_root_001", role="root", agent_name="orchestrator")

    events = [
        {"kind": "step_begin", "payload": {"n": 1}, "timestamp": 1.0, "metrics": {}},
        {"kind": "tool_call", "payload": {"id": "c1"}, "timestamp": 1.5, "metrics": {}},
        {"kind": "tool_result", "payload": {"id": "c1"}, "timestamp": 1.6, "metrics": {}},
        {"kind": "compaction_begin", "payload": {}, "timestamp": 2.0, "metrics": {}},
        {"kind": "status", "payload": {}, "timestamp": 2.1, "metrics": {"prompt_tokens": 100, "completion_tokens": 30}},
    ]
    state.push_events("t1", "run_root_001", events)

    trial = state.trials["t1"]
    run = trial.runs["run_root_001"]
    assert run.n_events == 5
    assert run.n_tool_calls == 1
    assert run.n_llm_calls == 1  # step_begin proxies an LLM call
    assert run.n_compactions == 1
    assert run.prompt_tokens == 100
    assert run.completion_tokens == 30
    assert trial.last_event_at == 2.1


def test_add_error_increments_trial_count() -> None:
    state = MonitorState(jobs_dir="/tmp/x")
    state.ensure_trial("t1", "/tmp/x/t1")
    state.add_error(ErrorRecord(trial_id="t1", run_id="run_root_001", ts=1.0, rule_id="x", snippet="boom"))
    assert state.trials["t1"].errors_count == 1
    assert len(state.errors_snapshot()) == 1


def test_resource_sample_roundtrip() -> None:
    state = MonitorState(jobs_dir="/tmp/x")
    state.add_resource_sample(ResourceSample(ts=1.0, cpu_percent=12.5, mem_percent=50.0, mem_used_mb=2048))
    latest = state.latest_resource()
    assert latest is not None
    assert latest.cpu_percent == 12.5


def test_process_snapshot_roundtrip() -> None:
    state = MonitorState(jobs_dir="/tmp/x")
    state.add_trial_process_snapshot(
        "t1",
        ProcessSnapshot(
            ts=1.0,
            container_name="t1-main-1",
            attribution_mode="process_only",
            processes=[
                ProcessInfo(
                    pid=10,
                    ppid=1,
                    cpu_percent=5.0,
                    mem_rss_mb=32.0,
                    elapsed_s=10,
                    command="python",
                    args="python worker.py",
                )
            ],
        ),
    )
    samples = state.trial_process_snapshots("t1")
    assert samples[0]["processes"][0]["pid"] == 10


def test_subscriber_drops_when_full() -> None:
    """Slow subscriber: queue fills, further publishes do not block."""
    state = MonitorState(jobs_dir="/tmp/x")
    q = state.subscribe(maxsize=2)
    for i in range(10):
        state.ensure_trial(f"t{i}", f"/tmp/x/t{i}")
    # Only 2 messages were buffered; the rest were dropped (no exception).
    assert q.qsize() == 2
