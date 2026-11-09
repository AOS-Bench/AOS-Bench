"""kimi-code session journals -> AgentTrace v0.1.

This module is the *single source of truth* for the kimi-code wire-record
vocabulary. The offline AgentTrace converter lives here; the live monitor
parser (``aos_bench/monitor/tail/kimi_code_parser.py``) mirrors the same kind
map incrementally, exactly like ``aos_bench/monitor/tail/parser.py`` mirrors
``aos_bench/trace/kimi_cli.py``. Keep the two in lockstep: the dashboard and
the post-run AgentTrace must describe the same run in the same words.

Why disk and not stdout
-----------------------
``kimi --output-format stream-json`` emits OpenAI-chat-shaped lines with no
timestamps, no token usage and no subagent attribution, and there is no final
``result`` line (blueprint §2.7). Everything interesting is on disk instead:

    $KIMI_CODE_HOME/
      session_index.jsonl                      {sessionId, sessionDir, workDir}
      sessions/wd_<slug>_<sha256[:12]>/
        session_<uuid>/
          state.json                           agent tree + parent links
          agents/main/wire.jsonl                root journal
          agents/main/tasks/<taskId>.json       background-task sidecars
          agents/main/tasks/<taskId>/output.log
          agents/agent-0/wire.jsonl             one journal per subagent

The split between agents is *physical* — there is no ``isSidechain`` flag
inside records (``packages/agent-core-v2/src/session/agentLifecycle/addressing.ts:19-29``),
so ``agents/<id>/`` is the run boundary and ``state.json`` is the only place
the parent link lives (``session/sessionMetadata.ts:17-45``).

Two engines, one directory layout
---------------------------------
kimi-code ships two agent engines that write *different record vocabularies
into the same layout* (verified against the landed fixtures under
``aos_bench/tests/fixtures/kimi_code/``):

* default engine (agent-core-v2), ``metadata.protocol_version == "1.5"``:
  ``profile.bind`` + ``plugin.session_start`` + ``turn.ended`` + ``task.*``,
  ``state.json`` with ``id`` / ``version: 2`` / epoch-ms ints.
* legacy engine (``KIMI_CODE_LEGACY_FLAG=1``), ``protocol_version "1.4"``, and
  the upstream ``apps/vis`` fixtures at ``"1.1"``: ``config.update`` ×2 +
  ``tools.set_active_tools``, no ``turn.ended``, and a ``state.json`` whose
  ``createdAt`` / ``updatedAt`` are **ISO strings** with ``workDir`` /
  ``lastPrompt`` instead of ``id`` / ``version`` / ``cwd``.

Every reader here tolerates both. Per the blueprint we do *not* run the
1.0→1.5 migration chain (``wire/migration/migration.ts:19-35``); a journal
older than 1.5 is read as-is and a warning lands in the trace ``notes``.

Correctness rules that are easy to get wrong
--------------------------------------------
* ``time`` is epoch **milliseconds** (``wire/event2.ts:24-27``, ``:50-63``) and
  is *optional* on ``WireRecord`` (``wire/record.ts:22-26``) — divide by 1000
  and carry the last known timestamp forward when it is absent. The first line
  is ``metadata`` and carries ``created_at`` (ms) instead of ``time``.
* Token metrics come from ``step.end`` **only**. ``usage.record`` repeats the
  identical ``TokenUsage`` object at turn scope (observed byte-for-byte in
  ``live_probe/solo``), so emitting metrics on both double-counts in
  ``aos_bench/monitor/state.py:331-337``.
* ``event.step`` restarts at 1 for every turn (observed:
  ``live_probe/subagent`` agent-0 has ``turnId "0"`` step 1 then ``turnId "1"``
  step 1), so it cannot be the gap-free step counter ATIF wants. We keep a
  running per-run counter and expose the native value as ``native_step``.
* Subagent attribution is *exact* on disk — no time heuristics needed. A
  foreground ``Agent`` result starts with ``agent_id: <id>``; ``AgentSwarm``
  results carry ``<subagent agent_id="agent-0" item="alpha" …>``; background
  delegation journals ``task.started`` whose ``info`` block names both
  ``parentToolCallId`` and ``agentId``.
* Media is dehydrated to ``blobref:<mime>;<hash>`` URLs
  (``agent/blob/agentBlobServiceImpl.ts:4-37``); v1 leaves those strings alone.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Identity constants ────────────────────────────────────────────────────
# Root agent id is literally "main"; children are "agent-0", "agent-1", …
# monotonic per session (addressing.ts:19-29).
ROOT_AGENT_ID = "main"
ROOT_RUN_ID = "run_root_001"

# The two tools that create child agents (agent/tools/agent/agentTool.ts:127,
# features/swarm/tools/agent-swarm/agent-swarm.ts:16-60). The shipped 0.36.1
# tarball has no Tower* tools at all, so this set is complete for that build.
SPAWN_TOOL_NAMES = frozenset({"Agent", "AgentSwarm"})

# Journals below this protocol version get a note instead of a migration.
MIN_PROTOCOL_VERSION = (1, 5)

# ``wd_<slug>_<sha256(workdir)[:12]>`` (_base/utils/workdir-slug.ts:15-35).
_WORKDIR_KEY_PREFIX = "wd_"
_WORKDIR_HASH_LENGTH = 12
_MAX_WORKDIR_SLUG_LENGTH = 40

# Monitor-only kind substitutions. ``aos_bench/monitor/tail/parser.py:20-38``
# accepts a closed set of 15+1 kinds that does NOT include ``reasoning`` or
# ``background_task``; anything else falls into the frontend's ``default`` arm
# (app.js:1577). The AgentTrace schema does have both kinds
# (docs/framework/agent_trace.md §8.3), so this module emits the richer name
# and the live parser downgrades through this table.
MONITOR_KIND_OVERRIDES = {
    "reasoning": "message",
    "background_task": "status",
}

# Top-level record type -> AgentTrace event kind. "" means *drop the record*.
# Types absent from the shipped 0.36.1 bundle (``prompt.accepted``,
# ``runtime.set_binding``, the whole ``tower_mode.*`` family) are deliberately
# NOT listed; they are covered by the control-family prefix fallback below so
# this table only claims what the build can actually emit.
_RECORD_KIND_MAP = {
    # Journal header — consumed for created_at / protocol_version, not emitted.
    "metadata": "",
    # Turn lifecycle (agent/loop/turnOps.ts:53-97).
    "turn.prompt": "turn_begin",
    "turn.ended": "turn_end",
    "turn.cancel": "turn_end",
    "turn.steer": "status",
    # Context memory (agent/contextMemory/contextEvents.ts:26-100).
    "context.append_loop_event": "",  # dispatched on event.type, see below
    "context.append_message": "",  # dispatched on message.role, see below
    "context.apply_compaction": "compaction_end",
    "context.clear": "status",
    "context.undo": "status",
    # Explicit full compaction (agent/fullCompaction/compactionOps.ts:55-73).
    "full_compaction.begin": "compaction_begin",
    "full_compaction.complete": "compaction_end",
    "full_compaction.cancel": "compaction_end",
    # Usage / context accounting (agent/usage/usageOps.ts:26-39,
    # agent/tokenCounting/tokenCountingOps.ts:42-58).
    "usage.record": "status",
    "token_counting.measured": "status",
    "token_counting.truncated": "status",
    "token_counting.rebased": "status",
    # Permission + approval (agent/permissionMode, session/interaction,
    # agent/permissionRules). interaction.request carries the question/approval
    # itself (interactionOps.ts:44-68).
    "permission.set_mode": "status",
    "permission.record_approval_result": "approval_response",
    "interaction.request": "approval_request",
    "interaction.resolved": "approval_response",
    "interruptionReminder.recorded": "status",
    # Session / profile / tool policy. ``profile.bind`` (v2) and
    # ``config.update`` (legacy) are the lane-label source (profileOps.ts:72-125).
    "profile.bind": "status",
    "config.update": "status",
    "tools.set_active_tools": "status",
    "tools.reset_active_tools": "status",
    "tools.register_user_tool": "status",
    "tools.unregister_user_tool": "status",
    "tools.update_store": "status",
    "plugin.session_start": "status",
    # The smoke-run assertion that aos_pool wired up (mcpDiscoveryOps.ts:49).
    "mcp.tools_discovered": "status",
    # Background tasks (agent/task/taskOps.ts:48-61).
    "task.started": "background_task",
    "task.terminated": "background_task",
    # Richest per-request timeline, but ~95 KB of a 101 KB journal is these two
    # records; skipped in v1 (they are read for model/provider only).
    "llm.request": "",
    "llm.tools_snapshot": "",
    # Modes and goals — payload kept verbatim.
    "swarm_mode.enter": "status",
    "swarm_mode.exit": "status",
    "plan_mode.enter": "status",
    "plan_mode.cancel": "status",
    "plan_mode.exit": "status",
    "plan.revision": "status",
    "goal.create": "status",
    "goal.update": "status",
    "goal.clear": "status",
    "forked": "status",
}

# ``context.append_loop_event`` -> kind, dispatched on the nested event type
# (agent/contextMemory/loopEventFold.ts:54-104). ``content.part`` is absent
# because it depends on ``part.type``.
_LOOP_EVENT_KIND_MAP = {
    "step.begin": "step_begin",
    "step.end": "step_end",
    "tool.call": "tool_call",
    "tool.result": "tool_result",
}

# A record type we do not know, but whose family is a control/metadata family,
# is a ``status`` rather than a ``message``. This is what keeps a future
# ``tools.foo`` or a re-introduced ``runtime.set_binding`` out of the message
# stream without this module claiming those types exist today.
_STATUS_TYPE_PREFIXES = (
    "config.",
    "goal.",
    "interruptionReminder.",
    "mcp.",
    "permission.",
    "plan.",
    "plan_mode.",
    "plugin.",
    "profile.",
    "prompt.",
    "runtime.",
    "swarm_mode.",
    "token_counting.",
    "tools.",
    "tower_mode.",
)

_AGENT_ID_RE = re.compile(r"^\s*agent_id:\s*(\S+)", re.MULTILINE)
_SWARM_CHILD_RE = re.compile(
    r"<subagent\b[^>]*\bagent_id=\"([^\"]+)\"(?:[^>]*\bitem=\"([^\"]*)\")?",
)
_SUBAGENT_TYPE_RE = re.compile(r"^\s*actual_subagent_type:\s*(\S+)", re.MULTILINE)


@dataclass
class KimiCodeSession:
    """One kimi-code session directory, read into memory.

    ``agents`` maps agent id -> the parsed wire records of that agent, ordered
    as written, with ``main`` first. ``parents`` / ``labels`` / ``agent_types``
    come from ``state.json``; ``labels`` keeps ``swarmItem`` so a swarm lane can
    be labelled with the item it worked on. ``created_at`` is epoch **seconds**.
    """

    session_dir: Path
    session_id: str
    agents: dict[str, list[dict[str, Any]]]
    parents: dict[str, str | None]
    labels: dict[str, dict[str, Any]]
    created_at: float | None
    agent_types: dict[str, str] = field(default_factory=dict)
    protocol_versions: dict[str, str] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ── Session discovery ─────────────────────────────────────────────────────


def find_kimi_code_session_dir(home_dir: Path, work_dir: str) -> Path:
    """Resolve the newest session directory for one work dir.

    The workspace key is deterministic — ``wd_<slug(basename)>_<sha256(path)[:12]>``
    (``_base/utils/workdir-slug.ts:19-35``) — so this needs no index lookup.
    Several ``session_<uuid>`` dirs can share one key (resume / fork), so the
    newest by mtime wins, mirroring ``find_kimi_cli_latest_session_dir``.
    """

    sessions_root = Path(home_dir) / "sessions" / _encode_work_dir_key(work_dir)
    if not sessions_root.is_dir():
        raise FileNotFoundError(f"No kimi-code sessions found for {work_dir}")
    session_dir = _newest_dir(sessions_root.glob("session_*"))
    if session_dir is None:
        session_dir = _newest_dir(
            path for path in sessions_root.iterdir() if path.is_dir()
        )
    if session_dir is None:
        raise FileNotFoundError(f"No kimi-code session directories in {sessions_root}")
    return session_dir


def find_kimi_code_latest_session_dir(home_dir: Path) -> Path | None:
    """Resolve the newest session dir in a ``$KIMI_CODE_HOME``, or ``None``.

    ``session_index.jsonl`` is the cheap path: append-only
    ``{sessionId, sessionDir, workDir}`` lines with ``{sessionId, deleted:true}``
    tombstones, later lines overriding earlier ones
    (``session/store/session-index.ts:7-80``; v2 writes the same file at
    ``sessionLifecycleService.ts:392-399``).

    The recorded ``sessionDir`` is an absolute path *as seen by the agent*, and
    in a trial the agent sees ``/logs/agent/kimi-code-home`` while we read
    ``<trial>/agent/kimi-code-home`` — so the recorded path is re-rooted onto
    ``home_dir`` by its trailing ``sessions/<wd>/<session>`` components before
    it is trusted. Falls back to globbing when the index is missing or stale.
    """

    home_dir = Path(home_dir)
    for session_dir in reversed(_index_session_dirs(home_dir)):
        if session_dir.is_dir():
            return session_dir

    sessions_root = home_dir / "sessions"
    if not sessions_root.is_dir():
        return None
    # Tiered glob: the real layout first, then progressively looser shapes so a
    # hand-made or upstream-fixture tree (``sessions/<name>/``) still resolves.
    for pattern in ("wd_*/session_*", "*/session_*", "*"):
        newest = _newest_dir(
            path
            for path in sessions_root.glob(pattern)
            if _looks_like_session_dir(path)
        )
        if newest is not None:
            return newest
    return None


def _index_session_dirs(home_dir: Path) -> list[Path]:
    """Live session dirs from ``session_index.jsonl``, oldest line first."""

    index_path = home_dir / "session_index.jsonl"
    if not index_path.is_file():
        return []

    # Later lines override earlier ones for the same sessionId, and a
    # ``deleted`` line is a tombstone.
    live: dict[str, Path] = {}
    for raw in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        # Index lines are NOT wire records (no ``type`` field), so
        # ``parse_kimi_code_wire_line`` would reject them. A torn last line is
        # possible here too, for the same append-only reason.
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        session_id = entry.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            continue
        if entry.get("deleted") is True:
            live.pop(session_id, None)
            continue
        recorded = entry.get("sessionDir")
        if not isinstance(recorded, str) or not recorded:
            continue
        live[session_id] = _reroot_session_dir(home_dir, recorded)
    return list(live.values())


def _reroot_session_dir(home_dir: Path, recorded: str) -> Path:
    """Map a recorded absolute ``sessionDir`` onto ``home_dir``.

    Keeps the trailing ``sessions/<workdir-key>/<session_id>`` components, which
    are stable across the container/host path split. Returns the literal path
    when the shape is unrecognised so an unusual layout still gets a chance.
    """

    parts = Path(recorded).parts
    if "sessions" in parts:
        tail = parts[parts.index("sessions") :]
        return home_dir.joinpath(*tail)
    return Path(recorded)


def _looks_like_session_dir(path: Path) -> bool:
    return path.is_dir() and (
        (path / "agents").is_dir() or (path / "state.json").is_file()
    )


def _newest_dir(candidates: Any) -> Path | None:
    dirs = [path for path in candidates if path.is_dir()]
    if not dirs:
        return None
    dirs.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return dirs[0]


def _encode_work_dir_key(work_dir: str) -> str:
    """Port of ``encodeWorkDirKey`` (_base/utils/workdir-slug.ts:29-35)."""

    normalized = str(work_dir).replace("\\", "/").rstrip("/")
    base = normalized.rsplit("/", 1)[-1] or normalized
    slug = _slugify_work_dir_name(base)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[
        :_WORKDIR_HASH_LENGTH
    ]
    return f"{_WORKDIR_KEY_PREFIX}{slug}_{digest}"


def _slugify_work_dir_name(name: str) -> str:
    """Port of ``slugifyWorkDirName`` (_base/utils/workdir-slug.ts:19-27)."""

    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower())
    slug = slug.strip("-")[:_MAX_WORKDIR_SLUG_LENGTH].strip("-")
    return "workspace" if slug in {"", ".", ".."} else slug


# ── Record parsing / kind mapping ─────────────────────────────────────────


def parse_kimi_code_wire_line(line: str) -> dict | None:
    """Parse one ``wire.jsonl`` line into a flat record, or ``None``.

    Records are flat: ``{"type": <dotted type>, "time": <epoch ms>, …}`` — the
    ``Event2.serialize()`` shape (``wire/event2.ts:50-63``). Writes are
    ``O_APPEND`` + fsync per microtask batch
    (``persistence/backends/node-fs/fileStorageService.ts:123-149``), so only
    the final line can ever be torn; upstream's own readers drop it silently
    (``appendLogStore.ts:84-99``) and so do we. The ``metadata`` header line is
    returned like any other record — callers decide what to do with it.
    """

    text = line.strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("type"), str):
        return None
    return record


def map_wire_kind(record: dict) -> str:
    """Map one wire record to its AgentTrace event kind.

    Returns ``""`` for records that must be **dropped** (the ``metadata``
    header, ``llm.request`` / ``llm.tools_snapshot``, and
    ``context.append_message`` with ``role == "assistant"`` — the latter is
    already covered by the loop events, so emitting both double-counts).

    The two kinds the live monitor cannot render (``reasoning``,
    ``background_task``) are listed in ``MONITOR_KIND_OVERRIDES``.
    """

    record_type = record.get("type")
    if not isinstance(record_type, str):
        return "message"

    if record_type == "context.append_loop_event":
        event = record.get("event")
        event_type = event.get("type") if isinstance(event, dict) else None
        if event_type == "content.part":
            part = event.get("part") if isinstance(event, dict) else None
            part_type = part.get("type") if isinstance(part, dict) else None
            # think -> reasoning is the one place the monitor kind differs.
            return "reasoning" if part_type == "think" else "message"
        if isinstance(event_type, str):
            return _LOOP_EVENT_KIND_MAP.get(event_type, "status")
        return "status"

    if record_type == "context.append_message":
        message = record.get("message")
        role = message.get("role") if isinstance(message, dict) else None
        if role == "assistant":
            return ""
        if role == "tool":
            # Never observed on the v2 engine (tool results arrive as loop
            # events); kept because the legacy engine can produce it.
            return "tool_result"
        return "message"

    mapped = _RECORD_KIND_MAP.get(record_type)
    if mapped is not None:
        return mapped
    if record_type.startswith(_STATUS_TYPE_PREFIXES):
        return "status"
    return "message"


# ── Session reading ──────────────────────────────────────────────────────


def read_kimi_code_session(session_dir: Path) -> KimiCodeSession:
    """Read every agent journal plus ``state.json`` from one session dir."""

    session_dir = Path(session_dir).resolve()
    if not session_dir.is_dir():
        raise FileNotFoundError(f"Session directory not found: {session_dir}")

    notes: list[str] = []
    state = _read_json(session_dir / "state.json")
    if not state:
        notes.append("state.json missing or unreadable — parent links unavailable")
    state_agents = state.get("agents") if isinstance(state.get("agents"), dict) else {}

    # Agent ids come from the directories on disk *and* from state.json: a
    # directory can exist before state.json is rewritten, and state.json can
    # name an agent whose journal has not been flushed yet.
    agents_root = session_dir / "agents"
    dir_ids = (
        {path.name for path in agents_root.iterdir() if path.is_dir()}
        if agents_root.is_dir()
        else set()
    )
    agent_ids = _sorted_agent_ids(dir_ids | set(state_agents))

    agents: dict[str, list[dict[str, Any]]] = {}
    protocol_versions: dict[str, str] = {}
    for agent_id in agent_ids:
        records = _load_wire_records(agents_root / agent_id / "wire.jsonl")
        agents[agent_id] = records
        version = _protocol_version(records)
        if version:
            protocol_versions[agent_id] = version

    parents: dict[str, str | None] = {}
    labels: dict[str, dict[str, Any]] = {}
    agent_types: dict[str, str] = {}
    for agent_id in agent_ids:
        meta = (
            state_agents.get(agent_id)
            if isinstance(state_agents.get(agent_id), dict)
            else {}
        )
        agent_labels = (
            meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
        )
        labels[agent_id] = dict(agent_labels)
        agent_types[agent_id] = str(
            meta.get("type") or ("main" if agent_id == ROOT_AGENT_ID else "sub")
        )
        parents[agent_id] = _resolve_parent_agent_id(
            agent_id, meta, agent_labels, agent_ids
        )

    created_at = _state_timestamp(state.get("createdAt"))
    if created_at is None:
        created_at = _records_created_at(agents.get(ROOT_AGENT_ID) or [])

    session_id = str(state.get("id") or session_dir.name)
    return KimiCodeSession(
        session_dir=session_dir,
        session_id=session_id,
        agents=agents,
        parents=parents,
        labels=labels,
        created_at=created_at,
        agent_types=agent_types,
        protocol_versions=protocol_versions,
        state=state,
        notes=notes,
    )


def _resolve_parent_agent_id(
    agent_id: str,
    meta: dict[str, Any],
    agent_labels: dict[str, Any],
    known_ids: list[str],
) -> str | None:
    """``labels.parentAgentId`` wins over ``parentAgentId``.

    ``subagentParentAgentId()`` prefers the label
    (``session/agentLifecycle/subagentMetadata.ts:11-46``); ``parentAgentId`` is
    set to ``"main"`` for every non-main agent, so it is the weaker signal.
    """

    if agent_id == ROOT_AGENT_ID:
        return None
    for candidate in (agent_labels.get("parentAgentId"), meta.get("parentAgentId")):
        if isinstance(candidate, str) and candidate and candidate != agent_id:
            return candidate
    # No state.json entry: assume the root, which is what every observed
    # session does anyway (children are always spawned by main).
    return ROOT_AGENT_ID if ROOT_AGENT_ID in known_ids else None


def _sorted_agent_ids(agent_ids: Any) -> list[str]:
    """``main`` first, then ``agent-<n>`` numerically, then anything else.

    ``state.json``'s ``agents`` key order is NOT sorted (the swarm fixture lists
    ``agent-1`` before ``agent-0``), so ordering is ours to impose.
    """

    def sort_key(agent_id: str) -> tuple[int, int, str]:
        if agent_id == ROOT_AGENT_ID:
            return (0, 0, "")
        match = re.fullmatch(r"agent-(\d+)", agent_id)
        if match:
            return (1, int(match.group(1)), agent_id)
        return (2, 0, agent_id)

    return sorted({str(agent_id) for agent_id in agent_ids}, key=sort_key)


def _load_wire_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        record = parse_kimi_code_wire_line(raw)
        if record is not None:
            records.append(record)
    return records


# ── Normalization ────────────────────────────────────────────────────────


def normalize_kimi_code_records(
    records: list[dict[str, Any]],
    *,
    run_id: str,
    synthesize_subagent_spawns: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize one agent's wire records into AgentTrace events.

    Returns ``(events, info)``. ``info`` is everything the caller needs that is
    not an event, and is part of this module's contract:

    ``created_at`` / ``started_at`` / ``ended_at`` / ``wall_time_ms``
        epoch **seconds** (ms for ``wall_time_ms``); ``created_at`` is the
        ``metadata`` header's value, the other two are first/last record.
    ``protocol_version``
        the header's version string, or ``None``.
    ``prompt_tokens`` / ``completion_tokens`` / ``n_steps`` / ``n_turns``
        aggregates. Tokens come from ``step.end`` plus any ``usage.record``
        whose ``usageScope`` is not ``"turn"`` — a turn-scoped record repeats
        its ``step.end``'s numbers, but an out-of-turn one (full compaction)
        has no ``step.end`` to repeat.
    ``profile_name`` / ``model_alias`` / ``model_name`` / ``model_provider``
        lane label + model identity, from ``profile.bind`` / ``config.update``
        and ``llm.request``.
    ``tool_calls``
        ``{tool_call_id: run_id}`` for cross-run tool bookkeeping.
    ``spawn_events``
        ``{child_agent_id: spawn event}`` — only populated once the child id is
        known (see below).
    ``child_launch_modes`` / ``child_subagent_types`` / ``child_swarm_items``
        per child-agent-id attributes recovered from the spawn site.
    ``background_tasks``
        ``{task_id: latest task info block}`` from ``task.started`` /
        ``task.terminated``.
    ``last_turn_reason`` / ``turn_end_reasons`` / ``last_finish_reason``
        run status input (the last ``step.end.finishReason`` is the fallback for
        legacy journals, which never carry ``turn.ended``). Both are **per-turn,
        not sticky**: a ``turn.prompt`` / ``turn.steer`` / ``step.begin`` after a
        turn ended clears them, because ``turn.ended`` fires once per turn and a
        run whose turn 1 completed can still be busy on turn 7.
        ``turn_end_reasons`` keeps the full history.
    ``mcp_servers``
        server names seen in ``mcp.tools_discovered`` — the "did aos_pool wire
        up" signal.
    ``notes``
        human-readable warnings to fold into the trace's ``notes``.

    With ``synthesize_subagent_spawns=True`` an ``Agent`` / ``AgentSwarm`` tool
    call emits ``subagent_spawn`` event(s) *before* the ``tool_call`` event
    (one per ``items[]`` entry for a swarm, so lane counts match). The child id
    is unknown at that moment, so ``child_run_id`` starts as ``None`` and is
    back-filled in place from the matching ``tool.result`` output or from
    ``task.started.info`` — both of which name the child agent explicitly, so
    no nearest-earlier-time heuristic is needed.
    """

    events: list[dict[str, Any]] = []
    info: dict[str, Any] = {
        "created_at": None,
        "started_at": None,
        "ended_at": None,
        "wall_time_ms": None,
        "protocol_version": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        # Count of ``content.part`` events whose part type is ``think``.
        #
        # kimi-code is the one arm where reasoning TOKENS are unobtainable: the
        # upstream API does return them (a direct probe of the gateway's moonshot
        # passthrough yields
        # ``usage.completion_tokens_details.reasoning_tokens``), but the CLI
        # remaps usage into its own four-field ``TokenUsage``
        # ``{inputOther, output, inputCacheRead, inputCacheCreation}`` and the
        # reasoning count has no destination field, so it is dropped. The
        # ``llm.request`` record logs only metadata (prompt/tool *hashes*, no
        # bodies), so the response is not recoverable from the journal either.
        #
        # The reasoning TEXT does survive, as ``think`` parts. Counting them
        # gives the thinking check a real signal for this arm instead of a bare
        # "unavailable" — it is a count of reasoning emissions, NOT a token
        # count, and must not be compared against codex's
        # ``reasoning_output_tokens``.
        "n_thinking_parts": 0,
        "n_steps": 0,
        "n_turns": 0,
        "profile_name": None,
        "model_alias": None,
        "model_name": None,
        "model_provider": None,
        "tool_calls": {},
        "spawn_events": {},
        "child_launch_modes": {},
        "child_subagent_types": {},
        "child_swarm_items": {},
        "background_tasks": {},
        "turn_end_reasons": [],
        "last_turn_reason": None,
        "last_finish_reason": None,
        "mcp_servers": [],
        "notes": [],
    }

    # Spawn events awaiting a child agent id, keyed by the spawning tool call.
    pending_spawns: dict[str, list[dict[str, Any]]] = {}
    seq = 0
    step_n = 0
    step_id: str | None = None
    last_ts: float | None = None

    for record in records:
        record_type = str(record.get("type") or "")

        if record_type == "metadata":
            info["created_at"] = _ms_to_seconds(record.get("created_at"))
            version = record.get("protocol_version")
            if isinstance(version, str):
                info["protocol_version"] = version
                if _parse_version(version) < MIN_PROTOCOL_VERSION:
                    info["notes"].append(
                        f"{run_id}: journal protocol_version {version} < 1.5 — "
                        "read as-is, no migration applied"
                    )
            continue

        ts = _ms_to_seconds(record.get("time"))
        if ts is None:
            ts = last_ts  # ``time`` is optional on WireRecord (record.ts:22-26)
        else:
            last_ts = ts
        if info["started_at"] is None and ts is not None:
            info["started_at"] = ts
        if ts is not None:
            info["ended_at"] = ts

        loop_event = (
            record.get("event") if record_type == "context.append_loop_event" else None
        )
        loop_type = loop_event.get("type") if isinstance(loop_event, dict) else None

        # ── side effects, before any drop decision ───────────────────────
        if record_type in {"profile.bind", "config.update"}:
            profile_name = record.get("profileName")
            if isinstance(profile_name, str) and profile_name:
                info["profile_name"] = profile_name
            alias = record.get("modelAlias")
            if isinstance(alias, str) and alias:
                info["model_alias"] = alias
        elif record_type == "llm.request":
            for key, target in (
                ("model", "model_name"),
                ("provider", "model_provider"),
            ):
                value = record.get(key)
                if isinstance(value, str) and value:
                    info[target] = value
        elif record_type == "mcp.tools_discovered":
            server = record.get("serverName")
            if isinstance(server, str) and server and server not in info["mcp_servers"]:
                info["mcp_servers"].append(server)
        elif record_type == "usage.record":
            # Out-of-turn LLM spend. ``usageScope`` is ``"turn"`` only for the
            # turn loop's own requests, which already carry their usage on a
            # ``step.end`` (byte-identical object, verified in
            # ``live_probe/solo``) — charging those twice would double every
            # token. Any other scope comes from a request the loop never made
            # (``AgentUsageService.record`` stamps ``"session"`` whenever the
            # request source is not a turn: full compaction and its
            # overflow-shrink retries) and therefore has **no** ``step.end`` at
            # all, so dropping it silently under-reports the run.
            if record.get("usageScope") != "turn":
                prompt_tokens, completion_tokens = _split_token_usage(
                    record.get("usage")
                )
                info["prompt_tokens"] += prompt_tokens
                info["completion_tokens"] += completion_tokens
        elif record_type == "turn.ended":
            reason = record.get("reason")
            if isinstance(reason, str):
                info["turn_end_reasons"].append(reason)
                info["last_turn_reason"] = reason
            info["n_turns"] += 1
        elif record_type in {"turn.prompt", "turn.steer"}:
            # A new (or steered) turn means the two status signals below
            # describe a turn that is already over, so they stop being the
            # run's verdict. ``turn.ended`` fires once per TURN upstream — not
            # once per session — so without this reset the first completed turn
            # would latch the run to ``completed`` for the rest of a trial that
            # is still working (blueprint §4 P4).
            info["last_turn_reason"] = None
            info["last_finish_reason"] = None
        elif record_type in {"task.started", "task.terminated"}:
            _absorb_task_record(record, info=info, pending_spawns=pending_spawns)
        elif loop_type == "content.part":
            # Reasoning emissions. See ``n_thinking_parts`` in the info dict
            # for why this is counted rather than read off a usage field.
            part = (
                loop_event.get("part") if isinstance(loop_event, dict) else None
            )
            if isinstance(part, dict) and part.get("type") == "think":
                info["n_thinking_parts"] += 1
        elif loop_type == "step.begin":
            step_n += 1
            step_id = f"{run_id}_step_{step_n}"
            info["n_steps"] = step_n
            # Same reset as ``turn.prompt`` above, for the legacy engine (which
            # writes neither ``turn.ended`` nor, in some journals, a
            # ``turn.prompt`` we can see) and for a steer that only shows up as
            # a fresh loop step.
            info["last_turn_reason"] = None
            info["last_finish_reason"] = None
        elif loop_type == "step.end":
            usage = loop_event.get("usage") if isinstance(loop_event, dict) else None
            prompt_tokens, completion_tokens = _split_token_usage(usage)
            info["prompt_tokens"] += prompt_tokens
            info["completion_tokens"] += completion_tokens
            finish_reason = loop_event.get("finishReason")
            if isinstance(finish_reason, str):
                info["last_finish_reason"] = finish_reason
        elif loop_type == "tool.call":
            tool_call_id = loop_event.get("toolCallId")
            if isinstance(tool_call_id, str) and tool_call_id:
                info["tool_calls"][tool_call_id] = run_id
            if synthesize_subagent_spawns:
                spawned = _synthesize_spawn_events(
                    loop_event,
                    run_id=run_id,
                    timestamp=ts,
                    step_id=step_id,
                    seq_start=seq,
                )
                if spawned:
                    seq += len(spawned)
                    events.extend(spawned)
                    call_id = str(loop_event.get("toolCallId") or "")
                    pending_spawns.setdefault(call_id, []).extend(spawned)
        elif loop_type == "tool.result":
            _absorb_tool_result(loop_event, info=info, pending_spawns=pending_spawns)

        kind = map_wire_kind(record)
        if not kind:
            continue

        seq += 1
        events.append(
            {
                "event_id": f"{run_id}_evt_{seq:04d}",
                "seq": seq,
                "timestamp": ts,
                "kind": kind,
                "phase": _event_phase(record_type, loop_type, kind),
                "step_id": step_id,
                "parent_event_id": None,
                "payload": _event_payload(record, kind=kind, step_n=step_n),
                "metrics": _event_metrics(kind, loop_event, record),
                "raw_ref": {
                    "source": "kimi-code-wire",
                    "path": None,
                    "message_type": loop_type or record_type,
                },
                "extra": _event_extra(record, loop_event),
            }
        )

    if info["started_at"] is not None and info["ended_at"] is not None:
        info["wall_time_ms"] = int(
            round((info["ended_at"] - info["started_at"]) * 1000)
        )

    return events, info


