"""In-memory state for the live Eval Monitor.

The monitor holds one ``MonitorState`` for the lifetime of the process. Trial
tailers, the error detector, and the resource sampler all mutate it; the
FastAPI handlers and the SSE pump read from it.

Concurrency model: a single asyncio event loop. Mutations happen from tasks
on the same loop, so simple attribute writes are safe. Pub-sub uses bounded
``asyncio.Queue`` per subscriber — slow consumers drop deltas rather than
blocking producers.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# Caps sized for 5h+ trials. Reasoning:
#   - root agent at peak ~1 event/sec → 5h = 18k events; cap at 20k.
#   - structural skeleton (spawn/turn/compaction/step_begin) is far sparser
#     but always-preserved → cap generously at 10k.
#   - container samples at 3s interval → 5h = 6k samples; cap at 12k.
#   - host samples at 2s → same scale.
# Transport: at ~300 bytes/event JSON after _trim_event_for_api (cap 600 chars/string),
# a single root with 20k events serializes to ~6 MB. The frontend caps rendered
# nodes per lane to keep DOM small even if the data is huge.
RUN_EVENT_TAIL_CAP = 20000
STRUCTURAL_EVENT_CAP = 10000
ERROR_LIST_CAP = 5000
RESOURCE_SAMPLE_CAP = 12000  # ~6.6h at 2s interval (host-wide)
TRIAL_RESOURCE_SAMPLE_CAP = 12000  # ~10h at 3s interval per trial
TRIAL_PROCESS_SAMPLE_CAP = 2400  # ~2h at 3s; full history remains on disk

# These event kinds are dropped from ``events_tail`` to keep it focused on
# orchestrator-visible behavior. They're already rendered elsewhere (the
# subagent's own lane carries its full event stream, so root's bridged
# subagent_event passes are pure duplicates), and they would otherwise
# swamp events_tail on long-running parent agents with many in-flight subs.
_TAIL_DROP_KINDS = frozenset({"subagent_event", "hook"})

# Structural events that mark important boundaries in a run's history.
# These are preserved separately from `events_tail` (which has a fixed cap)
# so that even on a long-running parent agent, the spawn / compaction /
# turn-boundary markers from EARLY in the run are still visible. Without
# this, a root with 300+ events would show the subagent_spawn events
# pushed out of the tail, making it look like subagents started before
# the root did.
_STRUCTURAL_KINDS = frozenset({
    "subagent_spawn", "compaction_begin", "compaction_end",
    "turn_begin", "turn_end",
})

# API-side payload trim: cap any single string field in event payloads at
# this many characters so a trial-detail response stays well under 500 KB
# even with 12 rich lanes × 150 events. Browsers + SSH tunnels choke on
# multi-MB JSON every 1.5s when the user has the Trial Detail tab open.
_API_STR_CAP = 600


def _display_name_component(value: str | None) -> str:
    """Return one filesystem/UI-safe component of a canonical trial name."""
    if not value:
        return ""
    component = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return re.sub(r"-+", "-", component).strip("-")


def _trim_event_for_api(ev: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``ev`` with long strings inside ``payload`` truncated.

    Keeps the event shape identical so the frontend rendering code doesn't
    need to change. Full untruncated content is still on disk (wire.jsonl);
    this is purely a transport-side trim.
    """
    if not isinstance(ev, dict):
        return ev
    payload = ev.get("payload")
    if payload is None:
        return ev
    trimmed_payload = _trim_value(payload, _API_STR_CAP)
    out = dict(ev)
    out["payload"] = trimmed_payload
    return out


def _trim_value(v: Any, cap: int) -> Any:
    if isinstance(v, str):
        return v if len(v) <= cap else v[:cap] + f"…[+{len(v) - cap} chars]"
    if isinstance(v, dict):
        return {k: _trim_value(vv, cap) for k, vv in v.items()}
    if isinstance(v, list):
        # Also cap list length so a giant ContentPart list doesn't slip through.
        capped = v[:20] if len(v) > 20 else v
        return [_trim_value(item, cap) for item in capped]
    return v


