"""Live tail of one kimi-cli session directory.

Given a trial's ``kimi-share/`` mount, ``KimiSessionTail`` finds the most
recent session directory (per harbor convention, one trial = one container =
one session), tails the root ``wire.jsonl``, watches ``subagents/`` for new
agent_id subdirs as they are created, and spawns inner tails per subagent.

Tail is poll-based (file size mtime check every ``interval``), not inotify —
keeps it portable and avoids the inotify-on-fuse / inotify-on-network-fs
quirks. The cost is at most ``interval`` seconds of UI lag.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.parser import IncrementalWireParser, parse_wire_line
from aos_bench.monitor.tasks import extract_task_lifecycle_from_events

# Match harbor's constant; kept inline to avoid importing harbor.
SHARE_DIRNAME = "kimi-share"


def _find_latest_session_dir(share_dir: Path) -> Path | None:
    """Mirror of ``find_kimi_cli_latest_session_dir`` but tolerant of an
    unpopulated share dir (returns None instead of raising)."""
    sessions_root = share_dir / "sessions"
    if not sessions_root.is_dir():
        return None
    # share_dir has one subdir per work_dir_key (md5 of work dir). For our
    # harbor setup there is exactly one. Pick the newest by mtime if multiple.
    work_dirs = [p for p in sessions_root.iterdir() if p.is_dir()]
    if not work_dirs:
        return None
    work_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for work_dir in work_dirs:
        sessions = [p for p in work_dir.iterdir() if p.is_dir()]
        if sessions:
            sessions.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return sessions[0]
    return None


def _find_share_dir(trial_dir: Path) -> Path | None:
    """Walk down from trial_dir to find the kimi-share dir.

    harbor lays out trial dirs as
    ``<trial_dir>/<trial_dir.name>/<trial_subdir>/agent/kimi-share``.
    The intermediate names are not stable so we search.
    """
    # Common shape first, then fallback to glob.
    candidates = list(trial_dir.glob(f"**/{SHARE_DIRNAME}"))
    if candidates:
        # Prefer shallowest.
        candidates.sort(key=lambda p: len(p.parts))
        return candidates[0]
    return None


class _FileTail:
    """Tail one wire.jsonl file in line-buffered fashion across polls.

    We open() on each poll because the underlying file may be on a cloud-mounted
    disk where the fd-based approach is finicky. We track the byte offset and
    seek to it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.buffer = ""  # partial last line carried across polls

    def _read_new_lines(self) -> list[str]:
        """Return complete lines appended since the last poll (parser-agnostic).

        Handles the byte-offset tracking and the partial-last-line carry; the
        IO + rotation logic is identical regardless of which on-disk format
        (kimi wire.jsonl vs Claude Code transcript) we are tailing.
        """
        if not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            # File truncated / rotated. Restart from beginning.
            self.offset = 0
            self.buffer = ""
        if size == self.offset:
            return []
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
        except OSError:
            return []
        data = self.buffer + chunk
        # Split on newlines; the last fragment may be partial.
        lines = data.split("\n")
        self.buffer = lines.pop() if lines and not data.endswith("\n") else ""
        return lines

    def read_new_records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for line in self._read_new_lines():
            parsed = parse_wire_line(line)
            if parsed is not None:
                records.append(parsed)
        return records

    def read_new_records_claude(self) -> list[dict[str, object]]:
        """Same byte tracking as :meth:`read_new_records`, but parse each line
        as a Claude Code transcript record instead of a kimi wire record."""
        from aos_bench.monitor.tail.claude_parser import parse_claude_line

        records: list[dict[str, object]] = []
        for line in self._read_new_lines():
            parsed = parse_claude_line(line)
            if parsed is not None:
                records.append(parsed)
        return records

    def read_new_records_codex(self) -> list[dict[str, object]]:
        """Same byte tracking as :meth:`read_new_records`, but parse each line
        as a codex ``rollout-*.jsonl`` record."""
        from aos_bench.monitor.tail.codex_parser import parse_codex_line

        records: list[dict[str, object]] = []
        for line in self._read_new_lines():
            parsed = parse_codex_line(line)
            if parsed is not None:
                records.append(parsed)
        return records

    def read_new_records_kimi_code(self) -> list[dict[str, object]]:
        """Same byte tracking as :meth:`read_new_records`, but parse each line
        as a kimi-code ``agents/<id>/wire.jsonl`` record.

        Note the difference from :meth:`read_new_records`: the kimi-code line
        parser keeps the ``metadata`` header (it carries ``protocol_version`` —
        the legacy-vs-default engine tell), and ``IncrementalKimiCodeParser``
        consumes it as run metadata instead of emitting an event."""
        from aos_bench.monitor.tail.kimi_code_parser import parse_kimi_code_line

        records: list[dict[str, object]] = []
        for line in self._read_new_lines():
            parsed = parse_kimi_code_line(line)
            if parsed is not None:
                records.append(parsed)
        return records