def _synthesize_spawn_events(
    loop_event: dict[str, Any],
    *,
    run_id: str,
    timestamp: float | None,
    step_id: str | None,
    seq_start: int,
) -> list[dict[str, Any]]:
    """Build ``subagent_spawn`` events for one ``Agent`` / ``AgentSwarm`` call.

    ``AgentSwarm`` fans out one child per ``items[]`` entry
    (``features/swarm/tools/agent-swarm/agent-swarm.ts:16-60``), so it gets one
    spawn event per item; the child ids arrive later in the tool result.
    """

    name = loop_event.get("name")
    if name not in SPAWN_TOOL_NAMES:
        return []

    call_id = loop_event.get("toolCallId")
    call_id = call_id if isinstance(call_id, str) else None
    args = loop_event.get("args") if isinstance(loop_event.get("args"), dict) else {}
    subagent_type = args.get("subagent_type") or args.get("type")
    description = args.get("description")
    prompt = args.get("prompt") or args.get("prompt_template")
    run_in_background = bool(args.get("run_in_background"))
    launch_mode = "background" if run_in_background else "foreground"

    items = args.get("items") if name == "AgentSwarm" else None
    swarm_items: list[Any] = (
        list(items) if isinstance(items, list) and items else [None]
    )

    spawns: list[dict[str, Any]] = []
    for index, item in enumerate(swarm_items):
        seq = seq_start + index + 1
        spawns.append(
            {
                "event_id": f"{run_id}_evt_{seq:04d}",
                "seq": seq,
                "timestamp": timestamp,
                "kind": "subagent_spawn",
                "phase": "begin",
                "step_id": step_id,
                "parent_event_id": None,
                "payload": {
                    # Unknown until the result / task record names the child.
                    "child_run_id": None,
                    "subagent_type": subagent_type
                    if isinstance(subagent_type, str)
                    else None,
                    "description": _clip(description),
                    "prompt": _clip(prompt),
                    "launch_mode": launch_mode,
                    "task_id": None,
                    "parent_tool_call_id": call_id,
                    "swarm_item": item,
                    "spawn_tool": name,
                },
                "metrics": {},
                "raw_ref": {
                    "source": "kimi-code-wire",
                    "path": None,
                    "message_type": f"tool.call:{name}",
                },
                "extra": {"parent_tool_call_id": call_id},
            }
        )
    return spawns