@dataclass
class RunSummary:
    """One agent run (root or subagent) inside a trial."""

    run_id: str
    parent_run_id: str | None
    role: str  # "root" | "subagent"
    agent_name: str
    status: str = "running"
    model_name: str | None = None
    started_at: float | None = None
    last_event_at: float | None = None
    n_events: int = 0
    n_tool_calls: int = 0
    n_llm_calls: int = 0  # StepBegin count is a decent proxy
    n_compactions: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    events_tail: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=RUN_EVENT_TAIL_CAP))
    # Structural skeleton: spawn / compaction / turn markers preserved
    # across the full run history so the dashboard can always show the
    # spawn arrows + boundary markers even after events_tail has rolled
    # over them.
    structural_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=STRUCTURAL_EVENT_CAP))

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "role": self.role,
            "agent_name": self.agent_name,
            "status": self.status,
            "model_name": self.model_name,
            "started_at": self.started_at,
            "last_event_at": self.last_event_at,
            "n_events": self.n_events,
            "n_tool_calls": self.n_tool_calls,
            "n_llm_calls": self.n_llm_calls,
            "n_compactions": self.n_compactions,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }

    def to_detail_dict(self) -> dict[str, Any]:
        d = self.to_summary_dict()
        # Merge structural events into the tail so the frontend always sees
        # the run's full skeleton (spawns, compactions, turn boundaries),
        # even after events_tail's rolling cap has dropped the early ones.
        # Dedup by id() since the same dict may be in both deques.
        seen_ids: set[int] = set()
        merged: list[dict[str, Any]] = []
        for ev in self.events_tail:
            seen_ids.add(id(ev))
            merged.append(ev)
        for ev in self.structural_events:
            if id(ev) in seen_ids:
                continue
            merged.append(ev)
        # Sort by timestamp so the timeline renders in chronological order.
        merged.sort(key=lambda e: (e.get("timestamp") if isinstance(e.get("timestamp"), (int, float)) else 0))
        d["events_tail"] = [_trim_event_for_api(ev) for ev in merged]
        return d


@dataclass
class TrialSummary:
    """One harbor trial = one container with one kimi-cli session."""

    trial_id: str
    trial_dir: str
    agent: str = "kimi-cli"
    harness_name: str | None = None
    model_name: str | None = None
    stream_name: str | None = None
    attempt_id: str | None = None
    status: str = "discovered"
    started_at: float = field(default_factory=time.time)
    runs: dict[str, RunSummary] = field(default_factory=dict)
    reward: float | None = None
    errors_count: int = 0
    # Latest dispatcher snapshot from the most recent todo_list ToolResult
    # in the root agent's wire. Stored separately so events_tail capping
    # doesn't lose it.
    latest_task_snapshot: list[dict[str, Any]] | None = None

    @property
    def agent_count(self) -> int:
        return len(self.runs)

    @property
    def llm_calls(self) -> int:
        return sum(r.n_llm_calls for r in self.runs.values())

    @property
    def tool_calls(self) -> int:
        return sum(r.n_tool_calls for r in self.runs.values())

    @property
    def last_event_at(self) -> float | None:
        ts = [r.last_event_at for r in self.runs.values() if r.last_event_at is not None]
        return max(ts) if ts else None

    @property
    def setting_name(self) -> str | None:
        """Canonical ``<harness>-<model>`` setting label, when known."""
        harness = _display_name_component(self.harness_name or self.agent)
        model = _display_name_component(self.model_name)
        if not harness or not model:
            return None
        return f"{harness}-{model}"

    @property
    def display_name(self) -> str:
        """Stable human name without Harbor's random attempt identifier."""
        setting = self.setting_name
        stream = _display_name_component(self.stream_name)
        if setting and stream:
            return f"{setting}-{stream}"
        return self.trial_id

    def to_summary_dict(self) -> dict[str, Any]:
        last = self.last_event_at
        return {
            "trial_id": self.trial_id,
            "trial_dir": self.trial_dir,
            "agent": self.agent,
            "harness_name": self.harness_name,
            "model_name": self.model_name,
            "stream_name": self.stream_name,
            "attempt_id": self.attempt_id,
            "setting_name": self.setting_name,
            "display_name": self.display_name,
            "status": self.status,
            "started_at": self.started_at,
            "agent_count": self.agent_count,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "last_event_age_s": (time.time() - last) if last else None,
            "reward": self.reward,
            "errors_count": self.errors_count,
        }

    def to_detail_dict(self) -> dict[str, Any]:
        d = self.to_summary_dict()
        d["runs"] = [r.to_detail_dict() for r in self.runs.values()]
        return d