class KimiSessionTail:
    """Async tailer for one trial's kimi-cli session."""

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
        self._root_tail: _FileTail | None = None
        self._root_parser: IncrementalWireParser | None = None
        self._sub_tails: dict[str, tuple[_FileTail, IncrementalWireParser]] = {}
        # Try to recover trial reward from verifier output if present.
        self._reward_path: Path | None = None
        # Running accumulator of ALL root events seen so far — for task
        # lifecycle extraction. events_tail in state is capped (default 150)
        # and would lose earlier todo_list responses on long-running trials.
        self._all_root_events: list[dict] = []

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name=f"tail-{self.trial_id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        # Register trial up-front so dashboard sees it even before first event.
        self.state.ensure_trial(self.trial_id, str(self.trial_dir))
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                # Don't let a per-trial fault kill the whole monitor.
                self.state.set_trial_status(self.trial_id, f"tail_error: {exc!r}")
            trial = self.state.trials.get(self.trial_id)
            if trial is not None and trial.status in {"completed", "failed", "crashed"}:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue

    def _tick(self) -> None:
        if self._session_dir is None:
            # Before searching for the share dir, check whether harbor has
            # already declared this trial dead. If the agent never started
            # writing wire.jsonl (e.g. uv install kimi-cli failed during
            # setup), harbor drops exception.txt + result.json and exits.
            # Without this check the trial sits at "discovered" forever.
            self._maybe_detect_crashed()
            # Also pick up result.json signals (covers the rarer case
            # where harbor wrote result.json before kimi-share appeared,
            # e.g. agent crash mid-init).
            self._maybe_finalize_from_result_json()
            share = _find_share_dir(self.trial_dir)
            if share is None:
                return
            sess = _find_latest_session_dir(share)
            if sess is None:
                return
            self._session_dir = sess
            root_wire = sess / "wire.jsonl"
            self._root_tail = _FileTail(root_wire)
            self._root_parser = IncrementalWireParser(
                run_id="run_root_001", synthesize_subagent_spawns=True
            )
            self.state.ensure_run(
                self.trial_id, "run_root_001", role="root", agent_name="orchestrator"
            )
            self.state.set_trial_status(self.trial_id, "running")

        assert self._session_dir is not None
        assert self._root_tail is not None
        assert self._root_parser is not None

        # Root events
        root_records = self._root_tail.read_new_records()
        if root_records:
            events = self._root_parser.feed(root_records)
            self.state.push_events(self.trial_id, "run_root_001", events)
            self.error_detector.scan(
                trial_id=self.trial_id, run_id="run_root_001", events=events
            )
            # Maintain a separate uncapped buffer of root events so
            # extract_task_lifecycle_from_events can always see the latest
            # todo_list response, even after events_tail has rotated past it.
            self._all_root_events.extend(events)
            # Bound the accumulator at a generous size (10k events) to keep
            # memory in check while still preserving the most recent todo_list.
            if len(self._all_root_events) > 10000:
                self._all_root_events = self._all_root_events[-8000:]
            snapshot = extract_task_lifecycle_from_events(self._all_root_events)
            if snapshot:
                self.state.set_trial_task_snapshot(self.trial_id, snapshot)

        # Discover new subagents
        sub_dir = self._session_dir / "subagents"
        if sub_dir.is_dir():
            for entry in sub_dir.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name in self._sub_tails:
                    pass  # already tailing
                else:
                    self._register_subagent(entry)

        # Pump subagent tails
        for agent_id, (tail, parser) in self._sub_tails.items():
            recs = tail.read_new_records()
            if not recs:
                continue
            events = parser.feed(recs)
            run_id = f"run_sub_{agent_id}"
            self.state.push_events(self.trial_id, run_id, events)
            self.error_detector.scan(
                trial_id=self.trial_id, run_id=run_id, events=events
            )

        # Refresh subagent metadata (status) periodically
        self._refresh_sub_meta(sub_dir)

        # Pick up reward.json if present
        self._maybe_load_reward()
        # Final fallback: harbor's result.json is the canonical "trial
        # ended" signal. Picks up cases the reward.json/exception.txt
        # checks miss (e.g. agent timed-out cleanly + verifier ran).
        self._maybe_finalize_from_result_json()

    def _register_subagent(self, sub_entry: Path) -> None:
        agent_id = sub_entry.name
        meta_path = sub_entry / "meta.json"
        meta: dict[str, object] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        run_id = f"run_sub_{agent_id}"
        agent_name = str(meta.get("subagent_type") or agent_id)
        launch_spec = meta.get("launch_spec") if isinstance(meta.get("launch_spec"), dict) else {}
        model_name = launch_spec.get("effective_model") if isinstance(launch_spec, dict) else None
        self.state.ensure_run(
            self.trial_id,
            run_id,
            role="subagent",
            agent_name=agent_name,
            parent_run_id="run_root_001",
            model_name=model_name if isinstance(model_name, str) else None,
        )
        status = str(meta.get("status") or "discovered")
        self.state.update_run_status(self.trial_id, run_id, status)
        wire_path = sub_entry / "wire.jsonl"
        self._sub_tails[agent_id] = (
            _FileTail(wire_path),
            IncrementalWireParser(run_id=run_id),
        )

    def _refresh_sub_meta(self, sub_dir: Path) -> None:
        for agent_id in list(self._sub_tails.keys()):
            meta_path = sub_dir / agent_id / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = meta.get("status")
            if isinstance(status, str):
                self.state.update_run_status(self.trial_id, f"run_sub_{agent_id}", status)

    def _maybe_detect_crashed(self) -> None:
        """Mark trial as ``crashed`` if harbor dropped an exception.txt before
        kimi-cli ever wrote a wire.jsonl. Common cause: ``uv tool install
        kimi-cli`` (agent setup) failed → no kimi-share/ ever materialised
        → KimiSessionTail would otherwise sit at status=discovered forever.
        Idempotent: only flips status once."""
        trial = self.state.trials.get(self.trial_id)
        if trial is None or trial.status not in ("discovered",):
            return
        # exception.txt can be at the trial dir directly (most common —
        # harbor writes it as <trial>/exception.txt) OR 1-2 levels deep
        # under legacy batch layouts. Check all three.
        candidates = [
            self.trial_dir / "exception.txt",
            *self.trial_dir.glob("*/exception.txt"),
            *self.trial_dir.glob("*/*/exception.txt"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                # Read a short snippet to surface as a one-line summary on
                # the trial row so the user knows why.
                snippet = ""
                try:
                    snippet = candidate.read_text(encoding="utf-8", errors="replace")[:400]
                except OSError:
                    pass
                self.state.set_trial_status(self.trial_id, "crashed")
                # Surface in errors panel too.
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
            # Walk up from the session dir looking for a sibling
            # ``verifier/`` directory. Harbor's stream verifier emits
            # ``reward.txt`` (one float) by default; older paths may
            # emit ``reward.json``. We accept either.
            assert self._session_dir is not None
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
        text = ""
        try:
            text = self._reward_path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not text:
            return
        # reward.txt: a single float. reward.json: a dict of named rewards
        # or a plain number.
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
        """Read harbor's per-trial ``result.json`` as the canonical
        "trial ended" signal. Covers cases the reward.txt + exception.txt
        paths miss:

          - reward.txt absent but verifier_result is set in result.json
          - exception_info present AND verifier_result is None →
            harbor died before verifier ran → mark ``failed``
          - exception_info present BUT verifier_result is also set →
            agent timed out cleanly + verifier still graded → mark
            ``completed`` with reward (this is the common case for
            timeout-bounded streams)

        Idempotent: once status is terminal we stop. result.json appears
        at one of:
          <trial_dir>/result.json                  (depth 0, default)
          <trial_dir>/<job>/result.json            (depth 1)
          <trial_dir>/<job>/<sub>/result.json      (depth 2, batch dirs)
        """
        trial = self.state.trials.get(self.trial_id)
        if trial is None:
            return
        # Already terminal — don't keep re-reading.
        if trial.status in ("completed", "failed", "crashed"):
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
        # Reward extraction: verifier_result.rewards is a dict like
        # {"reward": 1.0}. Average if multiple keys present.
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
        # No verifier output → harbor never reached verifier → failed.
        if exception_info:
            self.state.set_trial_status(self.trial_id, "failed")
            return
        # result.json exists but no verifier_result and no exception_info
        # — atypical, treat as terminal-unknown so we stop polling.
        self.state.set_trial_status(self.trial_id, "completed")
