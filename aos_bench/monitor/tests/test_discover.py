from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aos_bench.monitor.discover import TrialDiscoverer, _trial_identity
from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.kimi_code import KimiCodeSessionTail


def test_cloud_discovery_does_not_block_event_loop(
    tmp_path: Path, monkeypatch
) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    def slow_discovery(*_args) -> list:
        time.sleep(0.15)
        return []

    monkeypatch.setattr(
        "aos_bench.monitor.discover._discover_trial_entries", slow_discovery
    )
    state = MonitorState(jobs_dir=str(jobs_dir))
    disc = TrialDiscoverer(
        jobs_dir=jobs_dir,
        state=state,
        error_detector=ErrorDetector(state),
    )

    async def run() -> None:
        started = asyncio.get_running_loop().time()
        scan = asyncio.create_task(disc._scan_once())
        await asyncio.sleep(0.02)
        # If slow_discovery ran on the event loop, this sleep would not wake
        # until after the full 150 ms blocking scan.
        assert asyncio.get_running_loop().time() - started < 0.10
        await scan

    asyncio.run(run())


def test_discoverer_starts_resource_sampling_before_native_trace(
    tmp_path: Path, monkeypatch
) -> None:
    """An empty Harbor agent mount exists during setup, before JSONL logs."""
    jobs_dir = tmp_path / "jobs"
    trial_dir = jobs_dir / "stream__setup"
    (trial_dir / "agent").mkdir(parents=True)
    started: list[str] = []

    async def fake_start(sampler) -> None:
        started.append(sampler.trial_id)

    monkeypatch.setattr(
        "aos_bench.monitor.discover.TrialContainerSampler.start", fake_start
    )
    state = MonitorState(jobs_dir=str(jobs_dir))
    disc = TrialDiscoverer(
        jobs_dir=jobs_dir,
        state=state,
        error_detector=ErrorDetector(state),
    )
    asyncio.run(disc._scan_once())

    assert started == ["stream__setup"]
    assert state.trials["stream__setup"].agent == "pending"
    assert "stream__setup" not in disc._tails


def test_trial_identity_reads_authoritative_harbor_config(tmp_path: Path) -> None:
    trial_dir = tmp_path / "claude-glm52--stream--stream__attempt7"
    trial_dir.mkdir()
    (trial_dir / "config.json").write_text(
        json.dumps(
            {
                "trial_name": "light-5-scan-007__attempt7",
                "agent": {"name": "claude-code", "model_name": "glm-5.2"},
                "task": {
                    "task_kind": "stream",
                    "path": "/runtime-streams/light/light-5-scan-007",
                },
            }
        )
    )
    assert _trial_identity(trial_dir, "claude-code") == {
        "harness_name": "claude-code",
        "model_name": "glm-5.2",
        "stream_name": "light-5-scan-007",
        "attempt_id": "attempt7",
    }


def _materialize_trial(jobs_dir: Path, trial_id: str) -> None:
    """Create a minimal kimi-share session tree under a fake trial dir.

    Mirrors the harbor layout enough that ``KimiSessionTail._find_share_dir``
    and ``_find_latest_session_dir`` resolve correctly.
    """
    trial_dir = jobs_dir / trial_id
    # Two layers of trial_id matching harbor's pattern, then trial subdir
    # then agent/kimi-share/sessions/<work_hash>/<session_id>/
    sess_dir = (
        trial_dir
        / trial_id
        / "skillsbench-data-10-44__abcd"
        / "agent"
        / "kimi-share"
        / "sessions"
        / "0123456789abcdef0123456789abcdef"
        / "11111111-2222-3333-4444-555555555555"
    )
    sess_dir.mkdir(parents=True)
    wire = sess_dir / "wire.jsonl"
    wire.write_text(
        "\n".join(
            [
                json.dumps({"type": "metadata", "protocol_version": "0.5"}),
                json.dumps({"timestamp": 1.0, "message": {"type": "TurnBegin", "payload": {}}}),
                json.dumps({"timestamp": 2.0, "message": {"type": "StepBegin", "payload": {"n": 1}}}),
                json.dumps(
                    {
                        "timestamp": 3.0,
                        "message": {
                            "type": "ToolCall",
                            "payload": {"id": "c1", "function": {"name": "Shell"}},
                        },
                    }
                ),
            ]
        )
        + "\n"
    )


