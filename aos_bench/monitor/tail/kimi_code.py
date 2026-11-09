"""Live tail of one kimi-code session directory.

A harbor kimi-code trial sets ``KIMI_CODE_HOME = <trial>/agent/kimi-code-home``
(see ``harbor/src/harbor/agents/installed/kimi_code.py``: ``_HOME_DIRNAME =
"kimi-code-home"``), and the CLI writes one journal per agent under

    <home>/sessions/wd_<slug>_<sha256[:12]>/session_<uuid>/agents/<agentId>/wire.jsonl

with a sibling ``state.json`` (parent links + labels) and a home-level
``session_index.jsonl`` naming the live session dirs.

Unlike Claude Code — which interleaves sidechain turns into the *same*
transcript and tags them with ``agentId`` — kimi-code splits agents
**physically**: one directory, one journal, one lane. So this tail keeps one
``_FileTail`` + one :class:`IncrementalKimiCodeParser` per ``agents/<id>/``
directory and needs no per-record routing (contrast ``claude.py:190-217``).
``main`` is the root (``run_root_001``); every other id becomes
``run_sub_<agentId>``.

Three things this tail does that ``KimiSessionTail`` does not, all required by
the parity checklist in ``docs/tasks/kimi_code_integration.md`` §4:

- **Re-resolve every tick, never bind once** (P1). ``KimiSessionTail`` binds one
  session dir at discovery (``kimi.py:163``, ``:219-220``) and never re-checks;
  a kimi-code ``-r`` / ``-c`` resume or a fork creates a *second*
  ``session_<uuid>`` dir, so the newest session is re-resolved on every tick and
  the lanes are re-pointed at the new journals when it moves (the parsers are
  kept, so the lane's ``seq`` / token totals continue across the resume).
- **Lazy per-agent lane creation from the directory listing** (P2). A lane is
  created on first sight of ``agents/<id>/``, never on a spawn marker, so a
  child lane exists even when the spawn record was missed — the same reason
  ``collect_kimi_code_agent_trace`` builds runs from directories
  (``aos_bench/trace/kimi_code.py:1376-1377``).
- **Per-tick refresh of labels, parent links and status** (P3, P4).
  ``state.json`` is rewritten atomically as agents come and go, so it is
  re-read whenever it changes and the lane's label / parent link is
  updated in place. Per-agent *status* is not in ``state.json`` (verified
  against every landed fixture: the per-agent block is
  ``{homedir, type, parentAgentId?, labels?}``), so it comes from the journal
  via the same ``_run_status`` rules the offline trace uses.

Labels, parent links and statuses are all computed with the helpers from
``aos_bench/trace/kimi_code.py`` rather than reimplemented, for the reason
``kimi_code_parser.py`` imports its kind map from there: the live dashboard and
the post-run AgentTrace must describe the same run in the same words.

Tail is poll-based (byte-offset re-read every ``interval``), identical to
``KimiSessionTail`` / ``ClaudeSessionTail`` / ``CodexSessionTail`` — portable
across fuse / network mounts.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import ErrorRecord, MonitorState
from aos_bench.monitor.tail.kimi import _FileTail
from aos_bench.monitor.tail.kimi_code_parser import IncrementalKimiCodeParser
from aos_bench.monitor.tasks import extract_task_lifecycle_from_events

# Shared with the offline converter on purpose — see the module docstring. The
# private helpers are pure functions over already-read data; importing them is
# what keeps a lane's label / parent / status identical to the AgentTrace run
# the same session produces after the trial (a rename upstream breaks this
# import loudly at collection time, which is the intended failure mode).
from aos_bench.trace.kimi_code import (
    ROOT_AGENT_ID,
    ROOT_RUN_ID,
    _agent_name,
    _read_json,
    _resolve_parent_agent_id,
    _run_id_for,
    _run_status,
    _sorted_agent_ids,
    find_kimi_code_latest_session_dir,
)

# Match harbor's mount: ``KIMI_CODE_HOME = <agent_dir>/kimi-code-home``. This
# name is also the monitor's classification marker for the arm, so it must stay
# in step with ``_HOME_DIRNAME`` in the harbor adapter.
HOME_DIRNAME = "kimi-code-home"

# Trial statuses that mean "the trial is over". Once a trial reaches one of
# these the tail must not overwrite the status — a late stat() race or a
# racing reward.txt rewrite must not flip a completed run back to
# ``tail_error`` (only ``CodexSessionTail`` guards this today: codex.py:57,
# :186-197; blueprint §4 P7).
_TERMINAL_STATUSES = frozenset({"completed", "failed", "crashed"})

# ``_run_status`` speaks AgentTrace's vocabulary; ``created`` (a lane whose
# journal has not produced an event yet) has no dashboard colour of its own
# (``static/monitor.css:41-44`` styles running / completed / discovered / idle /
# failed / killed), so it is spelled the way the other tails spell an empty
# lane (``kimi.py:307``).
_STATUS_ALIASES = {"created": "discovered"}


def _find_home_dir(trial_dir: Path) -> Path | None:
    """Find the ``kimi-code-home`` dir under a trial dir.

    Harbor's shape is ``<trial_dir>/agent/kimi-code-home`` but the intermediate
    batch nesting is not stable, so we glob and prefer the shallowest match
    (same approach as ``kimi.py:49-63`` / ``claude.py:38-51``).

    Deliberately *not* filtered on ``sessions/`` existing: the home dir is the
    arm's marker, and the adapter mkdirs it as the first step of ``install()``
    — before the node/npm work that can fail. A trial whose agent died during
    install must still be classified kimi-code so ``_maybe_detect_crashed`` can
    report it.
    """

    candidates = [p for p in trial_dir.glob(f"**/{HOME_DIRNAME}") if p.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: len(p.parts))
    return candidates[0]


@dataclass
class _AgentLane:
    """One ``agents/<id>/`` directory: its journal tail, parser and run.

    The cached ``label`` / ``model_name`` / ``parent_run_id`` / ``status`` are
    the last values pushed to :class:`MonitorState`, so the per-tick refresh
    only writes (and only publishes an SSE delta) when something actually
    changed.
    """

    agent_id: str
    run_id: str
    wire_path: Path
    tail: _FileTail
    parser: IncrementalKimiCodeParser
    label: str | None = None
    model_name: str | None = None
    parent_run_id: str | None = None
    status: str | None = None

    @property
    def is_root(self) -> bool:
        return self.agent_id == ROOT_AGENT_ID


class KimiCodeSessionTail:
    """Async tailer for one trial's kimi-code session (root + every subagent)."""

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
        self._home_dir: Path | None = None
        self._session_dir: Path | None = None
        # agent id → lane. Keyed by agent id (not by path) so a resume into a
        # new session dir keeps the same lane, hence the same run_id.
        self._agents: dict[str, _AgentLane] = {}
        # Last parsed ``state.json`` + the (mtime, size) it was read at. Re-read
        # only when that stamp moves: the file is rewritten atomically on every
        # agent lifecycle change and is otherwise static.
        self._session_state: dict[str, Any] = {}
        self._state_stamp: tuple[float, int] | None = None
        self._reward_path: Path | None = None
        # Running accumulator of ALL root events, for task-lifecycle
        # extraction: ``events_tail`` is capped and would lose the earliest
        # ``todo_list`` response on a long trial (``kimi.py:166-171``).
        self._all_root_events: list[dict] = []

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._loop(), name=f"kimi-code-tail-{self.trial_id}"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        # Register the trial up-front so the dashboard sees it before the first
        # event lands. Mirrors the other three tails.
        self.state.ensure_trial(self.trial_id, str(self.trial_dir), agent="kimi-code")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                # Don't let a per-trial fault kill the whole monitor — but never
                # overwrite a terminal status with it (blueprint §4 P7,
                # ``codex.py:186-197``).
                trial = self.state.trials.get(self.trial_id)
                if trial is None or trial.status not in _TERMINAL_STATUSES:
                    self.state.set_trial_status(self.trial_id, f"tail_error: {exc!r}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue

    # ── main poll ──

    def _tick(self) -> None:
        # Before any lane exists harbor may already have declared the trial dead
        # (the npm install of the pinned CLI failed, the image had no node ≥22,
        # docker proxy reject, …). Surface that without waiting for a session
        # that will never appear.
        if not self._agents:
            self._maybe_detect_crashed()
            self._maybe_finalize_from_result_json()

        home = self._home_dir or _find_home_dir(self.trial_dir)
        if home is None:
            return
        self._home_dir = home

        # P1: resolve the session dir on EVERY tick. ``session_index.jsonl`` is
        # the cheap path and is re-rooted onto the host-side home for us; the
        # tiered glob is the fallback. A transient IO error here (cloud mount
        # blip, the index being appended to as we read it) must not surface as
        # ``tail_error`` on the dashboard — the next tick retries.
        try:
            session_dir = find_kimi_code_latest_session_dir(home)
        except OSError:
            return
        if session_dir is None:
            return
        if session_dir != self._session_dir:
            self._bind_session(session_dir)

        self._refresh_session_state()  # P3 / P4 inputs
        self._scan_agent_dirs()  # P2
        self._pump_lanes()  # events → state → errors (+ P5 on the root)
        self._refresh_runs()  # P3 labels + P4 status

        self._maybe_load_reward()
        # Final fallback: harbor's result.json is the canonical "trial ended"
        # signal, and catches the cases reward.txt / exception.txt miss.
        self._maybe_finalize_from_result_json()

    # ── session + agent discovery ──

    def _bind_session(self, session_dir: Path) -> None:
        """Point the tail at ``session_dir``, keeping existing lanes.

        On the first bind this just records the dir and flips the trial to
        ``running``. On a later bind — a ``kimi -r`` resume or a fork wrote a
        second ``session_<uuid>`` and it is now the newest — every known lane is
        re-pointed at the *new* journal with a fresh ``_FileTail`` (offset 0:
        the new file starts with its own ``metadata`` header) while keeping its
        parser, so the lane's ``seq``, step ids and token totals carry across
        the resume instead of restarting and colliding with already-pushed
        event ids.
        """

        previous = self._session_dir
        self._session_dir = session_dir
        # The new session has its own state.json; force a re-read.
        self._session_state = {}
        self._state_stamp = None

        if previous is None:
            trial = self.state.trials.get(self.trial_id)
            if trial is not None and trial.status == "discovered":
                self.state.set_trial_status(self.trial_id, "running")
            return

        for lane in self._agents.values():
            wire_path = session_dir / "agents" / lane.agent_id / "wire.jsonl"
            lane.wire_path = wire_path
            lane.tail = _FileTail(wire_path)

    def _refresh_session_state(self) -> None:
        """Re-read ``state.json`` when it changed (P4).

        The file is written by an atomic rename, so a successful read is always
        a whole document; an unreadable one keeps the previous contents rather
        than dropping the parent links we already had.

        The change stamp is ``(mtime, size)`` rather than mtime alone, and it is
        ignored entirely while a lane we already know about is still missing from
        the parsed copy — on a filesystem with coarse mtime granularity a rewrite
        landing in the same tick as our read would otherwise be invisible
        forever, and that rewrite is exactly the one carrying the new agent's
        parent link and swarm item.
        """

        if self._session_dir is None:
            return
        path = self._session_dir / "state.json"
        try:
            stat = path.stat()
        except OSError:
            return
        stamp = (stat.st_mtime, stat.st_size)
        if stamp == self._state_stamp and not self._lanes_missing_from_state():
            return
        self._state_stamp = stamp
        state = _read_json(path)
        if state:
            self._session_state = state

    def _lanes_missing_from_state(self) -> bool:
        """True while a registered child lane has no ``state.json`` entry yet."""

        known = self._state_agents()
        return any(
            agent_id not in known
            for agent_id in self._agents
            if agent_id != ROOT_AGENT_ID
        )

    def _state_agents(self) -> dict[str, Any]:
        agents = self._session_state.get("agents")
        return agents if isinstance(agents, dict) else {}

    def _agent_meta(self, agent_id: str) -> dict[str, Any]:
        meta = self._state_agents().get(agent_id)
        return meta if isinstance(meta, dict) else {}

    def _agent_labels(self, agent_id: str) -> dict[str, Any]:
        labels = self._agent_meta(agent_id).get("labels")
        return labels if isinstance(labels, dict) else {}

    def _scan_agent_dirs(self) -> None:
        """Create a lane for every agent we haven't seen yet (P2).

        Agent ids come from the directories on disk *and* from ``state.json``: a
        directory can exist before ``state.json`` is rewritten, and
        ``state.json`` can name an agent whose journal has not been flushed yet
        (``trace/kimi_code.py:478-487``). ``_sorted_agent_ids`` puts ``main``
        first so the root run exists before a child names it as its parent.
        """

        if self._session_dir is None:
            return
        agents_root = self._session_dir / "agents"
        dir_ids: set[str] = set()
        if agents_root.is_dir():
            try:
                dir_ids = {p.name for p in agents_root.iterdir() if p.is_dir()}
            except OSError:
                # Directory vanished / transient mount error: retry next tick.
                return
        known = _sorted_agent_ids(dir_ids | set(self._state_agents()))
        for agent_id in known:
            if agent_id not in self._agents:
                self._register_agent(agent_id, known_ids=known)

    def _register_agent(self, agent_id: str, *, known_ids: list[str]) -> None:
        assert self._session_dir is not None
        run_id = _run_id_for(agent_id)
        is_root = agent_id == ROOT_AGENT_ID
        wire_path = self._session_dir / "agents" / agent_id / "wire.jsonl"
        lane = _AgentLane(
            agent_id=agent_id,
            run_id=run_id,
            wire_path=wire_path,
            tail=_FileTail(wire_path),
            # Spawn synthesis runs on EVERY lane, not just the root: the
            # ``agent`` profile keeps Agent / AgentSwarm, so nesting is
            # possible, and the child id is recovered exactly from the tool
            # result — the same reasoning as the offline collector
            # (``trace/kimi_code.py:1387-1390``).
            parser=IncrementalKimiCodeParser(
                run_id=run_id, synthesize_subagent_spawns=True
            ),
        )
        lane.label = self._lane_label(lane)
        lane.parent_run_id = None if is_root else self._lane_parent(agent_id, known_ids)
        self._agents[agent_id] = lane
        self.state.ensure_run(
            self.trial_id,
            run_id,
            role="root" if is_root else "subagent",
            agent_name=lane.label,
            parent_run_id=lane.parent_run_id,
        )

    def _lane_label(self, lane: _AgentLane) -> str:
        """Human lane label (P3): the root is ``orchestrator``, a child is its
        own profile name suffixed with the swarm item it was given, falling back
        to ``agentId[:24]`` until ``profile.bind`` / ``config.update`` lands."""

        return _agent_name(
            lane.agent_id,
            {"profile_name": lane.parser.profile_name},
            self._agent_labels(lane.agent_id),
            is_root=lane.is_root,
        )

    def _lane_parent(self, agent_id: str, known_ids: list[str]) -> str | None:
        """``run_sub_<parent>`` for a child lane, or ``run_root_001``.

        ``labels.parentAgentId`` wins over the weaker top-level
        ``parentAgentId``, and a child with no ``state.json`` entry yet is
        assumed to hang off the root (which every observed session does anyway).
        """

        parent = _resolve_parent_agent_id(
            agent_id,
            self._agent_meta(agent_id),
            self._agent_labels(agent_id),
            known_ids,
        )
        if parent is None:
            return None
        return _run_id_for(parent)

    # ── event pumping ──

    def _pump_lanes(self) -> None:
        """Drain each lane's journal in a deterministic order (root first).

        Root first matters for the dashboard's arrows: the root's spawn markers
        should be pushed before the child lane's own events on the tick where
        both first appear.
        """

        for agent_id in _sorted_agent_ids(self._agents):
            lane = self._agents[agent_id]
            records = lane.tail.read_new_records_kimi_code()
            if not records:
                continue
            events = lane.parser.feed(records)
            if not events:
                continue
            self.state.push_events(self.trial_id, lane.run_id, events)
            self.error_detector.scan(
                trial_id=self.trial_id, run_id=lane.run_id, events=events
            )
            if lane.run_id == ROOT_RUN_ID:
                self._absorb_root_events(events)

    def _absorb_root_events(self, events: list[dict[str, Any]]) -> None:
        """Keep an uncapped buffer of root events and re-extract the dispatcher
        task lifecycle from it (P5).

        ``events_tail`` is capped (``state.py:30``), so a long trial would lose
        the ``todo_list`` snapshot the dispatcher panel is built from. Today
        only ``KimiSessionTail`` does this (``kimi.py:241-251``); without it the
        ``/tasks`` endpoint falls back to re-parsing the capped tail.
        """

        self._all_root_events.extend(events)
        if len(self._all_root_events) > 10000:
            self._all_root_events = self._all_root_events[-8000:]
        snapshot = extract_task_lifecycle_from_events(self._all_root_events)
        if snapshot:
            self.state.set_trial_task_snapshot(self.trial_id, snapshot)

    # ── per-tick run refresh (P3 / P4) ──

    def _refresh_runs(self) -> None:
        """Push label / model / parent / status changes into the run summaries.

        ``MonitorState`` has no setter for a run's label, model or parent — they
        are ctor-time arguments of :meth:`MonitorState.ensure_run` — so these
        are assigned on the ``RunSummary`` directly. That is safe for the same
        reason the parser's spawn back-fill is: ``state.py`` stores runs by
        reference and re-serializes them on every API read, so the next poll
        shows the new value. Only ``status`` has a setter (which publishes an
        SSE delta), and it is only called when the value actually changed.
        """

        trial = self.state.trials.get(self.trial_id)
        if trial is None:
            return
        known = _sorted_agent_ids(self._agents)
        for agent_id in known:
            lane = self._agents[agent_id]
            run = trial.runs.get(lane.run_id)
            if run is None:
                continue

            label = self._lane_label(lane)
            if label != lane.label:
                lane.label = label
                run.agent_name = label

            model_name = lane.parser.model_name
            if model_name and model_name != lane.model_name:
                lane.model_name = model_name
                run.model_name = model_name

            if not lane.is_root:
                parent_run_id = self._lane_parent(agent_id, known)
                if parent_run_id and parent_run_id != lane.parent_run_id:
                    lane.parent_run_id = parent_run_id
                    run.parent_run_id = parent_run_id

            status = self._lane_status(lane)
            if status != lane.status:
                lane.status = status
                self.state.update_run_status(self.trial_id, lane.run_id, status)

    def _lane_status(self, lane: _AgentLane) -> str:
        """Lane status from the journal (P4).

        ``state.json``'s per-agent block carries no status field (verified
        against every landed fixture), and its session-level ``lastTurnReason``
        describes the *session*, not one agent — so status comes from the same
        two signals the offline trace uses: ``turn.ended.reason`` when present,
        else the last ``step.end.finishReason`` (the legacy engine never writes
        a ``turn.ended``).

        Both signals are per-turn, not sticky: ``turn.ended{reason:'completed'}``
        fires after EVERY turn, so the parser clears them as soon as the next
        turn's first record lands (``kimi_code_parser.py``). That is what makes
        this a liveness signal — an orchestrator that finished turn 1 and is now
        driving turn 2 reads ``running``, not ``completed``, for the rest of the
        trial. A lane genuinely between turns (waiting on a background task)
        reads ``completed`` until its next ``turn.prompt``, which is the honest
        answer to "is this lane in a turn right now".
        """

        parser = lane.parser
        status = _run_status(
            {
                "last_turn_reason": parser.last_turn_reason,
                "last_finish_reason": parser.last_finish_reason,
            },
            # ``_run_status`` only tests this list for emptiness — "did this lane
            # produce any event at all". The parser's seq counter is exactly
            # that, without holding a second copy of the events.
            [] if parser.seq == 0 else [{}],
        )
        return _STATUS_ALIASES.get(status, status)

    # ── finalization (mirrors KimiSessionTail / CodexSessionTail) ──

    def _maybe_detect_crashed(self) -> None:
        """Mark the trial ``crashed`` if harbor dropped an ``exception.txt``
        before kimi-code ever wrote a journal. Common cause on this arm: the
        pinned ``npm i -g @moonshot-ai/kimi-code`` failed, or the image's node
        was too old for the shipped bundle to start — no session dir ever
        materialises and the trial would otherwise sit at ``discovered``
        forever. Idempotent: only flips status once."""

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
                    snippet = candidate.read_text(encoding="utf-8", errors="replace")[
                        :400
                    ]
                except OSError:
                    pass
                self.state.set_trial_status(self.trial_id, "crashed")
                self.state.add_error(
                    ErrorRecord(
                        trial_id=self.trial_id,
                        run_id=None,
                        ts=candidate.stat().st_mtime if candidate.exists() else 0.0,
                        rule_id="harbor_setup_crash",
                        snippet=(
                            snippet.splitlines()[-1]
                            if snippet
                            else "(see exception.txt)"
                        ),
                    )
                )
                return

    def _maybe_load_reward(self) -> None:
        """Walk up from the session dir looking for a sibling ``verifier/``.

        Harbor's stream verifier emits ``reward.txt`` (a single float) by
        default; older paths emit ``reward.json`` (dict or float). Identical
        contract to ``KimiSessionTail._maybe_load_reward``, plus codex's
        terminal-status guard (``codex.py:390-392``): without it the tail
        re-reads reward.txt every poll and re-publishes ``set_trial_reward``
        (which has no internal dedupe), spamming SSE subscribers with one
        ``trial_update`` per tick for the rest of the monitor's life.
        """

        trial = self.state.trials.get(self.trial_id)
        if trial is not None and trial.status in _TERMINAL_STATUSES:
            return
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
        """Read harbor's per-trial ``result.json`` as the canonical "trial
        ended" signal. Mirrors ``KimiSessionTail._maybe_finalize_from_result_json``:

          - ``verifier_result.rewards`` present → ``completed`` with the
            averaged reward, even when ``exception_info`` is also set (the agent
            timed out cleanly and the verifier still graded — the common case
            for timeout-bounded streams)
          - ``exception_info`` only → harbor died before the verifier →
            ``failed``
          - neither → atypical; treated as terminal-unknown so we stop polling

        Idempotent: once the status is terminal this is a no-op.
        """

        trial = self.state.trials.get(self.trial_id)
        if trial is None or trial.status in _TERMINAL_STATUSES:
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
