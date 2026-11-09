"""Scan a harbor jobs directory for kimi-cli / kimi-code / claude-code / codex trials.

A trial dir is any immediate child of ``jobs_dir`` that has an ``agent/``
mount somewhere within it. The directory naming convention varies:

  - Per-job mode: ``<jobs_dir>/<stream-name>__<short-hash>/``
    (when harbor's ``--jobs-dir`` was set to the per-job dir directly,
    e.g. monitor_smoke.sh).
  - Per-batch mode: ``<jobs_dir>/<agent>__<stream>__<mode>__att<N>/``
    (when the batch launcher named each job after the agent).

Each ``*SessionTail`` is responsible for walking down to find its session
files. The discoverer runs as a periodic asyncio loop and is idempotent —
registering the same trial twice is a no-op.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.resources import TrialContainerSampler
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.agent_events import AgentEventsTail
from aos_bench.monitor.tail.claude import ClaudeSessionTail
from aos_bench.monitor.tail.codex import CodexSessionTail
from aos_bench.monitor.tail.kimi import KimiSessionTail
from aos_bench.monitor.tail.kimi_code import HOME_DIRNAME, KimiCodeSessionTail

# Union type for the session tails — they share the start/stop interface the
# discoverer relies on (duck-typed; no shared base needed).
SessionTail = (
    KimiSessionTail
    | ClaudeSessionTail
    | AgentEventsTail
    | CodexSessionTail
    | KimiCodeSessionTail
)


def _looks_like_trial(trial_dir: Path) -> bool:
    """Return True iff ``trial_dir`` is itself a per-trial dir (i.e.
    has an ``agent/`` mount directly inside, not nested multiple levels down).

    Both kimi-cli and Claude Code trials mount the agent's writable area at
    ``<trial_dir>/agent/`` (kimi → ``agent/kimi-share``, Claude Code →
    ``agent/sessions``). ``agent/`` is the common marker.

    Why strict: when ``jobs_dir`` is a multi-phase root like
    ``EXP_ROOT/`` containing
    ``EXP_ROOT/<phase>/<source>/<job>/<trial>/agent/...``, the
    old liberal-glob check would classify the *phase* dir as one trial
    and bury 100+ real trials underneath. The recursive walker in
    ``_iter_trial_dirs`` finds each real trial separately.
    """
    return (trial_dir / "agent").is_dir()


def _classify_trial_agent(trial_dir: Path) -> str | None:
    """Classify a trial dir by which agent's trace it carries, or ``None`` if
    no trace has materialised yet (caller should defer building the tail).

    Detection is by on-disk trace layout — each signal mirrors what the
    corresponding tail consumes:

      - ``"claude-agent-sdk"`` — our SDK harness (solo / team / workflow) writes
        the ``Coordinator`` log at ``agent/events.jsonl``. Checked FIRST: the
        SDK also creates an (empty) ``agent/sessions/<role>/projects/`` from its
        ``CLAUDE_CONFIG_DIR``, so the CC check must not win on that empty dir.
      - ``"claude-code"`` — harbor's native Claude Code adapter lets Claude Code
        write a real transcript at ``agent/sessions/projects/<p>/<sess>/*.jsonl``.
        We require an actual ``*.jsonl`` (not just the dir) so an empty SDK
        ``projects/`` doesn't masquerade as claude-code.
      - ``"codex"`` — harbor's codex adapter sets ``CODEX_HOME = agent_dir``,
        so codex drops session logs at
        ``agent/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl``. The
        ``YYYY/MM/DD`` date hierarchy plus the ``rollout-`` prefix are the
        signature; this can't collide with the claude-code branch (which
        always has ``projects/`` in the path) because we check claude-code
        first.
      - ``"kimi-code"`` — harbor's kimi-code adapter sets
        ``KIMI_CODE_HOME = agent/kimi-code-home``, so the *directory* is the
        marker (not a journal file): the adapter's ``install()`` mkdirs it as
        its very first step, before the node/npm install that can fail, which
        is what lets an install-failure trial still be typed kimi-code so its
        ``exception.txt`` gets reported. Checked BEFORE
        kimi-cli purely for symmetry of "most specific first"; the two markers
        cannot collide (``kimi-share`` vs ``kimi-code-home``).
      - ``"kimi-cli"`` — kimi-cli writes ``agent/kimi-share/sessions/``.

    Returning ``None`` (rather than defaulting to kimi) is deliberate: an SDK
    trial sits at ``discovered`` for a few seconds before ``events.jsonl``
    appears, and the old kimi-default would lock it onto a tail that can never
    find a kimi-share. Deferring until a positive signal keeps the trial
    correctly typed.
    """
    agent_dir = trial_dir / "agent"
    if not agent_dir.is_dir():
        return None
    # 1) SDK harness — events.jsonl is the strongest, earliest signal.
    if (agent_dir / "events.jsonl").is_file() or list(agent_dir.glob("**/events.jsonl")):
        return "claude-agent-sdk"
    # 2) Native Claude Code — require a real transcript .jsonl under projects/,
    #    excluding subagents/ (mirrors harbor's session-dir discovery).
    for jsonl in agent_dir.glob("**/projects/**/*.jsonl"):
        if "subagents" not in jsonl.parts:
            return "claude-code"
    # 3) Codex — agent/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl.
    if list(
        agent_dir.glob(
            "**/sessions/[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/rollout-*.jsonl"
        )
    ):
        return "codex"
    # 4) kimi-code — the KIMI_CODE_HOME dir itself, no session file required.
    #    ``is_dir()`` on the glob hits too, so a stray *file* of that name (a
    #    log, a tarball) can't type the trial onto a tail that will never find a
    #    session — ``_find_home_dir`` applies the same filter.
    if (agent_dir / HOME_DIRNAME).is_dir() or any(
        p.is_dir() for p in agent_dir.glob(f"**/{HOME_DIRNAME}")
    ):
        return "kimi-code"
    # 5) kimi-cli.
    if (agent_dir / "kimi-share").is_dir() or list(agent_dir.glob("**/kimi-share")):
        return "kimi-cli"
    return None