def test_discoverer_registers_trial_and_tails_events(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    # Outer batch dirs that wrap the actual trial — the old "broken"
    # discoverer used to register one of these as the trial. The new
    # walker correctly digs through to the leaf dir that directly
    # contains agent/kimi-share/.
    outer_id = "kimi-cli__skillsbench-data-10-44__individual__court-form__att1"
    leaf_trial_id = "skillsbench-data-10-44__abcd"
    _materialize_trial(jobs_dir, outer_id)

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
        # Give it time for: 1 scan + 1 tail tick
        await asyncio.sleep(1.5)
        await disc.stop()

    asyncio.run(run())

    assert leaf_trial_id in state.trials
    trial = state.trials[leaf_trial_id]
    assert "run_root_001" in trial.runs
    run = trial.runs["run_root_001"]
    # Events ingested
    assert run.n_events >= 3
    assert run.n_tool_calls == 1
    assert run.n_llm_calls == 1  # StepBegin


# ── codex layout classification ────────────────────────────────────────


def _materialize_codex_trial(jobs_dir: Path, trial_id: str) -> Path:
    """Create the agent/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl layout
    that codex (via harbor's adapter) writes. Returns the rollout path."""
    trial_dir = jobs_dir / trial_id
    rollout_dir = trial_dir / "agent" / "sessions" / "2026" / "06" / "23"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout = rollout_dir / "rollout-2026-06-23T05-31-17-test.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-06-23T05:31:17.0Z",
                        "payload": {
                            "session_id": "test-sess",
                            "cli_version": "0.142.0",
                            "originator": "codex_exec",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-06-23T05:31:17.1Z",
                        "payload": {"type": "task_started", "turn_id": "t1"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn_context",
                        "timestamp": "2026-06-23T05:31:17.2Z",
                        "payload": {"model": "gpt-5.4-mini", "summary": "detailed"},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-23T05:31:17.3Z",
                        "payload": {
                            "type": "function_call",
                            "call_id": "c1",
                            "name": "shell",
                            "arguments": "{}",
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    return rollout


def test_classify_recognizes_codex_layout(tmp_path: Path) -> None:
    from aos_bench.monitor.discover import _classify_trial_agent

    trial_dir = tmp_path / "trial-x"
    _materialize_codex_trial(tmp_path, "trial-x")
    assert _classify_trial_agent(trial_dir) == "codex"


def test_classify_codex_does_not_match_claude_code_projects_layout(
    tmp_path: Path,
) -> None:
    """Claude Code's ``agent/sessions/projects/<p>/<sess>/*.jsonl`` must
    still classify as ``claude-code``, not codex — they share the
    ``sessions/`` prefix but only Claude Code has ``projects/`` in the
    middle."""
    from aos_bench.monitor.discover import _classify_trial_agent

    trial_dir = tmp_path / "cc-trial"
    cc_session = (
        trial_dir / "agent" / "sessions" / "projects" / "myproj" / "abc-sess"
    )
    cc_session.mkdir(parents=True)
    (cc_session / "session.jsonl").write_text("{}\n")
    assert _classify_trial_agent(trial_dir) == "claude-code"


def test_discoverer_registers_codex_trial_and_tails_events(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    trial_id = "hello-world__codex-test"
    _materialize_codex_trial(jobs_dir, trial_id)

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
        # 1 scan + a couple tail ticks to ingest the rollout.
        await asyncio.sleep(1.5)
        await disc.stop()

    asyncio.run(run())

    assert trial_id in state.trials
    trial = state.trials[trial_id]
    assert trial.agent == "codex"
    assert "run_root_001" in trial.runs
    run = trial.runs["run_root_001"]
    # task_started → turn_begin, turn_context → step_begin, function_call
    # → tool_call. session_meta is silently consumed.
    assert run.n_events >= 3
    assert run.n_tool_calls == 1
    assert run.n_llm_calls == 1  # synthesized step_begin off turn_context


# ── kimi-code layout classification ────────────────────────────────────


def _materialize_kimi_code_home(
    jobs_dir: Path, trial_id: str, *, with_session: bool = True
) -> Path:
    """Create ``agent/kimi-code-home`` (the arm's marker) under a trial dir.

    ``with_session=False`` leaves the home dir empty — the state harbor's
    adapter creates it in, *before* the CLI has started. Returns the trial dir.
    """
    trial_dir = jobs_dir / trial_id
    home = trial_dir / "agent" / "kimi-code-home"
    home.mkdir(parents=True, exist_ok=True)
    if with_session:
        agent_dir = (
            home
            / "sessions"
            / "wd_app_000000000000"
            / "session_11111111-2222-3333-4444-555555555555"
            / "agents"
            / "main"
        )
        agent_dir.mkdir(parents=True)
        (agent_dir / "wire.jsonl").write_text(
            json.dumps({"type": "metadata", "protocol_version": "1.5"}) + "\n"
        )
    return trial_dir


def test_classify_recognizes_kimi_code_layout(tmp_path: Path) -> None:
    from aos_bench.monitor.discover import _classify_trial_agent

    trial_dir = _materialize_kimi_code_home(tmp_path, "kc-trial")
    assert _classify_trial_agent(trial_dir) == "kimi-code"


def test_classify_kimi_code_home_without_sessions(tmp_path: Path) -> None:
    """The frozen marker is the *directory*, not a journal: a trial whose
    ``npm i -g @moonshot-ai/kimi-code`` failed has an empty ``kimi-code-home``
    and must still be typed kimi-code, so ``KimiCodeSessionTail`` gets built and
    reports the ``exception.txt`` instead of leaving the trial at
    ``discovered`` forever."""
    from aos_bench.monitor.discover import _classify_trial_agent

    trial_dir = _materialize_kimi_code_home(tmp_path, "kc-empty", with_session=False)
    assert _classify_trial_agent(trial_dir) == "kimi-code"


def test_classify_kimi_code_home_nested_under_batch_dirs(tmp_path: Path) -> None:
    """Harbor's intermediate nesting is not stable, so the marker is matched
    recursively — same reason ``_find_home_dir`` globs (``tail/kimi_code.py``)."""
    from aos_bench.monitor.discover import _classify_trial_agent

    trial_dir = tmp_path / "kc-nested"
    (trial_dir / "agent" / "mixed-5-1__abcd" / "kimi-code-home").mkdir(parents=True)
    assert _classify_trial_agent(trial_dir) == "kimi-code"


def test_classify_ignores_kimi_code_home_that_is_a_file(tmp_path: Path) -> None:
    """A *file* named ``kimi-code-home`` (a stray log or tarball) must not type
    the trial onto a tail that can never find a session dir."""
    from aos_bench.monitor.discover import _classify_trial_agent

    trial_dir = tmp_path / "kc-file"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "agent" / "kimi-code-home").write_text("not a dir\n")
    assert _classify_trial_agent(trial_dir) is None


def test_classify_bare_agent_dir_is_unclassified(tmp_path: Path) -> None:
    """No trace of any arm yet → ``None``, so the discoverer defers building a
    tail instead of locking the trial onto the wrong one."""
    from aos_bench.monitor.discover import _classify_trial_agent

    trial_dir = tmp_path / "bare"
    (trial_dir / "agent").mkdir(parents=True)
    assert _classify_trial_agent(trial_dir) is None


def test_classify_kimi_code_does_not_shadow_the_other_four(tmp_path: Path) -> None:
    """The kimi-code branch was inserted at step 4, ahead of kimi-cli. Re-assert
    every other marker still wins on its own layout — a regression here would
    silently re-type whole experiment dirs."""
    from aos_bench.monitor.discover import _classify_trial_agent

    # claude-agent-sdk: agent/events.jsonl (checked first of all).
    sdk = tmp_path / "sdk"
    (sdk / "agent").mkdir(parents=True)
    (sdk / "agent" / "events.jsonl").write_text("{}\n")
    assert _classify_trial_agent(sdk) == "claude-agent-sdk"

    # claude-code: a real transcript under projects/.
    cc = tmp_path / "cc"
    cc_sess = cc / "agent" / "sessions" / "projects" / "myproj" / "abc-sess"
    cc_sess.mkdir(parents=True)
    (cc_sess / "session.jsonl").write_text("{}\n")
    assert _classify_trial_agent(cc) == "claude-code"

    # codex: agent/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl.
    _materialize_codex_trial(tmp_path, "cdx")
    assert _classify_trial_agent(tmp_path / "cdx") == "codex"

    # kimi-cli: agent/kimi-share/.
    kcli = tmp_path / "kcli"
    (kcli / "agent" / "kimi-share" / "sessions").mkdir(parents=True)
    assert _classify_trial_agent(kcli) == "kimi-cli"

    # kimi-code: agent/kimi-code-home/.
    assert _classify_trial_agent(_materialize_kimi_code_home(tmp_path, "kc")) == (
        "kimi-code"
    )


def test_discoverer_registers_kimi_code_trial(tmp_path: Path) -> None:
    """End-to-end: the walker finds the trial, classification picks kimi-code
    and the dispatched tail is ``KimiCodeSessionTail`` (which stamps
    ``trial.agent``)."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    trial_id = "mixed-5-1__kimi-code-test"
    _materialize_kimi_code_home(jobs_dir, trial_id)

    state = MonitorState(jobs_dir=str(jobs_dir))
    detector = ErrorDetector(state)
    disc = TrialDiscoverer(
        jobs_dir=jobs_dir,
        state=state,
        error_detector=detector,
        scan_interval_s=0.2,
        tail_interval_s=0.2,
    )

    # ``stop()`` clears ``_tails``, so snapshot the dispatched class first.
    dispatched: list[type] = []

    async def run() -> None:
        await disc.start()
        await asyncio.sleep(1.0)
        dispatched.append(type(disc._tails[trial_id]))
        await disc.stop()

    asyncio.run(run())

    assert dispatched == [KimiCodeSessionTail]
    assert trial_id in state.trials
    assert state.trials[trial_id].agent == "kimi-code"
