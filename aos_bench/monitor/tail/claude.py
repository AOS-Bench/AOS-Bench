"""Live tail of one Claude Code session directory.

A harbor Claude Code trial mounts ``$CLAUDE_CONFIG_DIR`` at
``<trial_dir>/agent/sessions`` (see
``harbor/src/harbor/agents/installed/claude_code.py``: ``CLAUDE_CONFIG_DIR =
agent_dir / "sessions"``). Claude Code writes its transcript under
``sessions/projects/<project-slug>/<session-id>/*.jsonl``.

Unlike kimi-cli — which writes a separate ``subagents/<id>/wire.jsonl`` per
child — Claude Code interleaves subagent ("sidechain") turns into the *same*
transcript, tagged with ``isSidechain: true`` and a distinct ``agentId``. So
this tail keeps **one** :class:`IncrementalClaudeParser` per ``agentId`` and
routes each record to the right run, synthesizing the child run + spawn marker
the first time it sees a new sidechain agent. Some Claude Code builds also drop
a separate ``subagents/<id>/`` session dir; we tail those too and route by their
own ``agentId`` (deduped against the inline path).

Tail is poll-based (byte-offset re-read every ``interval``), identical to
``KimiSessionTail`` — portable across fuse / network mounts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.claude_parser import IncrementalClaudeParser
from aos_bench.monitor.tail.kimi import _FileTail
from aos_bench.monitor.tail.workflow import WorkflowFanoutTracker

# Match harbor's mount: CLAUDE_CONFIG_DIR = agent_dir / "sessions".
SHARE_DIRNAME = "sessions"
ROOT_RUN_ID = "run_root_001"


def _find_share_dir(trial_dir: Path) -> Path | None:
    """Find the Claude Code ``sessions/`` dir under a trial dir.

    Harbor's shape is ``<trial_dir>/agent/sessions`` but intermediate batch
    nesting is not stable, so we glob and prefer the shallowest match.
    """
    candidates = list(trial_dir.glob(f"**/{SHARE_DIRNAME}"))
    # Only ``sessions`` dirs that actually look like a Claude config dir
    # (contain ``projects/``) — avoids matching unrelated ``sessions`` names.
    candidates = [c for c in candidates if (c / "projects").is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: len(p.parts))
    return candidates[0]


def _find_root_session_dir(share_dir: Path) -> Path | None:
    """Pick the root session dir: a ``projects/<slug>/`` subtree's session
    folder holding ``*.jsonl`` whose path does NOT pass through ``subagents``.

    Mirrors harbor's ``_get_session_dir`` discovery, but tolerant of an
    unpopulated dir (returns ``None`` rather than logging) and of multiple
    candidates (picks the most-recently-modified instead of bailing).
    """
    project_root = share_dir / "projects"
    if not project_root.is_dir():
        return None
    session_dirs: list[Path] = []
    for project_dir in project_root.iterdir():
        if not project_dir.is_dir():
            continue
        jsonl_files = list(project_dir.rglob("*.jsonl"))
        session_dirs.extend(
            {f.parent for f in jsonl_files if "subagents" not in f.parent.parts}
        )
    if not session_dirs:
        return None
    session_dirs = list(dict.fromkeys(session_dirs))
    session_dirs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return session_dirs[0]


class ClaudeSessionTail:
    """Async tailer for one trial's Claude Code session."""

    def __init__(
        self,
        *,
        trial_id: str,
        trial_dir: Path,
        state: MonitorState,
        error_detector: ErrorDetector,
        interval_s: float = 1.5,
    ) -> None:
        self.trial_id = trial_id
        self.trial_dir = trial_dir
        self.state = state
        self.error_detector = error_detector
        self.interval_s = interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._session_dir: Path | None = None
        # One _FileTail per transcript file in the root session dir (Claude Code
        # may roll multiple *.jsonl; usually one).
        self._root_tails: dict[Path, _FileTail] = {}
        # One parser per run_id (root + each subagent agentId).
        self._parsers: dict[str, IncrementalClaudeParser] = {}
        # agentId → run_id mapping; agentId None / root-thread → ROOT_RUN_ID.
        self._agent_runs: dict[str | None, str] = {}
        # agentId → human label from agent-<id>.meta.json (description field).
        self._agent_labels: dict[str, str] = {}
        # Separate subagents/<id>/ session dirs (rarer build path).
        self._sub_tails: dict[Path, _FileTail] = {}
        # Workflow-tool fan-out (subagents/workflows/wf_*/) — its transcripts
        # need sidecar/journal handling the plain agentId routing can't do, and
        # the SDK tail needs the identical treatment, so it lives in one shared
        # helper rather than here.
        self._workflow = WorkflowFanoutTracker(
            trial_id=trial_id,
            state=state,
            error_detector=error_detector,
            parent_run_id=ROOT_RUN_ID,
        )
        self._reward_path: Path | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name=f"cc-tail-{self.trial_id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        self.state.ensure_trial(self.trial_id, str(self.trial_dir), agent="claude-code")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self.state.set_trial_status(self.trial_id, f"tail_error: {exc!r}")
            trial = self.state.trials.get(self.trial_id)
            if trial is not None and trial.status in {"completed", "failed", "crashed"}:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue

    # ── per-run helpers ──

    def _ensure_root_run(self) -> None:
        if ROOT_RUN_ID not in self._parsers:
            self._parsers[ROOT_RUN_ID] = IncrementalClaudeParser(
                run_id=ROOT_RUN_ID, synthesize_subagent_spawns=True
            )
            self._agent_runs[None] = ROOT_RUN_ID
            self.state.ensure_run(
                self.trial_id, ROOT_RUN_ID, role="root", agent_name="claude-code"
            )

    def _note_subagent_meta(self, jsonl: Path) -> None:
        """Resolve the human-readable lane label for a per-file subagent
        transcript, so the lane shows a task label instead of a hex id.

        Task-tool subagents get it from ``agent-<id>.meta.json`` (the
        ``description`` the caller passed). Workflow-tool agents are handled by
        :class:`WorkflowFanoutTracker` instead — their meta.json carries no
        description, so they need the sidecar.
        """
        name = jsonl.stem
        if not name.startswith("agent-"):
            return
        agent_id = name[len("agent-") :]
        if agent_id in self._agent_labels:
            return
        meta_path = jsonl.with_suffix(".meta.json")
        if not meta_path.is_file():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        desc = str(meta.get("description") or "").strip()
        if desc:
            self._agent_labels[agent_id] = desc[:48]

    def _run_id_for_agent(self, agent_id: str | None, *, is_sidechain: bool) -> str:
        """Map a record's ``agentId`` to a monitor run_id, creating the child
        run + parser on first sight of a new sidechain agent."""
        if not is_sidechain or not agent_id:
            return ROOT_RUN_ID
        existing = self._agent_runs.get(agent_id)
        if existing is not None:
            return existing
        run_id = f"run_sub_{agent_id}"
        self._agent_runs[agent_id] = run_id
        self._parsers[run_id] = IncrementalClaudeParser(run_id=run_id)
        self.state.ensure_run(
            self.trial_id,
            run_id,
            role="subagent",
            agent_name=self._agent_labels.get(agent_id, agent_id[:24]),
            parent_run_id=ROOT_RUN_ID,
        )
        self.state.update_run_status(self.trial_id, run_id, "running")
        return run_id

    def _dispatch_records(self, records: list[dict]) -> None:
        """Group raw records by target run, feed each run's parser, push to state.

        Records arrive in file order; grouping per poll keeps each parser's
        per-run sequence monotonic while letting one transcript carry both the
        root thread and many sidechain agents.
        """
        if not records:
            return
        self._ensure_root_run()
        # Preserve order but batch consecutive same-run records into one feed.
        by_run: dict[str, list[dict]] = {}
        ordered_runs: list[str] = []
        for rec in records:
            run_id = self._run_id_for_agent(
                rec.get("agentId"), is_sidechain=bool(rec.get("isSidechain"))
            )
            if run_id not in by_run:
                by_run[run_id] = []
                ordered_runs.append(run_id)
            by_run[run_id].append(rec)
        for run_id in ordered_runs:
            parser = self._parsers[run_id]
            events = parser.feed(by_run[run_id])
            if not events:
                continue
            self.state.push_events(self.trial_id, run_id, events)
            self.error_detector.scan(trial_id=self.trial_id, run_id=run_id, events=events)

    # ── main poll ──

    def _tick(self) -> None:
        if self._session_dir is None:
            self._maybe_detect_crashed()
            self._maybe_finalize_from_result_json()
            share = _find_share_dir(self.trial_dir)
            if share is None:
                return
            sess = _find_root_session_dir(share)
            if sess is None:
                return
            self._session_dir = sess
            self._ensure_root_run()
            self.state.set_trial_status(self.trial_id, "running")

        assert self._session_dir is not None

        # Root transcript files (any *.jsonl directly in the session dir).
        for jsonl in sorted(self._session_dir.glob("*.jsonl")):
            tail = self._root_tails.get(jsonl)
            if tail is None:
                tail = _FileTail(jsonl)
                self._root_tails[jsonl] = tail
            self._dispatch_records(tail.read_new_records_claude())

        # Separate subagent transcripts, wherever this build wrote them:
        #   - legacy flat:   <session_dir>/subagents/<id>/*.jsonl
        #   - 2.1.x nested:  <session_dir>/<session-uuid>/subagents/agent-<id>.jsonl
        # Both carry isSidechain+agentId, so the normal agentId routing in
        # _dispatch_records applies. Workflow-tool transcripts live under
        # ``subagents/workflows/`` and are skipped here — the tracker below
        # owns them, because naming and completing those lanes needs the
        # sidecar + journal, not just the transcript.
        for jsonl in sorted(self._session_dir.rglob("*.jsonl")):
            parts = jsonl.parts
            if "subagents" not in parts or "workflows" in parts:
                continue
            tail = self._sub_tails.get(jsonl)
            if tail is None:
                tail = _FileTail(jsonl)
                self._sub_tails[jsonl] = tail
            # Label resolution is retried until it succeeds: a transcript can
            # be globbed before its first record is flushed, and ensure_run
            # freezes agent_name at run-creation time.
            self._note_subagent_meta(jsonl)
            self._dispatch_records(tail.read_new_records_claude())

        self._workflow.tick([self._session_dir])
        self._maybe_load_reward()
        self._maybe_finalize_from_result_json()

    # ── finalization (mirrors KimiSessionTail) ──

    def _maybe_detect_crashed(self) -> None:
        trial = self.state.trials.get(self.trial_id)
        if trial is None or trial.status not in ("discovered",):
            return
        candidates = [
            self.trial_dir / "exception.txt",
            *self.trial_dir.glob("*/exception.txt"),
            *self.trial_dir.glob("*/*/exception.txt"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                snippet = ""
                try:
                    snippet = candidate.read_text(encoding="utf-8", errors="replace")[:400]
                except OSError:
                    pass
                self.state.set_trial_status(self.trial_id, "crashed")
                from aos_bench.monitor.state import ErrorRecord

                self.state.add_error(
                    ErrorRecord(
                        trial_id=self.trial_id,
                        run_id=None,
                        ts=candidate.stat().st_mtime if candidate.exists() else 0.0,
                        rule_id="harbor_setup_crash",
                        snippet=snippet.splitlines()[-1] if snippet else "(see exception.txt)",
                    )
                )
                return

    def _maybe_load_reward(self) -> None:
        if self._reward_path is None:
            if self._session_dir is None:
                return
            cursor: Path | None = self._session_dir
            for _ in range(8):
                if cursor is None or cursor == self.trial_dir.parent:
                    break
                for fname in ("reward.txt", "reward.json"):
                    candidate = cursor / "verifier" / fname
                    if candidate.exists():
                        self._reward_path = candidate
                        break
                if self._reward_path is not None:
                    break
                cursor = cursor.parent
            if self._reward_path is None:
                return
        if not self._reward_path.exists():
            return
        try:
            text = self._reward_path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not text:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                data = float(text)
            except ValueError:
                return
        if isinstance(data, dict):
            vals = [v for v in data.values() if isinstance(v, (int, float))]
            if vals:
                self.state.set_trial_reward(self.trial_id, sum(vals) / len(vals))
                self.state.set_trial_status(self.trial_id, "completed")
        elif isinstance(data, (int, float)):
            self.state.set_trial_reward(self.trial_id, float(data))
            self.state.set_trial_status(self.trial_id, "completed")

    def _maybe_finalize_from_result_json(self) -> None:
        trial = self.state.trials.get(self.trial_id)
        if trial is None or trial.status in ("completed", "failed", "crashed"):
            return
        candidates = [
            self.trial_dir / "result.json",
            *self.trial_dir.glob("*/result.json"),
            *self.trial_dir.glob("*/*/result.json"),
        ]
        result_path = next((c for c in candidates if c.is_file()), None)
        if result_path is None:
            return
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        verifier_result = data.get("verifier_result")
        exception_info = data.get("exception_info")
        reward: float | None = None
        if isinstance(verifier_result, dict):
            rewards = verifier_result.get("rewards")
            if isinstance(rewards, dict):
                vals = [v for v in rewards.values() if isinstance(v, (int, float))]
                if vals:
                    reward = sum(vals) / len(vals)
            elif isinstance(rewards, (int, float)):
                reward = float(rewards)
        if reward is not None:
            self.state.set_trial_reward(self.trial_id, reward)
            self.state.set_trial_status(self.trial_id, "completed")
            return
        if exception_info:
            self.state.set_trial_status(self.trial_id, "failed")
            return
        self.state.set_trial_status(self.trial_id, "completed")
