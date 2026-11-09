"""Read the Workflow-tool sidecar that names each workflow agent.

The ``Workflow`` tool (raw CLI, ultracode-gated) runs its orchestration
script's ``agent()`` calls as headless agents. Each one gets its own
transcript, but those transcripts carry ``isSidechain: true`` and a per-agent
``agentId`` exactly like Task-tool spawns, so
:class:`~aos_bench.monitor.tail.claude.ClaudeSessionTail` decodes them through
its ordinary subagent routing and they land on normal subagent lanes.

What the transcripts do *not* carry is **identity**. A Task subagent is named
by ``agent-<id>.meta.json``'s ``description``; a workflow agent's meta.json
holds only ``{"agentType": "workflow-subagent", "spawnDepth": N}``. The
``label``/``phase`` passed to ``agent()`` live in the workflow runtime, which
writes them to a per-run sidecar beside the transcripts::

    <session-uuid>/
        subagents/workflows/wf_<runid>/
            journal.jsonl            # {type: started|result, agentId, ...}
            agent-<agentId>.jsonl    # ordinary CC transcript
            agent-<agentId>.meta.json
        workflows/
            wf_<runid>.json          # ← the sidecar this module reads
            scripts/<name>-wf_<runid>.js

``wf_<runid>.json`` carries ``workflowName``, ``phases`` and a
``workflowProgress`` list interleaving ``workflow_phase`` entries with
``workflow_agent`` entries — the latter giving, per ``agentId``: ``label``,
``phaseTitle``, ``model``, ``state`` and running ``toolCalls``/``tokens``
counters. In an AOS-Bench stream trial the ``label`` is the **task id** the
lead dispatched, which is exactly what a lane should be called.

The sidecar is treated as *best-effort enrichment*, never a precondition: it
is written by a separate code path from the transcripts and may lag them or be
absent entirely (older builds, killed runs). Lanes always appear from the
transcript alone; this index only upgrades their labels when it can.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.claude_parser import IncrementalClaudeParser
from aos_bench.monitor.tail.kimi import _FileTail

# Workflow agent transcripts under a scan root, at any depth. Depth varies by
# harness: the native claude-code adapter mounts CLAUDE_CONFIG_DIR at
# ``agent/sessions`` (tree at ``sessions/projects/<slug>/<uuid>/subagents/...``)
# while the SDK team harness nests one level deeper at
# ``agent/sessions/<role>/projects/...`` — ``**`` covers both.
_WF_AGENT_GLOB = "**/subagents/workflows/wf_*/agent-*.jsonl"
_WF_JOURNAL = "journal.jsonl"

# Terminal ``state`` values in a workflowProgress entry → monitor run status.
_AGENT_STATE_STATUS = {
    "done": "completed",
    "error": "failed",
    "failed": "failed",
    "skipped": "failed",
}

# Terminal top-level ``status`` of a whole Workflow invocation.
_WORKFLOW_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
    "aborted": "failed",
    "killed": "failed",
}


class WorkflowAgentInfo:
    """What the sidecar knows about one ``agent()`` call."""

    __slots__ = ("agent_id", "label", "phase", "model", "state", "workflow_name", "wf_run")

    def __init__(
        self,
        *,
        agent_id: str,
        label: str | None,
        phase: str | None,
        model: str | None,
        state: str | None,
        workflow_name: str | None,
        wf_run: str | None,
    ) -> None:
        self.agent_id = agent_id
        self.label = label
        self.phase = phase
        self.model = model
        self.state = state
        self.workflow_name = workflow_name
        self.wf_run = wf_run

    @property
    def status(self) -> str | None:
        """Monitor run status implied by the sidecar's ``state``, if terminal."""
        return _AGENT_STATE_STATUS.get((self.state or "").lower())

    def lane_label(self) -> str | None:
        """Lane label: ``wf <phase>/<label>``, or ``wf <label>``.

        The phase is included only when the workflow declares more than the
        implicit single phase, so simple one-phase fan-outs (the common shape
        for a task-pool batch) stay uncluttered.
        """
        if not self.label:
            return None
        if self.phase:
            return f"wf {self.phase}/{self.label}"
        return f"wf {self.label}"