def _absorb_tool_result(
    loop_event: dict[str, Any],
    *,
    info: dict[str, Any],
    pending_spawns: dict[str, list[dict[str, Any]]],
) -> None:
    """Back-fill child agent ids from an ``Agent`` / ``AgentSwarm`` result.

    Both tools name their children in the result text, so attribution is exact:
    ``agent_id: agent-0\\nactual_subagent_type: coder\\nstatus: completed`` for
    ``Agent``, and ``<subagent agent_id="agent-0" item="alpha" …>`` per child
    for ``AgentSwarm`` (observed in ``live_probe/{subagent,swarm}``).
    """

    call_id = loop_event.get("toolCallId")
    if not isinstance(call_id, str) or call_id not in pending_spawns:
        return
    spawns = pending_spawns[call_id]
    text = _result_output_text(loop_event.get("result"))
    if not text:
        return

    swarm_children = _SWARM_CHILD_RE.findall(text)
    if swarm_children:
        by_item = {item: agent_id for agent_id, item in swarm_children if item}
        for index, spawn in enumerate(_unbound_spawns(spawns)):
            item = spawn["payload"].get("swarm_item")
            agent_id = by_item.get(item)
            if agent_id is None and index < len(swarm_children):
                agent_id = swarm_children[index][0]
            if agent_id:
                _bind_spawn_child(spawn, agent_id=agent_id, info=info)
        return

    match = _AGENT_ID_RE.search(text)
    unbound = _unbound_spawns(spawns)
    if match and unbound:
        subagent_type = _SUBAGENT_TYPE_RE.search(text)
        _bind_spawn_child(
            unbound[0],
            agent_id=match.group(1),
            info=info,
            subagent_type=subagent_type.group(1) if subagent_type else None,
        )