@dataclass
class ErrorRecord:
    trial_id: str
    run_id: str | None
    ts: float
    rule_id: str
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResourceSample:
    ts: float
    cpu_percent: float
    mem_percent: float
    mem_used_mb: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContainerSample:
    """One container/cgroup resource reading for a trial's main container.

    The first seven fields are the original Docker stats schema.  Everything
    else is optional so old JSONL logs remain replayable on hosts where a
    particular cgroup v2 controller is unavailable.
    """

    ts: float
    cpu_percent: float
    mem_used_mb: float
    mem_limit_mb: float
    mem_percent: float
    net_rx_mb: float
    net_tx_mb: float
    block_read_mb: float | None = None
    block_write_mb: float | None = None
    pids: int | None = None
    cgroup_mem_current_mb: float | None = None
    cgroup_mem_limit_mb: float | None = None
    memory_peak_mb: float | None = None
    swap_used_mb: float | None = None
    swap_limit_mb: float | None = None
    mem_events_low: int | None = None
    mem_events_high: int | None = None
    mem_events_max: int | None = None
    mem_events_oom: int | None = None
    mem_events_oom_kill: int | None = None
    mem_events_oom_group_kill: int | None = None
    cpu_usage_s: float | None = None
    cpu_user_s: float | None = None
    cpu_system_s: float | None = None
    cpu_nr_periods: int | None = None
    cpu_nr_throttled: int | None = None
    cpu_throttled_s: float | None = None
    cpu_throttled_period_percent: float | None = None
    cpu_quota_us: int | None = None
    cpu_period_us: int | None = None
    io_read_mb: float | None = None
    io_write_mb: float | None = None
    io_read_ops: int | None = None
    io_write_ops: int | None = None
    pids_current: int | None = None
    pids_limit: int | None = None
    pids_events_max: int | None = None
    cpu_pressure_some_avg10: float | None = None
    cpu_pressure_full_avg10: float | None = None
    memory_pressure_some_avg10: float | None = None
    memory_pressure_full_avg10: float | None = None
    io_pressure_some_avg10: float | None = None
    io_pressure_full_avg10: float | None = None
    fs_size_mb: float | None = None
    fs_used_mb: float | None = None
    fs_available_mb: float | None = None
    fs_used_percent: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProcessInfo:
    """One process observed inside a trial container."""

    pid: int
    ppid: int
    cpu_percent: float
    mem_rss_mb: float
    elapsed_s: int
    command: str
    args: str
    run_id: str | None = None
    attribution: str = "unattributed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProcessSnapshot:
    """Process table captured alongside one container resource sample."""

    ts: float
    container_name: str
    attribution_mode: str
    processes: list[ProcessInfo]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


