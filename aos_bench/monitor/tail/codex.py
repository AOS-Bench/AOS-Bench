"""Live tail of one codex CLI session directory.

A harbor codex trial sets ``CODEX_HOME = <trial_dir>/agent`` (see
``harbor/src/harbor/agents/installed/codex.py``: the adapter writes
``$CODEX_HOME/config.toml`` and codex itself drops session logs under
``$CODEX_HOME/sessions/<yyyy>/<mm>/<dd>/rollout-<ts>-<sid>.jsonl``).

Each ``codex exec`` invocation produces ONE rollout-*.jsonl. Codex 0.122+
adds the ``spawn_agent`` tool which lets the root agent dispatch a fresh
``codex`` subprocess; each spawned worker writes its OWN rollout file
under the same ``sessions/`` tree, sharing the parent's ``session_id``
but with a unique ``id`` (= "thread id" = the ``agent_id`` returned by
the spawn call). So a single trial can produce a tree of rollouts:

    sessions/<y>/<m>/<d>/rollout-<root_ts>-<root_thread_id>.jsonl   ← root, no agent_role
    sessions/<y>/<m>/<d>/rollout-<sub_ts>-<sub_thread_id>.jsonl     ← agent_role="worker"

Each sub rollout's ``session_meta.id`` matches the ``agent_id`` returned
from the root's ``spawn_agent`` tool_call_output; that mapping is what
lets us route the child run back to the right parent step.

The tail polls the ``sessions/`` directory for new rollout files (codex
creates them eagerly when a worker starts), tails each via its own
``_FileTail`` + ``IncrementalCodexParser``, and keys runs by the rollout
file path. The first rollout discovered is treated as the root run
(``run_root_001``); subsequent rollouts are treated as subagent runs
keyed by their own thread id (``run_sub_<thread_id>``).

Tail is poll-based (byte-offset re-read every ``interval``), identical to
``KimiSessionTail`` / ``ClaudeSessionTail`` — portable across fuse / network
mounts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import ErrorRecord, MonitorState
from aos_bench.monitor.tail.codex_parser import (
    IncrementalCodexParser,
    parse_codex_line,
)
from aos_bench.monitor.tail.kimi import _FileTail

# Match harbor's mount: ``CODEX_HOME = <agent_dir>``; codex drops session
# logs under ``$CODEX_HOME/sessions/...``.
SHARE_DIRNAME = "sessions"
ROOT_RUN_ID = "run_root_001"

# Trial statuses that mean "the trial is over". Once a trial reaches one
# of these, the tail must not overwrite the status (e.g. a transient
# stat() race in _tick after the trial completed would otherwise revert
# the dashboard to ``tail_error``).
_TERMINAL_STATUSES = frozenset({"completed", "failed", "crashed"})


def _find_share_dir(trial_dir: Path) -> Path | None:
    """Find the codex ``sessions/`` dir under a trial dir.

    Harbor's shape is ``<trial_dir>/agent/sessions`` (the adapter sets
    ``CODEX_HOME = agent_dir``, so codex writes ``sessions/`` inside it).
    Intermediate batch nesting is not stable, so we glob and prefer the
    shallowest match that actually contains the date hierarchy.
    """
    candidates = list(trial_dir.glob(f"**/{SHARE_DIRNAME}"))
    # Restrict to ``sessions`` dirs whose immediate children look like
    # 4-digit year directories — guards against false positives from
    # unrelated ``sessions`` names elsewhere in the trial tree.
    filtered: list[Path] = []
    for c in candidates:
        if not c.is_dir():
            continue
        if any(child.is_dir() and len(child.name) == 4 and child.name.isdigit() for child in c.iterdir()):
            filtered.append(c)
    if not filtered:
        return None
    filtered.sort(key=lambda p: len(p.parts))
    return filtered[0]


def _find_all_rollouts(share_dir: Path) -> list[Path]:
    """Return every ``rollout-*.jsonl`` under ``sessions/``, sorted by
    create time (mtime ascending) so the first item is the root rollout
    and subsequent items are subagent rollouts in spawn order.

    Tolerates OS errors per file: if a stat() races a delete or hits a
    transient cloud-mount glitch, that file is dropped from this scan
    rather than aborting the whole tick. The caller polls again on the
    next interval so a recoverable miss self-heals.
    """
    candidates = list(
        share_dir.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/rollout-*.jsonl")
    )
    decorated: list[tuple[float, Path]] = []
    for p in candidates:
        try:
            decorated.append((p.stat().st_mtime, p))
        except OSError:
            # File vanished between glob and stat, or the mount returned
            # a transient error. Skip silently; ``_FileTail`` instances
            # already pointing at this path will surface the read failure
            # on the next ``_read_new_lines`` and recover when the FS
            # settles.
            continue
    decorated.sort(key=lambda item: item[0])
    return [p for _, p in decorated]


def _peek_session_meta(rollout: Path) -> dict | None:
    """Read just the first line of a rollout to extract session_meta. Used
    to classify a rollout as root vs sub without forcing a full
    IncrementalCodexParser instance up front.
    """
    try:
        with rollout.open("r", encoding="utf-8", errors="replace") as f:
            line = f.readline()
    except OSError:
        return None
    rec = parse_codex_line(line)
    if rec is None:
        return None
    if rec.get("type") != "session_meta":
        return None
    payload = rec.get("payload")
    return payload if isinstance(payload, dict) else None


class CodexSessionTail:
    """Async tailer for one trial's codex session (root + any spawned subagents)."""

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
        # rollout_path → (_FileTail, IncrementalCodexParser, run_id, role)
        self._rollouts: dict[
            Path, tuple[_FileTail, IncrementalCodexParser, str, str]
        ] = {}
        # Path of the root rollout — set on first registration. Used to
        # decide whether a newly-discovered rollout is the root (it
        # isn't if we already have one).
        self._root_rollout: Path | None = None
        # Cumulative child_id → spawn_call_id discovered across feeds of
        # the root parser. Sub rollouts look up their thread_id here to
        # find which root spawn_agent call birthed them.
        self._spawn_map: dict[str, str] = {}
        # Native thread id -> normalized run id. This preserves nested Codex
        # parentage and keeps full UUID run ids aligned with spawn events.
        self._thread_runs: dict[str, str] = {}
        self._reward_path: Path | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._loop(), name=f"codex-tail-{self.trial_id}"
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
        # Register trial up-front so the dashboard sees it even before the
        # first event lands. Mirrors KimiSessionTail / ClaudeSessionTail.
        self.state.ensure_trial(self.trial_id, str(self.trial_dir), agent="codex")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                # Don't let a per-trial fault kill the whole monitor. Once
                # the trial reached a terminal status (completed / failed /
                # crashed), do NOT overwrite it — a late stat() race or a
                # racing reward.txt rewrite must not flip a completed run
                # back to ``tail_error`` (downstream aggregators filter by
                # terminal status).
                trial = self.state.trials.get(self.trial_id)
                if trial is None or trial.status not in _TERMINAL_STATUSES:
                    self.state.set_trial_status(
                        self.trial_id, f"tail_error: {exc!r}"
                    )
            trial = self.state.trials.get(self.trial_id)
            if trial is not None and trial.status in _TERMINAL_STATUSES:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue

    def _tick(self) -> None:
        # Before any rollout exists harbor may have already declared the
        # trial dead (npm install timeout, docker proxy reject, …). Surface
        # that without waiting for a session that will never appear.
        if not self._rollouts:
            self._maybe_detect_crashed()
            self._maybe_finalize_from_result_json()

        share = _find_share_dir(self.trial_dir)
        if share is None:
            return

        # 1) Discover any newly-created rollout files (codex spawns workers
        # by starting fresh ``codex exec`` subprocesses, each of which
        # eagerly creates its own rollout-*.jsonl).
        self._discover_rollouts(share)
        if not self._rollouts:
            return

        # First time we register the root rollout, mark the trial running.
        # ensure_trial alone leaves status="discovered".
        trial = self.state.trials.get(self.trial_id)
        if trial is not None and trial.status == "discovered":
            self.state.set_trial_status(self.trial_id, "running")

        # 2) Drain new lines from each tail in deterministic order (root
        # first, then subagents in spawn order). Root's parser updates
        # the spawn_map; we re-import that into self._spawn_map after
        # each feed so subsequent rollout discoveries can resolve their
        # parent.
        ordered = self._rollouts_in_order()
        for rollout_path in ordered:
            tail, parser, run_id, _role = self._rollouts[rollout_path]
            records = tail.read_new_records_codex()
            if not records:
                continue
            events = parser.feed(records)
            if events:
                self.state.push_events(self.trial_id, run_id, events)
                self.error_detector.scan(
                    trial_id=self.trial_id, run_id=run_id, events=events
                )
            # Pick up any new spawn_call_id → child_id mappings produced
            # by the root parser; ignore sub parsers' spawn_map (they
            # shouldn't spawn, but defensively merge so any future
            # nested fanout still maps).
            for cid, child in parser.spawn_map.items():
                if cid not in self._spawn_map:
                    self._spawn_map[cid] = child

        self._maybe_load_reward()
        self._maybe_finalize_from_result_json()

    # ── rollout discovery + registration ──

    def _discover_rollouts(self, share: Path) -> None:
        """Scan ``share`` for rollout files and register any we haven't
        seen yet. Order matters: the first (oldest mtime) rollout that
        carries no ``agent_role`` is the root; everything else is a
        sub. We also defensively reject a rollout whose session_meta
        is unreadable (yet) — we'll pick it up on the next tick once
        codex has written the header line.
        """
        for rollout in _find_all_rollouts(share):
            if rollout in self._rollouts:
                continue
            meta = _peek_session_meta(rollout)
            if meta is None:
                # Header not flushed yet — defer until next scan.
                continue
            self._register_rollout(rollout, meta)

    def _register_rollout(self, rollout: Path, meta: dict) -> None:
        agent_role = meta.get("agent_role")
        thread_id = meta.get("id")
        is_root = not agent_role and self._root_rollout is None
        if is_root:
            run_id = ROOT_RUN_ID
            role = "root"
            agent_name = "codex"
            parent_run_id = None
            model_name = None
            self._root_rollout = rollout
        else:
            # Build the run id from the rollout's own thread id (so
            # different sub workers don't collide). Even if the rollout
            # somehow has no thread_id, fall back to the filename stem
            # to keep it unique.
            native_id = str(thread_id or rollout.stem)
            short = native_id[:24]
            run_id = f"run_sub_{native_id}"
            role = "subagent"
            nickname = meta.get("agent_nickname")
            agent_name = (
                f"codex/{nickname}" if isinstance(nickname, str) and nickname else f"codex/{short}"
            )
            parent_thread_id = meta.get("parent_thread_id")
            parent_run_id = self._thread_runs.get(str(parent_thread_id), ROOT_RUN_ID)
            model_name = None

        if thread_id:
            self._thread_runs[str(thread_id)] = run_id

        self.state.ensure_run(
            self.trial_id,
            run_id,
            role=role,
            agent_name=agent_name,
            parent_run_id=parent_run_id,
            model_name=model_name,
        )

        tail = _FileTail(rollout)
        parser = IncrementalCodexParser(run_id=run_id)
        self._rollouts[rollout] = (tail, parser, run_id, role)

    def _rollouts_in_order(self) -> list[Path]:
        """Root rollout first, then subagent rollouts in mtime order so
        spawn_map updates happen before the children that depend on them
        are drained.

        Tolerates per-file stat failures: a rollout whose stat raises is
        evicted from ``self._rollouts`` (its tail is unreachable anyway
        once the file is gone) so subsequent ticks don't keep retrying
        the dead path. ``_FileTail`` holds no persistent fd so eviction
        is a plain dict pop.
        """
        root_first: list[Path] = []
        subs: list[tuple[float, Path]] = []
        dead: list[Path] = []
        for path, (_, _, _, role) in self._rollouts.items():
            if role == "root":
                root_first.append(path)
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                dead.append(path)
                continue
            subs.append((mtime, path))
        for path in dead:
            self._rollouts.pop(path, None)
        subs.sort(key=lambda item: item[0])
        return root_first + [p for _, p in subs]

    # ── finalization (mirrors KimiSessionTail) ──

    def _maybe_detect_crashed(self) -> None:
        """harbor drops ``exception.txt`` next to a trial that died before the
        agent ever wrote a rollout (e.g. apt install network failure, docker
        proxy queue rejection). Without this check the dashboard would sit at
        ``discovered`` forever."""
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
                self.state.add_error(
                    ErrorRecord(
                        trial_id=self.trial_id,
                        run_id=None,
                        ts=candidate.stat().st_mtime if candidate.exists() else 0.0,
                        rule_id="harbor_setup_crash",
                        snippet=(snippet.splitlines()[-1] if snippet else "(see exception.txt)"),
                    )
                )
                return

    def _maybe_load_reward(self) -> None:
        """Walk up from the root rollout file looking for a sibling ``verifier/``
        directory. Harbor emits ``reward.txt`` (a single float) by default;
        older paths emit ``reward.json`` (dict or float). Identical contract
        to KimiSessionTail._maybe_load_reward.

        Idempotent across ticks: once the trial is terminal (completed /
        failed / crashed), this is a no-op. Without that guard the tail
        re-reads reward.txt on every poll and re-publishes
        ``set_trial_reward`` (which has no internal dedupe), spamming
        SSE subscribers with one ``trial_update`` per tick until the
        monitor process exits.
        """
        trial = self.state.trials.get(self.trial_id)
        if trial is not None and trial.status in _TERMINAL_STATUSES:
            return
        if self._reward_path is None:
            if self._root_rollout is None:
                return
            cursor: Path | None = self._root_rollout.parent
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
        """Pick up harbor's per-trial ``result.json`` as the canonical
        "trial ended" signal. Mirrors KimiSessionTail._maybe_finalize_from_result_json."""
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