def _absorb_task_record(
    record: dict[str, Any],
    *,
    info: dict[str, Any],
    pending_spawns: dict[str, list[dict[str, Any]]],
) -> None:
    """Record a background task and bind its child agent, if it has one.

    ``task.started`` / ``task.terminated`` carry an ``info`` block with both
    ``parentToolCallId`` and ``agentId`` (``agent/task/taskOps.ts:48-61`,
    observed in ``live_probe/background``) — the exact join a
    ``run_in_background`` delegation needs.
    """

    task_info = record.get("info")
    if not isinstance(task_info, dict):
        return
    task_id = task_info.get("taskId") or task_info.get("task_id")
    if isinstance(task_id, str) and task_id:
        merged = dict(info["background_tasks"].get(task_id) or {})
        merged.update(task_info)
        output_tail = record.get("outputTail")
        if isinstance(output_tail, str):
            merged["outputTail"] = output_tail
        info["background_tasks"][task_id] = merged

    agent_id = task_info.get("agentId")
    parent_call_id = task_info.get("parentToolCallId")
    if not isinstance(agent_id, str) or not agent_id:
        return
    spawns = (
        pending_spawns.get(parent_call_id) if isinstance(parent_call_id, str) else None
    )
    subagent_type = task_info.get("subagentType")
    unbound = _unbound_spawns(spawns) if spawns else []
    if unbound:
        _bind_spawn_child(
            unbound[0],
            agent_id=agent_id,
            info=info,
            subagent_type=subagent_type if isinstance(subagent_type, str) else None,
            launch_mode="background",
            task_id=task_id if isinstance(task_id, str) else None,
        )
    else:
        # No spawn event to bind (e.g. the parser was told not to synthesize),
        # but the attributes are still worth carrying to the child run.
        info["child_launch_modes"][agent_id] = "background"
        if isinstance(subagent_type, str) and subagent_type:
            info["child_subagent_types"][agent_id] = subagent_type