class MonitorState:
    """The single shared state object."""

    def __init__(self, *, jobs_dir: str) -> None:
        self.jobs_dir: str = jobs_dir
        self.started_at: float = time.time()
        self.trials: dict[str, TrialSummary] = {}
        self.errors: deque[ErrorRecord] = deque(maxlen=ERROR_LIST_CAP)
        self.resources: deque[ResourceSample] = deque(maxlen=RESOURCE_SAMPLE_CAP)
        # Per-trial container resource time-series (docker stats samples).
        self.trial_resources: dict[str, deque[ContainerSample]] = {}
        # Per-trial process snapshots. These are intentionally separate from
        # container samples so PID tables do not bloat the CPU/memory curve.
        self.trial_processes: dict[str, deque[ProcessSnapshot]] = {}
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    # ── trial lifecycle ──

    def ensure_trial(
        self,
        trial_id: str,
        trial_dir: str,
        *,
        agent: str = "kimi-cli",
        harness_name: str | None = None,
        model_name: str | None = None,
        stream_name: str | None = None,
        attempt_id: str | None = None,
    ) -> TrialSummary:
        if trial_id not in self.trials:
            self.trials[trial_id] = TrialSummary(
                trial_id=trial_id,
                trial_dir=trial_dir,
                agent=agent,
                harness_name=harness_name,
                model_name=model_name,
                stream_name=stream_name,
                attempt_id=attempt_id,
            )
            self._publish({"type": "trial_discovered", "trial_id": trial_id})
        trial = self.trials[trial_id]
        # Config files can appear after the agent mount. Fill in metadata on a
        # later discovery pass without erasing values already recovered.
        if harness_name:
            trial.harness_name = harness_name
        if model_name:
            trial.model_name = model_name
        if stream_name:
            trial.stream_name = stream_name
        if attempt_id:
            trial.attempt_id = attempt_id
        return trial

    def ensure_run(
        self,
        trial_id: str,
        run_id: str,
        *,
        role: str,
        agent_name: str,
        parent_run_id: str | None = None,
        model_name: str | None = None,
    ) -> RunSummary:
        trial = self.trials[trial_id]
        if run_id not in trial.runs:
            trial.runs[run_id] = RunSummary(
                run_id=run_id,
                parent_run_id=parent_run_id,
                role=role,
                agent_name=agent_name,
                model_name=model_name,
            )
            self._publish(
                {
                    "type": "run_added",
                    "trial_id": trial_id,
                    "run_id": run_id,
                    "role": role,
                    "agent_name": agent_name,
                    "parent_run_id": parent_run_id,
                }
            )
        return trial.runs[run_id]

    def push_events(self, trial_id: str, run_id: str, events: Iterable[dict[str, Any]]) -> None:
        trial = self.trials.get(trial_id)
        if trial is None:
            return
        run = trial.runs.get(run_id)
        if run is None:
            return
        any_pushed = False
        for ev in events:
            kind = ev.get("kind", "")
            # Always update counters/timestamps (these count every event)
            run.n_events += 1
            ts = ev.get("timestamp")
            if isinstance(ts, (int, float)):
                run.last_event_at = float(ts)
                if run.started_at is None:
                    run.started_at = float(ts)
            if kind == "tool_call":
                run.n_tool_calls += 1
            elif kind == "step_begin":
                run.n_llm_calls += 1
            elif kind == "compaction_begin":
                run.n_compactions += 1
            metrics = ev.get("metrics") or {}
            pt = metrics.get("prompt_tokens")
            ct = metrics.get("completion_tokens")
            if isinstance(pt, int):
                run.prompt_tokens += pt
            if isinstance(ct, int):
                run.completion_tokens += ct
            # Only keep orchestrator-visible events in the rendered tail.
            # Drop bridge events (subagent_event / hook) — they're duplicates
            # of what the subagent's own lane already shows.
            if kind not in _TAIL_DROP_KINDS:
                run.events_tail.append(ev)
            # Always preserve structural events in the uncapped skeleton so
            # the dashboard can render the spawn arrows + boundaries even
            # for runs whose total event count exceeds RUN_EVENT_TAIL_CAP.
            if kind in _STRUCTURAL_KINDS:
                run.structural_events.append(ev)
            any_pushed = True
        if any_pushed:
            self._publish({"type": "trial_update", "trial_id": trial_id})

    def update_run_status(self, trial_id: str, run_id: str, status: str) -> None:
        trial = self.trials.get(trial_id)
        if trial is None:
            return
        run = trial.runs.get(run_id)
        if run is None or run.status == status:
            return
        run.status = status
        self._publish({"type": "trial_update", "trial_id": trial_id})

    def update_run_meta(
        self,
        trial_id: str,
        run_id: str,
        *,
        agent_name: str | None = None,
        model_name: str | None = None,
    ) -> None:
        """Relabel an existing run.

        ``ensure_run`` freezes ``agent_name`` at creation time, but some
        identity only becomes knowable later: a Workflow-tool agent's lane is
        created from its transcript, while its human label (the task id) comes
        from a ``wf_<runid>.json`` sidecar that the workflow runtime may not
        have written yet. Without this, such lanes would be stuck showing a
        hex agent id for the whole trial.
        """
        trial = self.trials.get(trial_id)
        if trial is None:
            return
        run = trial.runs.get(run_id)
        if run is None:
            return
        changed = False
        if agent_name and run.agent_name != agent_name:
            run.agent_name = agent_name
            changed = True
        if model_name and run.model_name != model_name:
            run.model_name = model_name
            changed = True
        if changed:
            self._publish({"type": "trial_update", "trial_id": trial_id})

    def set_trial_status(self, trial_id: str, status: str) -> None:
        trial = self.trials.get(trial_id)
        if trial is None or trial.status == status:
            return
        trial.status = status
        self._publish({"type": "trial_update", "trial_id": trial_id})

    def set_trial_agent(self, trial_id: str, agent: str) -> None:
        trial = self.trials.get(trial_id)
        if trial is None or trial.agent == agent:
            return
        trial.agent = agent
        self._publish({"type": "trial_update", "trial_id": trial_id})

    def set_trial_reward(self, trial_id: str, reward: float | None) -> None:
        trial = self.trials.get(trial_id)
        if trial is None:
            return
        trial.reward = reward
        self._publish({"type": "trial_update", "trial_id": trial_id})

    def set_trial_task_snapshot(self, trial_id: str, snapshot: list[dict[str, Any]]) -> None:
        trial = self.trials.get(trial_id)
        if trial is None:
            return
        trial.latest_task_snapshot = snapshot
        # Don't publish — the SSE `trial_update` events already fire on push_events.

    # ── errors ──

    def add_error(self, error: ErrorRecord) -> None:
        self.errors.append(error)
        trial = self.trials.get(error.trial_id)
        if trial is not None:
            trial.errors_count += 1
        self._publish({"type": "error", "payload": error.to_dict()})

    # ── resources ──

    def add_resource_sample(self, sample: ResourceSample) -> None:
        self.resources.append(sample)
        self._publish({"type": "resource", "payload": sample.to_dict()})

    def latest_resource(self) -> ResourceSample | None:
        return self.resources[-1] if self.resources else None

    # ── per-trial container resources ──

    def add_trial_resource_sample(self, trial_id: str, sample: ContainerSample) -> None:
        buf = self.trial_resources.get(trial_id)
        if buf is None:
            buf = deque(maxlen=TRIAL_RESOURCE_SAMPLE_CAP)
            self.trial_resources[trial_id] = buf
        buf.append(sample)
        self._publish(
            {
                "type": "trial_resource",
                "trial_id": trial_id,
                "payload": sample.to_dict(),
            }
        )

    def trial_resource_samples(self, trial_id: str) -> list[dict[str, Any]]:
        buf = self.trial_resources.get(trial_id)
        return [s.to_dict() for s in buf] if buf else []

    # ── per-trial process resources ──

    def add_trial_process_snapshot(
        self, trial_id: str, snapshot: ProcessSnapshot
    ) -> None:
        buf = self.trial_processes.get(trial_id)
        if buf is None:
            buf = deque(maxlen=TRIAL_PROCESS_SAMPLE_CAP)
            self.trial_processes[trial_id] = buf
        buf.append(snapshot)
        self._publish(
            {
                "type": "trial_process",
                "trial_id": trial_id,
                "payload": snapshot.to_dict(),
            }
        )

    def trial_process_snapshots(self, trial_id: str) -> list[dict[str, Any]]:
        buf = self.trial_processes.get(trial_id)
        return [s.to_dict() for s in buf] if buf else []

    # ── pub-sub ──

    def subscribe(self, maxsize: int = 256) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def _publish(self, event: dict[str, Any]) -> None:
        # Non-blocking fan-out: drop on slow consumer rather than back-pressure
        # the data path.
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # consumer is slow; drop this delta. Periodic full refresh
                # via REST keeps them eventually-consistent.
                pass

    # ── snapshot for REST ──

    def trials_summary(self) -> list[dict[str, Any]]:
        return [t.to_summary_dict() for t in self.trials.values()]

    def trial_detail(self, trial_id: str) -> dict[str, Any] | None:
        trial = self.trials.get(trial_id)
        return trial.to_detail_dict() if trial is not None else None

    def errors_snapshot(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.errors]