_MAX_TRIAL_DEPTH = 6
"""Cap recursive search at this many levels under jobs_dir. The deepest
layout in practice is 4 (phase/source/job/trial); 6 leaves headroom for
future batch-of-batch layouts without scanning unbounded subtrees.
"""


def _iter_trial_dirs(root: Path) -> list[Path]:
    """DFS walk from ``root``; yield every dir that
    ``_looks_like_trial`` accepts. Stops recursing into a subtree
    once a trial dir is found (the trial owns everything under it).
    """
    out: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if depth > _MAX_TRIAL_DEPTH:
            return
        if _looks_like_trial(d):
            out.append(d)
            return
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for child in entries:
            if child.is_dir():
                walk(child, depth + 1)

    if not root.is_dir():
        return out
    for child in root.iterdir():
        if child.is_dir():
            walk(child, 1)
    return out


def _trial_identity(trial_dir: Path, detected_agent: str | None) -> dict[str, str]:
    """Recover canonical naming fields from Harbor's per-trial config.

    Canonical monitor views are often symlinks with lossy aliases such as
    ``claude-glm52``. The resolved trial config is authoritative and contains
    the full harness, model and stream path.
    """
    identity: dict[str, str] = {}
    try:
        native_dir = trial_dir.resolve()
    except OSError:
        native_dir = trial_dir
    try:
        data: dict[str, Any] = json.loads(
            (native_dir / "config.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, TypeError):
        data = {}

    agent = data.get("agent")
    if isinstance(agent, dict):
        harness = str(agent.get("name") or "").strip()
        model = str(agent.get("model_name") or "").strip()
        if harness:
            identity["harness_name"] = harness
        if model:
            identity["model_name"] = model
    if "harness_name" not in identity and detected_agent:
        identity["harness_name"] = detected_agent

    task = data.get("task")
    if isinstance(task, dict):
        task_path = str(task.get("path") or "").rstrip("/")
        task_name = str(task.get("name") or "").strip()
        stream = Path(task_path).name if task_path else task_name
        if stream:
            identity["stream_name"] = stream

    native_name = str(data.get("trial_name") or native_dir.name)
    if "stream_name" not in identity:
        stream = native_name.split("__", 1)[0]
        if stream:
            identity["stream_name"] = stream
    if "__" in native_name:
        attempt_id = native_name.rsplit("__", 1)[-1].strip()
        if attempt_id:
            identity["attempt_id"] = attempt_id
    return identity


def _discover_trial_entries(
    root: Path,
    known_agents: dict[str, str],
    known_identities: dict[str, dict[str, str]],
) -> list[tuple[Path, str | None, dict[str, str]]]:
    """Perform cloud-backed discovery off the web server's event loop."""
    found: list[tuple[Path, str | None, dict[str, str]]] = []
    for entry in _iter_trial_dirs(root):
        known = known_agents.get(entry.name)
        agent = known if known and known != "pending" else _classify_trial_agent(entry)
        identity = known_identities.get(entry.name) or _trial_identity(entry, agent)
        found.append((entry, agent, identity))
    return found


class TrialDiscoverer:
    def __init__(
        self,
        *,
        jobs_dir: Path,
        state: MonitorState,
        error_detector: ErrorDetector,
        scan_interval_s: float = 2.0,
        tail_interval_s: float = 1.5,
        container_sample_interval_s: float = 3.0,
    ) -> None:
        self.jobs_dir = Path(jobs_dir)
        self.state = state
        self.error_detector = error_detector
        self.scan_interval_s = scan_interval_s
        self.tail_interval_s = tail_interval_s
        self.container_sample_interval_s = container_sample_interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._tails: dict[str, SessionTail] = {}
        self._samplers: dict[str, TrialContainerSampler] = {}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="trial-discoverer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None
        # Tear down all per-trial tails + container samplers.
        await asyncio.gather(*(t.stop() for t in self._tails.values()), return_exceptions=True)
        await asyncio.gather(*(s.stop() for s in self._samplers.values()), return_exceptions=True)
        self._tails.clear()
        self._samplers.clear()

    async def _loop(self) -> None:
        # Initial scan immediately, then periodic.
        while not self._stop.is_set():
            try:
                await self._scan_once()
            except Exception:
                # Discovery must not crash the monitor.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.scan_interval_s)
            except asyncio.TimeoutError:
                continue

    async def _scan_once(self) -> None:
        if not self.jobs_dir.is_dir():
            return
        # Recursively walk jobs_dir up to _MAX_TRIAL_DEPTH and register
        # every per-trial dir found. Supports both layouts:
        #   1. jobs_dir IS the per-job dir (depth 1: <trial>/)
        #   2. jobs_dir is a multi-phase root (depth 2-6: phase/source/job/<trial>/)
        # ``jobs_dir`` commonly lives on /cloud. Path.is_dir/glob/read_text can
        # block for seconds there, so never execute the scan on FastAPI's event
        # loop; otherwise even /api/health and static assets intermittently hang.
        known_agents = {
            trial_id: trial.agent for trial_id, trial in self.state.trials.items()
        }
        known_identities = {
            trial_id: {
                "harness_name": trial.harness_name,
                "model_name": trial.model_name,
                "stream_name": trial.stream_name,
                "attempt_id": trial.attempt_id,
            }
            for trial_id, trial in self.state.trials.items()
            if trial.harness_name and trial.model_name and trial.stream_name
        }
        entries = await asyncio.to_thread(
            _discover_trial_entries,
            self.jobs_dir,
            known_agents,
            known_identities,
        )
        for entry, agent, identity in entries:
            # trial_id must be unique across the whole scan; use the dir
            # name. Different harbor invocations produce different trial
            # name hashes so collisions on same-task different-attempt
            # don't actually happen, but defensively, qualify by parent.
            trial_id = entry.name

            # Register the trial and start container sampling as soon as the
            # Harbor agent mount exists. Native trajectory files appear only
            # after agent setup, which used to lose the beginning of the
            # container CPU/memory curve.
            self.state.ensure_trial(
                trial_id,
                str(entry),
                agent=agent or "pending",
                **identity,
            )
            if agent is not None:
                self.state.set_trial_agent(trial_id, agent)
            if trial_id not in self._samplers:
                sampler = TrialContainerSampler(
                    trial_id=trial_id,
                    state=self.state,
                    interval_s=self.container_sample_interval_s,
                    trial_dir=entry,
                    agent_kind=agent,
                )
                self._samplers[trial_id] = sampler
                await sampler.start()
            elif agent is not None:
                self._samplers[trial_id].agent_kind = agent

            if trial_id in self._tails:
                continue
            # Dispatch the agent-appropriate tail. All three share the
            # (trial_id, trial_dir, state, error_detector, interval_s)
            # constructor + start/stop interface. If the trial hasn't written
            # any recognisable trace yet, classify returns None — defer until a
            # later scan rather than locking onto the wrong tail.
            if agent is None:
                continue
            tail_cls = {
                "claude-agent-sdk": AgentEventsTail,
                "claude-code": ClaudeSessionTail,
                "codex": CodexSessionTail,
                "kimi-code": KimiCodeSessionTail,
                "kimi-cli": KimiSessionTail,
            }[agent]
            tail: SessionTail = tail_cls(
                trial_id=trial_id,
                trial_dir=entry,
                state=self.state,
                error_detector=self.error_detector,
                interval_s=self.tail_interval_s,
            )
            self._tails[trial_id] = tail
            await tail.start()