def _unbound_spawns(spawns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The spawn events in ``spawns`` that still have no child agent id.

    ``pending_spawns`` is keyed by ``toolCallId``, which is provider-generated
    and only unique *within* a step — upstream explicitly tolerates providers
    that reuse ids across responses (``call_0`` again in the next step). Two
    ``Agent`` calls sharing an id therefore share one pending list, and binding
    ``spawns[0]`` unconditionally gave the first spawn the *second* result's
    child (leaving the second spawn unbound and its child with no
    ``source_event_id``). Results and ``task.started`` records arrive in call
    order, so consuming the first still-unbound spawn is the exact pairing.
    """

    return [spawn for spawn in spawns if spawn["payload"].get("child_run_id") is None]


def _bind_spawn_child(
    spawn: dict[str, Any],
    *,
    agent_id: str,
    info: dict[str, Any],
    subagent_type: str | None = None,
    launch_mode: str | None = None,
    task_id: str | None = None,
) -> None:
    """Attach a now-known child agent id to a spawn event, in place."""

    payload = spawn["payload"]
    payload["child_run_id"] = f"run_sub_{agent_id}"
    # ``task_id`` is the agent id on purpose: the frontend falls back to
    # ``"run_sub_" + payload.task_id`` when ``child_run_id`` is missing
    # (aos_bench/monitor/static/app.js:1528-1540).
    payload["task_id"] = agent_id
    if subagent_type and not payload.get("subagent_type"):
        payload["subagent_type"] = subagent_type
    if launch_mode:
        payload["launch_mode"] = launch_mode
    if task_id:
        payload["native_task_id"] = task_id
        spawn["extra"]["native_task_id"] = task_id

    info["spawn_events"][agent_id] = spawn
    info["child_launch_modes"][agent_id] = payload.get("launch_mode") or "foreground"
    if payload.get("subagent_type"):
        info["child_subagent_types"][agent_id] = payload["subagent_type"]
    if payload.get("swarm_item") is not None:
        info["child_swarm_items"][agent_id] = payload["swarm_item"]


# ── Event field builders ─────────────────────────────────────────────────


def _event_phase(record_type: str, loop_type: str | None, kind: str) -> str:
    """``begin`` / ``update`` / ``completed`` only (parser.py:41-48)."""

    if kind in {"turn_begin", "step_begin", "compaction_begin", "subagent_spawn"}:
        return "begin"
    if kind in {
        "turn_end",
        "step_end",
        "compaction_end",
        "tool_result",
        "approval_response",
    }:
        return "completed"
    if kind == "background_task":
        return "completed" if record_type == "task.terminated" else "begin"
    if kind in {"status", "approval_request"}:
        return "update"
    if loop_type == "tool.call":
        return "begin"
    return "completed"


def _event_payload(record: dict[str, Any], *, kind: str, step_n: int) -> dict[str, Any]:
    """Build the normalized payload for one record.

    Payload keys are a hard contract with the frontend (blueprint §4 P9/P12):
    ``message`` needs ``type`` + matching ``text`` / ``think``; ``tool_call``
    needs ``function.name`` and ``id``; ``tool_result`` needs the same id key
    plus a ``return_value`` in the kimi dict shape ``{"output": …}`` so
    ``aos_bench/monitor/tasks.py:115-124`` works unchanged; ``step_begin``
    needs ``n``; ``status`` may carry ``context_tokens``.
    """

    record_type = str(record.get("type") or "")
    event = record.get("event") if record_type == "context.append_loop_event" else None
    if isinstance(event, dict):
        return _loop_event_payload(event, step_n=step_n)

    if record_type == "context.append_message":
        return _message_payload(record, kind=kind)

    if record_type in {"turn.prompt", "turn.steer"}:
        # There is no ``user_input`` field on disk — the prompt is
        # ``input: [{type:'text',text:…}]`` plus an ``origin`` block
        # (agent/loop/turnOps.ts:46-70). ``user_input`` is synthesized for the
        # readers that expect one word for the turn.
        parts = record.get("input") if isinstance(record.get("input"), list) else []
        return {
            "user_input": _join_text_parts(parts),
            "input": parts,
            "origin": record.get("origin"),
        }

    if record_type in {"turn.ended", "turn.cancel"}:
        reason = record.get("reason")
        payload: dict[str, Any] = {
            "reason": reason,
            "turn_id": record.get("turnId"),
            "duration_ms": record.get("durationMs"),
            "target": record.get("target"),
            "turn_error": record.get("error"),
        }
        # ``payload["error"] is True`` is the error-detector's marker
        # (aos_bench/monitor/errors.py:51-57); only a genuinely failed turn
        # should trip it, not a user cancellation.
        if reason == "failed":
            payload["error"] = True
        return _drop_none(payload)

    if record_type == "usage.record":
        return {
            "token_usage": record.get("usage") or {},
            "usage_scope": record.get("usageScope"),
            "model": record.get("model"),
        }

    if record_type.startswith("token_counting."):
        # ``{length, tokens}`` (+ ``measured`` on rebased) —
        # agent/tokenCounting/tokenCountingOps.ts:39-62. The frontend's status
        # list reads ``payload.context_tokens``.
        return _drop_none(
            {
                "context_tokens": record.get("tokens"),
                "context_length": record.get("length"),
                "measured": record.get("measured"),
                "counting": record_type.split(".", 1)[1],
            }
        )

    if record_type == "context.apply_compaction":
        return _drop_none(
            {
                "summary": _clip(record.get("summary") or record.get("contextSummary")),
                "compactedCount": record.get("compactedCount") or record.get("count"),
                "tokensBefore": record.get("tokensBefore"),
                "tokensAfter": record.get("tokensAfter"),
                "droppedCount": record.get("droppedCount"),
            }
        )

    if record_type == "mcp.tools_discovered":
        # Fields are {serverName, hash, tools, enabledNames, collisions?}
        # (agent/mcp/mcpDiscoveryOps.ts:30-52). ``tools`` holds full JSON
        # Schemas, so only the names are kept.
        return _drop_none(
            {
                "server_name": record.get("serverName"),
                "tool_names": record.get("enabledNames"),
                "collisions": record.get("collisions"),
                "hash": record.get("hash"),
            }
        )

    if record_type in {"task.started", "task.terminated"}:
        task_info = record.get("info") if isinstance(record.get("info"), dict) else {}
        return _drop_none(
            {
                "task_id": task_info.get("taskId") or task_info.get("task_id"),
                "kind": task_info.get("kind"),
                "description": _clip(task_info.get("description")),
                "status": task_info.get("status"),
                "agent_id": task_info.get("agentId"),
                "subagent_type": task_info.get("subagentType"),
                "tool_call_id": task_info.get("parentToolCallId"),
                "detached": task_info.get("detached"),
                "timeout_ms": task_info.get("timeoutMs"),
                "exit_code": task_info.get("exitCode"),
                "stop_reason": task_info.get("stopReason"),
                "command": _clip(task_info.get("command")),
                "output_tail": _clip(record.get("outputTail")),
            }
        )

    if record_type == "profile.bind":
        # ``systemPrompt`` is ~24 KB and ``activeToolNames`` is the tool policy;
        # keep the label + policy, drop the prompt body.
        return _drop_none(
            {
                "profile_name": record.get("profileName"),
                "model_alias": record.get("modelAlias"),
                "thinking_effort": record.get("thinkingEffort"),
                "active_tool_names": record.get("activeToolNames"),
                "disallowed_tools": record.get("disallowedTools"),
                "agents_md_paths": record.get("agentsMdPaths"),
                "environment_disclosure": record.get("environmentDisclosure"),
            }
        )

    if record_type == "config.update":
        # Legacy-engine twin of ``profile.bind``, same treatment.
        return _drop_none(
            {
                "profile_name": record.get("profileName"),
                "model_alias": record.get("modelAlias"),
                "thinking_effort": record.get("thinkingEffort"),
                "cwd": record.get("cwd"),
                "subagent_names": record.get("subagentNames"),
            }
        )

    if record_type == "interaction.request":
        # ``{id, kind, toolCallId?, agentId?, request}``
        # (session/interaction/interactionOps.ts:44-56).
        return _drop_none(
            {
                "interaction_id": record.get("id"),
                "interaction_kind": record.get("kind"),
                "tool_call_id": record.get("toolCallId"),
                "agent_id": record.get("agentId"),
                "request": record.get("request"),
            }
        )

    if record_type == "interaction.resolved":
        return _drop_none(
            {
                "interaction_id": record.get("id"),
                "response": record.get("response"),
            }
        )

    # Everything else keeps its payload verbatim minus the envelope fields, so
    # nothing observable is silently dropped.
    return {key: value for key, value in record.items() if key not in {"type", "time"}}


def _loop_event_payload(event: dict[str, Any], *, step_n: int) -> dict[str, Any]:
    event_type = str(event.get("type") or "")

    if event_type == "step.begin":
        # ``n`` is OUR running counter, not ``event.step``: the native value
        # restarts at 1 every turn, so it is useless as a step label / step_id.
        return _drop_none(
            {
                "n": step_n,
                "native_step": event.get("step"),
                "turn_id": event.get("turnId"),
                "step_uuid": event.get("uuid"),
            }
        )

    if event_type == "step.end":
        latency = _drop_none(
            {
                "llm_first_token_ms": event.get("llmFirstTokenLatencyMs"),
                "llm_stream_duration_ms": event.get("llmStreamDurationMs"),
                "llm_request_build_ms": event.get("llmRequestBuildMs"),
                "llm_server_first_token_ms": event.get("llmServerFirstTokenMs"),
                "llm_server_decode_ms": event.get("llmServerDecodeMs"),
                "llm_client_consume_ms": event.get("llmClientConsumeMs"),
            }
        )
        return _drop_none(
            {
                "n": step_n,
                "native_step": event.get("step"),
                "turn_id": event.get("turnId"),
                "step_uuid": event.get("uuid"),
                "finish_reason": event.get("finishReason"),
                "provider_finish_reason": event.get("providerFinishReason"),
                "raw_finish_reason": event.get("rawFinishReason"),
                "message_id": event.get("messageId"),
                "token_usage": event.get("usage"),
                "latency_ms": latency or None,
            }
        )

    if event_type == "content.part":
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        part_type = part.get("type")
        if part_type == "think":
            return {"type": "think", "think": part.get("think")}
        if part_type == "text":
            return {"type": "text", "text": part.get("text")}
        # image_url / audio_url / video_url — the URL is a ``blobref:`` string
        # and stays unrehydrated in v1.
        return {"type": part_type, "part": part}

    if event_type == "tool.call":
        call_id = event.get("toolCallId")
        name = event.get("name")
        args = event.get("args")
        return {
            "id": call_id,
            # Both id keys are set on purpose: app.js accepts either
            # (app.js:1512-1531) but aos_bench/monitor/tasks.py reads ``id`` on
            # tool_call and ``tool_call_id`` on tool_result (tasks.py:149, :178).
            "tool_call_id": call_id,
            "name": name,
            "function": {
                "name": name,
                "arguments": args if isinstance(args, str) else json.dumps(args or {}),
            },
        }

    if event_type == "tool.result":
        call_id = event.get("toolCallId")
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        is_error = bool(result.get("isError"))
        payload: dict[str, Any] = {
            "id": call_id,
            "tool_call_id": call_id,
            # The kimi dict shape, which tasks.py._result_text already handles.
            "return_value": _drop_none(
                {
                    "output": result.get("output"),
                    "is_error": result.get("isError"),
                    "note": result.get("note"),
                }
            ),
        }
        if is_error:
            payload["error"] = True
        return payload

    return {key: value for key, value in event.items() if key != "type"}


def _message_payload(record: dict[str, Any], *, kind: str) -> dict[str, Any]:
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), list) else []
    text = _join_text_parts(content)
    if kind == "tool_result":
        call_id = message.get("toolCallId")
        payload: dict[str, Any] = {
            "id": call_id,
            "tool_call_id": call_id,
            "return_value": {"output": text},
        }
        if message.get("isError"):
            payload["error"] = True
        return payload
    return _drop_none(
        {
            "type": "text",
            "text": text,
            "role": message.get("role"),
            "message_id": message.get("id"),
            "note": message.get("note"),
        }
    )


def _event_metrics(
    kind: str,
    loop_event: dict[str, Any] | None,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Token metrics, from ``step.end`` and out-of-turn ``usage.record``.

    A ``usage.record`` with ``usageScope == "turn"`` repeats its ``step.end``'s
    ``TokenUsage`` (verified in ``live_probe/solo``: identical object on both
    records), so charging it would double every token in
    ``state.push_events``. Any other scope belongs to a request the turn loop
    never made — full compaction runs its summarization through the same
    requester with a non-turn source — and there is no ``step.end`` carrying it,
    so it is charged here or nowhere.

    ``record`` is optional so a caller that only has the loop event keeps
    working; without it only ``step_end`` produces metrics.
    """

    if kind == "step_end" and isinstance(loop_event, dict):
        usage = loop_event.get("usage")
    elif (
        isinstance(record, dict)
        and str(record.get("type") or "") == "usage.record"
        and record.get("usageScope") != "turn"
    ):
        usage = record.get("usage")
    else:
        return {}
    prompt_tokens, completion_tokens = _split_token_usage(usage)
    if not prompt_tokens and not completion_tokens:
        return {}
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}