class WorkflowSidecarIndex:
    """agentId → :class:`WorkflowAgentInfo`, refreshed from ``wf_*.json``.

    Cheap to poll: each sidecar is re-parsed only when its mtime/size moves,
    so this can run on every tail tick.
    """

    def __init__(self) -> None:
        self._agents: dict[str, WorkflowAgentInfo] = {}
        # sidecar path → (mtime, size) at last successful parse.
        self._stamps: dict[Path, tuple[float, int]] = {}
        # wf_<runid> → workflow display name.
        self._workflow_names: dict[str, str] = {}
        # wf_<runid> → run status ("completed"/"failed") once terminal.
        self._workflow_status: dict[str, str] = {}
        # wf_<runid> → workflow start, epoch seconds.
        self._workflow_start: dict[str, float] = {}

    def get(self, agent_id: str) -> WorkflowAgentInfo | None:
        return self._agents.get(agent_id)

    def workflow_name(self, wf_run: str) -> str | None:
        return self._workflow_names.get(wf_run)

    def workflow_status(self, wf_run: str) -> str | None:
        return self._workflow_status.get(wf_run)

    def workflow_start(self, wf_run: str) -> float | None:
        return self._workflow_start.get(wf_run)

    def refresh(self, session_dir: Path) -> None:
        """Re-scan ``**/workflows/wf_*.json`` under ``session_dir``.

        Never raises: a malformed or half-written sidecar just leaves the
        previous (or empty) knowledge in place until the next tick.
        """
        for sidecar in session_dir.rglob("workflows/wf_*.json"):
            try:
                st = sidecar.stat()
            except OSError:
                continue
            stamp = (st.st_mtime, st.st_size)
            if self._stamps.get(sidecar) == stamp:
                continue
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Half-written flush; retry next tick (stamp not recorded).
                continue
            if not isinstance(data, dict):
                self._stamps[sidecar] = stamp
                continue
            self._stamps[sidecar] = stamp
            self._ingest(data, wf_run=sidecar.stem)

    # ── internals ──

    def _ingest(self, data: dict, *, wf_run: str) -> None:
        workflow_name = str(data.get("workflowName") or "").strip() or None
        if workflow_name:
            self._workflow_names[wf_run] = workflow_name
        status = _WORKFLOW_STATUS.get(str(data.get("status") or "").lower())
        if status:
            self._workflow_status[wf_run] = status
        # startTime is epoch milliseconds; the monitor works in seconds.
        start = data.get("startTime")
        if isinstance(start, (int, float)) and start > 0:
            self._workflow_start[wf_run] = start / 1000.0
        progress = data.get("workflowProgress")
        if not isinstance(progress, list):
            return
        # Only label lanes with a phase when the workflow really has several;
        # a single-phase script would otherwise repeat one word on every lane.
        n_phases = sum(
            1
            for e in progress
            if isinstance(e, dict) and e.get("type") == "workflow_phase"
        )
        for entry in progress:
            if not isinstance(entry, dict) or entry.get("type") != "workflow_agent":
                continue
            agent_id = entry.get("agentId")
            if not isinstance(agent_id, str) or not agent_id:
                continue
            phase = entry.get("phaseTitle") if n_phases > 1 else None
            self._agents[agent_id] = WorkflowAgentInfo(
                agent_id=agent_id,
                label=_clean(entry.get("label")),
                phase=_clean(phase),
                model=_clean(entry.get("model")),
                state=_clean(entry.get("state")),
                workflow_name=workflow_name,
                wf_run=wf_run,
            )


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def _first_user_prompt(jsonl: Path) -> str | None:
    """First user-message text in a transcript, collapsed to one line.

    The fallback lane label for a workflow agent, used until the sidecar lands
    (or forever, if the Workflow never completes). Only the head of the file is
    read, so this is cheap enough to retry every tick.
    """
    try:
        with jsonl.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(8):
                line = fh.readline()
                if not line:
                    return None
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user":
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = next(
                        (
                            b.get("text")
                            for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ),
                        None,
                    )
                if isinstance(content, str) and content.strip():
                    return " ".join(content.split())
    except OSError:
        return None
    return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class WorkflowFanoutTracker:
    """Render Workflow-tool fan-out as subagent lanes, for any host tail.

    Both agent arms can drive the ``Workflow`` tool, but their transcripts sit
    at different depths and are reached by different tails:

    * native ``claude-code`` — :class:`~aos_bench.monitor.tail.claude.ClaudeSessionTail`
    * SDK team harness — :class:`~aos_bench.monitor.tail.agent_events.AgentEventsTail`,
      which reads the SDK's ``events.jsonl`` and never looks at the CC session
      tree at all

    So the logic lives here once and each tail embeds it in its ``_tick``,
    passing the roots to scan and the lane the fan-out should hang under.
    Workflow agents become ordinary ``run_sub_<agentId>`` lanes — the same run
    id scheme as Task-tool spawns — grouped under one ``run_wfrun_<wf_id>``
    lane per ``Workflow`` invocation.
    """

    def __init__(
        self,
        *,
        trial_id: str,
        state: MonitorState,
        error_detector: ErrorDetector,
        parent_run_id: str,
        rescan_interval_s: float = 10.0,
    ) -> None:
        self.trial_id = trial_id
        self.state = state
        self.error_detector = error_detector
        self.parent_run_id = parent_run_id
        self.rescan_interval_s = rescan_interval_s
        self._index = WorkflowSidecarIndex()
        self._tails: dict[Path, _FileTail] = {}
        self._parsers: dict[Path, IncrementalClaudeParser] = {}
        self._run_ids: dict[Path, str] = {}
        self._agent_runs: dict[str, str] = {}
        self._wf_of_agent: dict[str, str] = {}
        self._wf_dirs: dict[str, Path] = {}
        self._wf_lanes: dict[str, str] = {}
        self._labels: dict[str, str] = {}
        self._relabelled: set[str] = set()
        self._journal_tails: dict[Path, _FileTail] = {}
        self._pending_results: set[str] = set()
        self._last_rescan = 0.0

    def tick(self, scan_roots: list[Path]) -> None:
        """Poll once. Safe to call every tick; never raises."""
        roots = [r for r in scan_roots if r is not None and r.is_dir()]
        if not roots:
            return
        # Sidecar identity is refreshed EVERY tick, not on the rescan
        # interval: it is the only source of real labels/phases/terminal state
        # and it appears all at once when the Workflow completes, so gating it
        # would leave every lane mislabelled and stuck "running" for up to a
        # full interval after the run visibly finished. Re-parsing is
        # mtime-gated inside the index, so the cost is one directory walk.
        for root in roots:
            self._index.refresh(root)
        now = time.monotonic()
        if not self._tails or now - self._last_rescan >= self.rescan_interval_s:
            self._last_rescan = now
            for root in roots:
                self._discover(root)
        for path, tail in self._tails.items():
            records = tail.read_new_records_claude()
            if not records:
                continue
            run_id = self._ensure_run(path)
            events = self._parsers[path].feed(records)
            if not events:
                continue
            self.state.push_events(self.trial_id, run_id, events)
            self.error_detector.scan(
                trial_id=self.trial_id, run_id=run_id, events=events
            )
        self._apply_sidecar()
        self._scan_journals()

    # ── internals ──

    def _discover(self, root: Path) -> None:
        for jsonl in sorted(root.glob(_WF_AGENT_GLOB)):
            if jsonl in self._tails:
                continue
            agent_id = jsonl.stem.removeprefix("agent-")
            wf_run = jsonl.parent.name
            self._tails[jsonl] = _FileTail(jsonl)
            self._parsers[jsonl] = IncrementalClaudeParser(
                run_id=f"run_sub_{agent_id}"
            )
            self._run_ids[jsonl] = f"run_sub_{agent_id}"
            self._wf_of_agent[agent_id] = wf_run
            self._wf_dirs.setdefault(wf_run, jsonl.parent)

    def _label_for(self, jsonl: Path, agent_id: str, wf_run: str) -> str:
        info = self._index.get(agent_id)
        if info is not None and (label := info.lane_label()):
            return label
        if agent_id not in self._labels:
            prompt = _first_user_prompt(jsonl)
            if prompt:
                # Identity first: sibling agents in one Workflow often share a
                # prompt preamble, so a prompt-only label renders N identical
                # lanes. Stored long — the frontend truncates and shows the
                # whole string on hover.
                short = wf_run.removeprefix("wf_")[:4]
                self._labels[agent_id] = f"wf {short}/{agent_id[:5]} {prompt}"[:120]
        return self._labels.get(agent_id, f"wf {agent_id[:14]}")

    def _ensure_run(self, jsonl: Path) -> str:
        run_id = self._run_ids[jsonl]
        agent_id = jsonl.stem.removeprefix("agent-")
        if agent_id in self._agent_runs:
            return run_id
        wf_run = self._wf_of_agent[agent_id]
        info = self._index.get(agent_id)
        self.state.ensure_run(
            self.trial_id,
            run_id,
            role="subagent",
            agent_name=self._label_for(jsonl, agent_id, wf_run),
            parent_run_id=self._ensure_wf_lane(wf_run),
            model_name=info.model if info else None,
        )
        self.state.update_run_status(self.trial_id, run_id, "running")
        self._agent_runs[agent_id] = run_id
        return run_id

    def _ensure_wf_lane(self, wf_run: str) -> str:
        """Lane standing for one ``Workflow`` invocation; parent of its agents."""
        run_id = self._wf_lanes.get(wf_run)
        if run_id is not None:
            return run_id
        run_id = f"run_wfrun_{wf_run}"
        self._wf_lanes[wf_run] = run_id
        name = self._index.workflow_name(wf_run) or wf_run
        self.state.ensure_run(
            self.trial_id,
            run_id,
            role="subagent",
            agent_name=f"workflow {name}",
            parent_run_id=self.parent_run_id,
        )
        self.state.update_run_status(self.trial_id, run_id, "running")
        # This lane has no transcript, so without a timestamped event it would
        # have no started_at — and the frontend anchors the parent→child spawn
        # arrow on started_at. The sidecar's startTime is exact but only exists
        # once the Workflow finished, so fall back to the wf_ dir's mtime.
        started = self._index.workflow_start(wf_run)
        if started is None:
            started = _mtime(self._wf_dirs.get(wf_run) or Path("."))
        self.state.push_events(
            self.trial_id,
            run_id,
            [
                {
                    "event_id": f"{run_id}_evt_0000",
                    "seq": 0,
                    "timestamp": started,
                    "kind": "status",
                    "phase": "update",
                    "step_id": None,
                    "parent_event_id": None,
                    "payload": {"workflow_run": wf_run, "workflow_name": name},
                    "metrics": {},
                    "raw_ref": {"source": "workflow-sidecar"},
                    "extra": {},
                }
            ],
        )
        return run_id

    def _apply_sidecar(self) -> None:
        """Fold late-arriving sidecar identity into already-created lanes."""
        for agent_id, run_id in self._agent_runs.items():
            if agent_id in self._relabelled:
                continue
            info = self._index.get(agent_id)
            if info is None:
                continue
            if label := info.lane_label():
                self.state.update_run_meta(
                    self.trial_id, run_id, agent_name=label, model_name=info.model
                )
                self._relabelled.add(agent_id)
            if info.status:
                self.state.update_run_status(self.trial_id, run_id, info.status)
        for wf_run, run_id in self._wf_lanes.items():
            if name := self._index.workflow_name(wf_run):
                self.state.update_run_meta(
                    self.trial_id, run_id, agent_name=f"workflow {name}"
                )
            if status := self._index.workflow_status(wf_run):
                self.state.update_run_status(self.trial_id, run_id, status)

    def _scan_journals(self) -> None:
        """Fold each workflow's ``journal.jsonl`` into its agents' run status.

        The journal is the only place the runtime records that an ``agent()``
        call *finished* when no sidecar has been written yet: a transcript
        alone just stops growing. Without it every lane sits at "running".
        """
        for wf_run, wf_dir in self._wf_dirs.items():
            journal = wf_dir / _WF_JOURNAL
            tail = self._journal_tails.get(journal)
            if tail is None:
                if not journal.is_file():
                    continue
                tail = _FileTail(journal)
                self._journal_tails[journal] = tail
            for rec in tail.read_new_records_claude():
                agent_id = rec.get("agentId")
                if rec.get("type") != "result" or not agent_id:
                    continue
                # A result can be journalled before its transcript is picked
                # up. _FileTail only moves forward, so park it and retry.
                self._pending_results.add(agent_id)
        for agent_id in list(self._pending_results):
            run_id = self._agent_runs.get(agent_id)
            if run_id is not None:
                self.state.update_run_status(self.trial_id, run_id, "completed")
                self._pending_results.discard(agent_id)
