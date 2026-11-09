"""Tests for the trial-finalization code paths added when phase 1 of
exp_0601 surfaced a wave of trials stuck at status=running on disk
despite having terminal result.json.

Three layers under test:
  1. ``_iter_trial_dirs`` (discover.py) — walks any depth, finds leaf
     trial dirs (those that directly contain agent/ or agent/kimi-share).
  2. ``_maybe_detect_crashed`` (tail/kimi.py) — flips a discovered
     trial to crashed when exception.txt appears at trial_dir/
     (depth 0; the old version only globbed at depth 1-2 and missed
     harbor's standard exception.txt location).
  3. ``_maybe_finalize_from_result_json`` (tail/kimi.py) — terminal
     state derived from harbor's per-trial result.json. Covers the
     timeout-but-graded path (exception_info present AND
     verifier_result.rewards present → completed with reward) the
     reward.txt path misses.

Layers 2 and 3 are agent-independent — they read harbor's own artifacts
(``exception.txt`` / ``result.json`` / ``verifier/reward.txt``), not any
agent's transcript — so every tail that owns a trial's terminal state has
to implement them identically. They are therefore parametrized over
``_FINALIZING_TAILS`` rather than re-tested per arm: a new tail added to
that tuple inherits the whole contract, and an arm that drifts (e.g. drops
the reward walk-up) fails here instead of silently leaving its trials at
``running`` forever.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aos_bench.monitor.discover import (
    TrialDiscoverer,
    _iter_trial_dirs,
    _looks_like_trial,
)
from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.kimi import KimiSessionTail
from aos_bench.monitor.tail.kimi_code import KimiCodeSessionTail

# Tails that own trial finalization. All share the keyword-only
# ``(trial_id, trial_dir, state, error_detector, interval_s)`` ctor
# (``discover.py`` dispatches them interchangeably) plus the three
# ``_maybe_*`` methods below. ``ClaudeSessionTail`` / ``CodexSessionTail`` /
# ``AgentEventsTail`` are covered by their own suites.
_FINALIZING_TAILS = (KimiSessionTail, KimiCodeSessionTail)
_finalizing_tails = pytest.mark.parametrize(
    "tail_cls", _FINALIZING_TAILS, ids=lambda c: c.__name__
)


# ── _iter_trial_dirs / _looks_like_trial ─────────────────────────


def _materialize(trial_dir: Path) -> None:
    """Make a dir look like a leaf trial: agent/kimi-share/sessions/<h>/<u>/wire.jsonl."""
    sess = (
        trial_dir
        / "agent"
        / "kimi-share"
        / "sessions"
        / "0123456789abcdef0123456789abcdef"
        / "11111111-2222-3333-4444-555555555555"
    )
    sess.mkdir(parents=True)
    (sess / "wire.jsonl").write_text(
        json.dumps({"type": "metadata", "protocol_version": "0.5"}) + "\n"
    )


def test_iter_trial_dirs_finds_depth1(tmp_path: Path) -> None:
    """Smoke layout: jobs_dir/<trial>/agent/kimi-share/…"""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    _materialize(jobs / "trial_a")
    _materialize(jobs / "trial_b")
    out = _iter_trial_dirs(jobs)
    assert {d.name for d in out} == {"trial_a", "trial_b"}


def test_iter_trial_dirs_finds_deep_leafs(tmp_path: Path) -> None:
    """exp_0601 layout: jobs_dir/<phase>/<source>/<job>/<trial>/agent/...

    The old _looks_like_trial used liberal globs (`*/agent/...`,
    `*/*/agent/...`) and would classify the *phase* dir as a single
    trial, hiding the 100+ real trials below. The recursive walker
    must yield the actual leaf dirs.
    """
    jobs = tmp_path / "exp_root"
    leafs = [
        jobs / "01-individual" / "tb2" / "tb2-rerun" / "vulnerable-secret__abc",
        jobs / "01-individual" / "skillsbench" / "skillsbench-rerun" / "dapt-intrusion__def",
        jobs / "02-burst" / "light" / "light-burst" / "mixed-5-3__xyz",
    ]
    for d in leafs:
        _materialize(d)
    out = _iter_trial_dirs(jobs)
    assert {d.name for d in out} == {leaf.name for leaf in leafs}


def test_iter_trial_dirs_stops_at_trial_boundary(tmp_path: Path) -> None:
    """A trial may have nested agent/ structure (kimi-share/sessions/...)
    but the walker should only register the trial root, not descend
    further once it sees agent/kimi-share/.
    """
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    _materialize(jobs / "trial_one")
    out = _iter_trial_dirs(jobs)
    assert [d.name for d in out] == ["trial_one"]


def test_looks_like_trial_rejects_intermediate_dirs(tmp_path: Path) -> None:
    """A dir with kimi-share buried *under* a subdir is NOT a trial
    itself — that's a job dir containing trials. This used to return
    True under the old glob logic, swallowing the real children.
    """
    jobs = tmp_path / "jobs"
    _materialize(jobs / "job_a" / "trial_x")
    assert not _looks_like_trial(jobs / "job_a")
    assert _looks_like_trial(jobs / "job_a" / "trial_x")


# ── _maybe_detect_crashed (exception.txt at depth 0) ──────────────────


def _bare_trial(tmp_path: Path) -> Path:
    """A trial dir that harbor opened (has agent/) but where kimi-cli
    never wrote wire.jsonl (e.g. uv install kimi-cli failed). This is
    what `_looks_like_trial` falls back on for "weak" trial signals.
    """
    trial = tmp_path / "jobs" / "stuck_in_setup"
    (trial / "agent").mkdir(parents=True)
    return trial


@_finalizing_tails
def test_detect_crashed_picks_up_exception_at_depth0(
    tmp_path: Path, tail_cls: type
) -> None:
    """Harbor writes <trial_dir>/exception.txt directly. Old globs only
    looked at depth 1-2 and missed this — trial stayed at status=discovered
    forever even after harbor wrote result.json.
    """
    trial = _bare_trial(tmp_path)
    (trial / "exception.txt").write_text("EnvironmentStartTimeoutError: 600s\n")
    state = MonitorState(jobs_dir=str(tmp_path / "jobs"))
    state.ensure_trial("stuck_in_setup", str(trial))
    tail = tail_cls(
        trial_id="stuck_in_setup",
        trial_dir=trial,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=10,
    )
    tail._maybe_detect_crashed()
    assert state.trials["stuck_in_setup"].status == "crashed"


@_finalizing_tails
def test_detect_crashed_skips_if_already_running(
    tmp_path: Path, tail_cls: type
) -> None:
    """Once a trial advances past `discovered`, the crashed detector
    no-ops — otherwise a normal AgentTimeoutError trial (which also
    writes exception.txt while the wire is healthy) would lose its
    real terminal state.
    """
    trial = _bare_trial(tmp_path)
    (trial / "exception.txt").write_text("AgentTimeoutError: 5400s\n")
    state = MonitorState(jobs_dir=str(tmp_path / "jobs"))
    state.ensure_trial("running_trial", str(trial))
    state.set_trial_status("running_trial", "running")  # past discovered
    tail = tail_cls(
        trial_id="running_trial",
        trial_dir=trial,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=10,
    )
    tail._maybe_detect_crashed()
    assert state.trials["running_trial"].status == "running"


# ── _maybe_finalize_from_result_json ──────────────────────────────────


@_finalizing_tails
def test_finalize_extracts_reward_from_verifier_result(
    tmp_path: Path, tail_cls: type
) -> None:
    """result.json with verifier_result.rewards = {"reward": 1.0} →
    status=completed, reward=1.0. Even if exception_info is present
    (the agent timed out but verifier still graded — the canonical
    "AgentTimeoutError but verifier ran" path).
    """
    trial = tmp_path / "jobs" / "trial1"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "verifier_result": {"rewards": {"reward": 1.0}},
        "exception_info": {
            "exception_type": "AgentTimeoutError",
            "exception_message": "Agent execution timed out after 5400.0 seconds",
        },
    }))
    state = MonitorState(jobs_dir=str(tmp_path / "jobs"))
    state.ensure_trial("trial1", str(trial))
    tail = tail_cls(
        trial_id="trial1",
        trial_dir=trial,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=10,
    )
    tail._maybe_finalize_from_result_json()
    t = state.trials["trial1"]
    assert t.status == "completed"
    assert t.reward == 1.0


@_finalizing_tails
def test_finalize_marks_failed_when_no_verifier_result(
    tmp_path: Path, tail_cls: type
) -> None:
    """exception_info present + verifier_result is None → trial never
    reached verifier (e.g. AddTestsDirError mid-setup) → status=failed.
    """
    trial = tmp_path / "jobs" / "trial2"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "verifier_result": None,
        "exception_info": {
            "exception_type": "EnvironmentStartTimeoutError",
            "exception_message": "Environment start timed out after 600.0 seconds",
        },
    }))
    state = MonitorState(jobs_dir=str(tmp_path / "jobs"))
    state.ensure_trial("trial2", str(trial))
    tail = tail_cls(
        trial_id="trial2",
        trial_dir=trial,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=10,
    )
    tail._maybe_finalize_from_result_json()
    assert state.trials["trial2"].status == "failed"


@_finalizing_tails
def test_finalize_is_idempotent(tmp_path: Path, tail_cls: type) -> None:
    """Once a trial is in a terminal state, finalize is a no-op (we
    don't keep re-reading result.json on every tick after the trial
    is done)."""
    trial = tmp_path / "jobs" / "trial3"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "verifier_result": {"rewards": {"reward": 0.0}},
        "exception_info": None,
    }))
    state = MonitorState(jobs_dir=str(tmp_path / "jobs"))
    state.ensure_trial("trial3", str(trial))
    state.set_trial_status("trial3", "completed")
    state.set_trial_reward("trial3", 0.5)  # poison value
    tail = tail_cls(
        trial_id="trial3",
        trial_dir=trial,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=10,
    )
    tail._maybe_finalize_from_result_json()
    assert state.trials["trial3"].reward == 0.5  # unchanged
    assert state.trials["trial3"].status == "completed"


@_finalizing_tails
def test_finalize_finds_result_json_at_depth0(tmp_path: Path, tail_cls: type) -> None:
    """result.json sits directly under trial_dir in the exp_0601 layout.
    The finalizer must accept depth 0 (not just nested batch layouts).
    """
    trial = tmp_path / "jobs" / "trial4"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "verifier_result": {"rewards": {"reward": 1.0}},
        "exception_info": None,
    }))
    state = MonitorState(jobs_dir=str(tmp_path / "jobs"))
    state.ensure_trial("trial4", str(trial))
    tail = tail_cls(
        trial_id="trial4",
        trial_dir=trial,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=10,
    )
    tail._maybe_finalize_from_result_json()
    assert state.trials["trial4"].status == "completed"
    assert state.trials["trial4"].reward == 1.0


# ── _maybe_load_reward (reward.txt parse) ─────────────────────────────


@_finalizing_tails
def test_load_reward_accepts_plain_text(tmp_path: Path, tail_cls: type) -> None:
    """Harbor's stream verifier writes verifier/reward.txt as a single
    float, not JSON. The old loader only accepted JSON and silently
    skipped everyone."""
    trial = tmp_path / "jobs" / "trial5"
    sess = (
        trial / "agent" / "kimi-share" / "sessions"
        / "0123456789abcdef0123456789abcdef"
        / "ssn"
    )
    sess.mkdir(parents=True)
    verif = trial / "verifier"
    verif.mkdir()
    (verif / "reward.txt").write_text("1\n")
    state = MonitorState(jobs_dir=str(tmp_path / "jobs"))
    state.ensure_trial("trial5", str(trial))
    tail = tail_cls(
        trial_id="trial5",
        trial_dir=trial,
        state=state,
        error_detector=ErrorDetector(state),
        interval_s=10,
    )
    tail._session_dir = sess
    tail._maybe_load_reward()
    assert state.trials["trial5"].status == "completed"
    assert state.trials["trial5"].reward == 1.0