def _event_extra(
    record: dict[str, Any], loop_event: dict[str, Any] | None
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    origin = record.get("origin")
    if origin is None and isinstance(record.get("message"), dict):
        origin = record["message"].get("origin")
    if isinstance(origin, dict):
        # ``origin.kind == 'system_trigger'`` with ``name == 'subagent'`` is a
        # clean discriminator for subagent-driven turns (contextMemory/types.ts).
        extra["origin_kind"] = origin.get("kind")
        if origin.get("name"):
            extra["origin_name"] = origin.get("name")
        if origin.get("variant"):
            extra["origin_variant"] = origin.get("variant")
    if isinstance(loop_event, dict):
        if loop_event.get("extras"):
            extra["tool_extras"] = loop_event["extras"]
        if loop_event.get("stepUuid"):
            extra["step_uuid"] = loop_event["stepUuid"]
    return extra


def _split_token_usage(usage: Any) -> tuple[int, int]:
    """``TokenUsage`` -> ``(prompt_tokens, completion_tokens)``.

    ``{inputOther, output, inputCacheRead, inputCacheCreation}``
    (``kosong/contract/usage.ts:9-14``).
    """

    if not isinstance(usage, dict):
        return 0, 0
    prompt_tokens = (
        _as_int(usage.get("inputOther"))
        + _as_int(usage.get("inputCacheRead"))
        + _as_int(usage.get("inputCacheCreation"))
    )
    return prompt_tokens, _as_int(usage.get("output"))


# ── AgentTrace assembly ──────────────────────────────────────────────────


def collect_kimi_code_agent_trace(
    session_dir: Path,
    *,
    trace_id: str | None = None,
    model_name: str | None = None,
    agent_version: str = "unknown",
) -> dict[str, Any]:
    """Build an AgentTrace v0.1 dict from one kimi-code session directory.

    ``main`` becomes ``run_root_001`` (role ``root``); every other
    ``agents/<id>`` becomes ``run_sub_<id>`` (role ``subagent``) with
    ``parent_run_id`` from ``state.json``. Runs are created for every agent
    *directory* — never only for agents we saw a spawn marker for — so a lane
    exists even when the spawn record was missed (blueprint §4 P2).
    """

    session = read_kimi_code_session(session_dir)
    session_dir = session.session_dir
    trace_id = trace_id or f"trace_{session_dir.name}_{uuid.uuid4().hex[:8]}"
    notes: list[str] = list(session.notes)

    normalized: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    for agent_id, records in session.agents.items():
        # Spawn synthesis runs for *every* agent, not just the root: nesting is
        # possible (the ``agent`` profile keeps Agent/AgentSwarm) and the child
        # id is recovered exactly from the tool result, so there is no
        # false-positive risk to trade against.
        normalized[agent_id] = normalize_kimi_code_records(
            records,
            run_id=_run_id_for(agent_id),
            synthesize_subagent_spawns=True,
        )
        notes.extend(normalized[agent_id][1]["notes"])

    # Child attributes recovered at whichever parent spawned them.
    spawn_by_agent: dict[str, dict[str, Any]] = {}
    launch_modes: dict[str, str] = {}
    subagent_types: dict[str, str] = {}
    swarm_items: dict[str, Any] = {}
    for _events, info in normalized.values():
        spawn_by_agent.update(info["spawn_events"])
        launch_modes.update(info["child_launch_modes"])
        subagent_types.update(info["child_subagent_types"])
        swarm_items.update(info["child_swarm_items"])

    if _looks_like_legacy_engine(session):
        notes.append(
            "legacy-engine vocabulary detected (config.update / "
            "tools.set_active_tools, no profile.bind) — records read with the "
            "same kind map"
        )

    runs: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = [
        _artifact("native_session_dir", session_dir),
        _artifact("native_session_state", session_dir / "state.json"),
    ]
    background_task_count = 0

    for agent_id, (events, info) in normalized.items():
        run_id = _run_id_for(agent_id)
        agent_dir = session_dir / "agents" / agent_id
        is_root = agent_id == ROOT_AGENT_ID

        task_events, task_artifacts, n_tasks = _background_task_extras(
            agent_dir,
            run_id=run_id,
            info=info,
            seq_start=len(events),
        )
        background_task_count += n_tasks
        events = events + task_events

        spawn = spawn_by_agent.get(agent_id)
        labels = session.labels.get(agent_id) or {}
        run = {
            "run_id": run_id,
            "parent_run_id": None if is_root else _parent_run_id(session, agent_id),
            "source_event_id": None if spawn is None else spawn["event_id"],
            "role": "root" if is_root else "subagent",
            "launch_mode": "inline"
            if is_root
            else launch_modes.get(agent_id, "foreground"),
            # Subagents get their own agent record inside the parent's session
            # dir, i.e. a child execution boundary rather than a shared one.
            "session_mode": "same_session" if is_root else "child_session",
            "agent_name": _agent_name(agent_id, info, labels, is_root=is_root),
            "model": {
                "provider": info["model_provider"],
                "name": info["model_name"] or model_name,
            },
            "status": _run_status(info, events),
            "task": None
            if is_root
            else _run_task(
                agent_id,
                spawn,
                subagent_types=subagent_types,
                swarm_items=swarm_items,
                labels=labels,
            ),
            "timing": {
                "created_at": info["created_at"],
                "started_at": info["started_at"],
                "ended_at": info["ended_at"],
            },
            "resource_limits": {},
            "resource_usage": _drop_none(
                {
                    "wall_time_ms": info["wall_time_ms"],
                    "prompt_tokens": info["prompt_tokens"] or None,
                    "completion_tokens": info["completion_tokens"] or None,
                }
            ),
            "metrics": _drop_none(
                {
                    "n_steps": info["n_steps"] or None,
                    "n_turns": info["n_turns"] or None,
                    # Reasoning emissions, not tokens — see the info dict.
                    "n_thinking_parts": info["n_thinking_parts"] or None,
                }
            ),
            "events": events,
            "artifacts": [
                _artifact("native_agent_wire", agent_dir / "wire.jsonl"),
                *task_artifacts,
            ],
            "extra": _drop_none(
                {
                    "native_session_id": session.session_id,
                    "native_agent_id": agent_id,
                    "native_agent_type": session.agent_types.get(agent_id),
                    "native_profile_name": info["profile_name"],
                    "native_model_alias": info["model_alias"],
                    "native_protocol_version": info["protocol_version"],
                    "native_labels": labels or None,
                    "native_mcp_servers": info["mcp_servers"] or None,
                    "native_paths": {
                        "wire": str(agent_dir / "wire.jsonl"),
                        "state": str(session_dir / "state.json"),
                    },
                }
            ),
        }
        runs.append(run)
        artifacts.extend(run["artifacts"])

    root_info = normalized.get(ROOT_AGENT_ID, ([], {}))[1]
    return {
        "schema_version": "AgentTrace-v0.1",
        "trace_id": trace_id,
        "benchmark": {},
        "agent_system": {
            "name": "kimi-code",
            "version": agent_version,
            "entrypoint": "@moonshot-ai/kimi-code:kimi",
            "default_model": model_name or root_info.get("model_name"),
            "trace_source": "native",
            "extra": _drop_none(
                {
                    "native_session_id": session.session_id,
                    "protocol_version": root_info.get("protocol_version"),
                }
            ),
        },
        "root_run_id": ROOT_RUN_ID,
        "runs": runs,
        "artifacts": artifacts,
        "summary": {
            "subagent_count_total": max(0, len(runs) - 1),
            "background_task_count": background_task_count,
            "mcp_servers": root_info.get("mcp_servers") or [],
        },
        "notes": "\n".join(dict.fromkeys(notes)),
        "extra": {},
    }


def _run_id_for(agent_id: str) -> str:
    return ROOT_RUN_ID if agent_id == ROOT_AGENT_ID else f"run_sub_{agent_id}"


def _parent_run_id(session: KimiCodeSession, agent_id: str) -> str | None:
    parent = session.parents.get(agent_id)
    return None if parent is None else _run_id_for(parent)


def _agent_name(
    agent_id: str,
    info: dict[str, Any],
    labels: dict[str, Any],
    *,
    is_root: bool,
) -> str:
    """Human lane label.

    The root is always ``orchestrator``, matching the other arms' root labels
    (``aos_bench/monitor/tail/kimi.py:225``, ``claude.py:147``) — every
    kimi-code root binds the profile ``agent``, which says nothing. Children use
    their own profile name (``agent`` / ``coder`` / ``explore`` / ``plan`` —
    ``agent/profile/profiles.ts:101-137``), suffixed with the swarm item when
    they are one lane of a swarm, and fall back to ``agentId[:24]`` (claude's
    fallback, ``claude.py:184``). The raw profile name always stays in the run's
    ``extra.native_profile_name``.
    """

    if is_root:
        return "orchestrator"
    profile_name = info.get("profile_name")
    if isinstance(profile_name, str) and profile_name:
        item = labels.get("swarmItem")
        if item:
            return f"{profile_name}:{item}"[:48]
        return profile_name[:48]
    return agent_id[:24]


def _run_status(info: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """AgentTrace ``status`` for one agent run.

    ``turn.ended.reason`` is the authority when present
    (``completed`` / ``cancelled`` / ``failed`` / ``blocked`` —
    ``agent/loop/turnOps.ts:82-97``). No ``turn.ended`` record was ever observed
    from the legacy engine, so a journal without one falls back to the last
    ``step.end.finishReason``: ``end_turn`` means the loop stopped on its own.

    Both inputs are per-turn rather than sticky (see
    :func:`normalize_kimi_code_records`): ``turn.ended`` fires at the end of
    *every* turn, so a run that opened turn N+1 after turn N completed reports
    ``running`` — which is what keeps a live orchestrator lane from reading
    ``completed`` for the rest of a trial, and what stops a journal SIGKILLed
    mid-turn from being recorded ``completed`` because its first turn ended
    cleanly.
    """

    reason = info.get("last_turn_reason")
    if reason == "completed":
        return "completed"
    if reason in {"failed", "blocked"}:
        return "failed"
    if reason == "cancelled":
        return "killed"
    if info.get("last_finish_reason") == "end_turn":
        return "completed"
    if events:
        return "running"
    return "created"


def _run_task(
    agent_id: str,
    spawn: dict[str, Any] | None,
    *,
    subagent_types: dict[str, str],
    swarm_items: dict[str, Any],
    labels: dict[str, Any],
) -> dict[str, Any]:
    """The subtask a child run carries, joined from its spawn site.

    ``labels.swarmItem`` from ``state.json`` is the last fallback for the swarm
    item, since it survives even when the spawning tool call was never matched.
    """

    payload = (spawn or {}).get("payload") or {}
    return _drop_none(
        {
            "description": payload.get("description"),
            "prompt": payload.get("prompt"),
            "task_id": agent_id,
            "subagent_type": payload.get("subagent_type")
            or subagent_types.get(agent_id),
            "swarm_item": (
                payload.get("swarm_item")
                or swarm_items.get(agent_id)
                or labels.get("swarmItem")
            ),
        }
    )


def _background_task_extras(
    agent_dir: Path,
    *,
    run_id: str,
    info: dict[str, Any],
    seq_start: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Background-task events + artifacts for one agent.

    Ownership is the *directory* — ``<agentDir>/tasks/<taskId>.json`` plus
    ``<taskId>/output.log`` (``agent/task/persist.ts:5-31``) — so none of
    kimi-cli's owner-resolution precedence chain
    (``aos_bench/trace/kimi_cli.py:443-472``) is needed here.

    ``task.started`` / ``task.terminated`` already produced ``background_task``
    events during normalization, so the sidecars only add (a) artifacts and
    (b) synthetic events for task ids the journal never mentioned — which is
    what a crash or a torn final line looks like.
    """

    tasks_dir = agent_dir / "tasks"
    if not tasks_dir.is_dir():
        return [], [], len(info["background_tasks"])

    events: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    seq = seq_start
    seen = set(info["background_tasks"])

    for spec_path in sorted(tasks_dir.glob("*.json")):
        task = _read_json(spec_path)
        task_id = task.get("taskId") or task.get("task_id") or spec_path.stem
        artifacts.append(_artifact("native_background_task", spec_path))
        output_log = tasks_dir / str(task_id) / "output.log"
        if output_log.is_file():
            artifacts.append(_artifact("native_background_task_output", output_log))
        if task_id in seen:
            continue
        seen.add(str(task_id))
        seq += 1
        events.append(_sidecar_task_event(task, run_id=run_id, seq=seq, path=spec_path))

    return events, artifacts, len(seen)


def _sidecar_task_event(
    task: dict[str, Any],
    *,
    run_id: str,
    seq: int,
    path: Path,
) -> dict[str, Any]:
    """One ``background_task`` event rebuilt from a task sidecar JSON.

    Legacy snake_case sidecars are normalized on read upstream
    (``agent/task/persist.ts:248-279``); accept both spellings here too.
    """

    started_at = _ms_to_seconds(task.get("startedAt") or task.get("started_at"))
    ended_at = _ms_to_seconds(task.get("endedAt") or task.get("ended_at"))
    wall_time_ms = None
    if started_at is not None and ended_at is not None:
        wall_time_ms = int(round((ended_at - started_at) * 1000))
    status = task.get("status")
    return {
        "event_id": f"{run_id}_evt_{seq:04d}",
        "seq": seq,
        "timestamp": ended_at or started_at,
        "kind": "background_task",
        "phase": "completed" if ended_at is not None else "begin",
        "step_id": None,
        "parent_event_id": None,
        "payload": _drop_none(
            {
                "task_id": task.get("taskId") or task.get("task_id"),
                "kind": task.get("kind"),
                "description": _clip(task.get("description")),
                "status": status,
                "agent_id": task.get("agentId") or task.get("agent_id"),
                "subagent_type": task.get("subagentType") or task.get("subagent_type"),
                "detached": task.get("detached"),
                "timeout_ms": task.get("timeoutMs") or task.get("timeout_ms"),
                "exit_code": task.get("exitCode") or task.get("exit_code"),
                "stop_reason": task.get("stopReason") or task.get("stop_reason"),
                "command": _clip(task.get("command")),
            }
        ),
        "metrics": _drop_none({"wall_time_ms": wall_time_ms}),
        "raw_ref": {
            "source": "kimi-code-task",
            "path": str(path),
            "task_id": task.get("taskId") or task.get("task_id"),
        },
        "extra": {},
    }


def _looks_like_legacy_engine(session: KimiCodeSession) -> bool:
    """True when a journal speaks the legacy vocabulary.

    Legacy writes ``config.update`` ×2 + ``tools.set_active_tools`` and never
    ``profile.bind``; the default v2 engine is the other way round. Both land in
    the same directory layout, so the vocabulary is the only tell.
    """

    for records in session.agents.values():
        types = {str(record.get("type") or "") for record in records}
        if not types:
            continue
        if "profile.bind" in types:
            return False
        if "config.update" in types or "tools.set_active_tools" in types:
            return True
    return False


# ── Small helpers ────────────────────────────────────────────────────────


def _protocol_version(records: list[dict[str, Any]]) -> str | None:
    for record in records:
        if record.get("type") == "metadata":
            version = record.get("protocol_version")
            return version if isinstance(version, str) else None
    return None


def _records_created_at(records: list[dict[str, Any]]) -> float | None:
    for record in records:
        if record.get("type") == "metadata":
            return _ms_to_seconds(record.get("created_at"))
    return None


def _state_timestamp(value: Any) -> float | None:
    """``state.json`` timestamps: epoch-ms ints (v2) or ISO strings (legacy)."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _ms_to_seconds(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _ms_to_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) / 1000.0


def _parse_version(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in value.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def _join_text_parts(parts: Any) -> str:
    """Concatenate the ``text`` of every text part in a ContentPart list."""

    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "".join(chunks)


def _result_output_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    return _join_text_parts(result.get("output"))


def _clip(value: Any, limit: int = 400) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= limit else value[:limit]


def _drop_none(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in mapping.items() if value is not None}


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _artifact(kind: str, path: Path) -> dict[str, Any]:
    return {"kind": kind, "path": str(path)}
