"""Incremental parser for kimi-code journals (``agents/<id>/wire.jsonl``).

This module has the same relationship to ``aos_bench/trace/kimi_code.py`` that
``aos_bench/monitor/tail/parser.py:1-12`` has to ``aos_bench/trace/kimi_cli.py``
and ``claude_parser.py:14-33`` has to claude's offline converter: the offline
collector walks a finished journal in one pass, while the live monitor must feed
records as they land and carry ``seq`` / ``step_id`` / token totals across
``feed()`` calls.

Mirrored by import, not by copy
-------------------------------
``aos_bench/trace/kimi_code.py`` is the *single source of truth* for the
kimi-code record vocabulary, and the vocabulary is 60-odd record types wide with
a per-type payload contract. So instead of restating it (``parser.py:20-38``
restates kimi-cli's 15-entry table, which is small enough to keep in sync by
eye), this module imports ``map_wire_kind`` and the payload / phase / metrics /
extra builders from the trace module. The two therefore *cannot* drift, and the
events the dashboard sees live are byte-identical to the ones the post-run
AgentTrace records for the same journal — same ``event_id``, ``seq``,
``payload``, ``metrics``, ``raw_ref`` — with exactly three deliberate
differences:

1. ``MONITOR_KIND_OVERRIDES`` downgrades the two AgentTrace kinds the dashboard
   cannot render: ``reasoning`` → ``message`` and ``background_task`` →
   ``status``. The monitor's kind set is closed (``parser.py:20-38``); anything
   outside it lands in the frontend's ``default`` arm (``app.js:1577``). Only
   ``kind`` is rewritten — ``phase`` is still derived from the AgentTrace kind,
   so a ``task.started`` / ``task.terminated`` pair keeps its ``begin`` /
   ``completed`` shape instead of flattening to ``update``.
2. Spawn synthesis is deduped by the ``tool.call`` loop event's ``uuid``, or by
   its tool-call id when it has none (the offline reader sees every record
   exactly once; a truncated or rotated file makes ``_FileTail`` reset to byte 0
   and replay, so the live parser can see the same ``Agent`` call twice).
3. Records this parser has not seen yet cannot be back-numbered, so ``seq``
   restarts nowhere: it is a single running counter for the life of the parser,
   which is one parser per *run* (root or one subagent directory).

Everything the frontend reads literally (blueprint §4 P9-P13) is inherited from
the trace module's payload contract: ``message`` carries ``type`` +
``text`` / ``think``; ``tool_call`` carries ``function.name`` and both ``id``
*and* ``tool_call_id``; ``tool_result`` repeats the same id keys and puts the
body in the kimi dict shape ``return_value = {"output": …}`` that
``aos_bench/monitor/tasks.py:115-124`` already understands; ``step_begin``
carries ``n``; ``token_counting.*`` statuses carry ``context_tokens``; failed
tool results and failed turns carry ``error: True`` so
``aos_bench/monitor/errors.py:51-57`` fires.

Timestamps
----------
``time`` is epoch **milliseconds** and is optional on a record, so it is divided
by 1000 and the last known value is carried forward — events with a non-float
timestamp are silently dropped by the SVG renderer (``app.js:545``), which would
make a lane look *empty* rather than wrong (blueprint §4 P11).

Spawn markers and the back-fill
-------------------------------
``Agent`` / ``AgentSwarm`` tool calls emit ``subagent_spawn`` immediately, with
``child_run_id: None`` because the child agent id only arrives with the tool
*result* (or with ``task.started`` for a background delegation). The id is then
back-filled **into the same dict object** the tail already handed to
``MonitorState.push_events``: ``state.py:342`` stores the event by reference and
``to_detail_dict`` re-serializes on every API read, so the spawn arrow
(``app.js:1528-1540``) gains its link on the next poll without a second event.
Counters and error scanning already ran off that event and are unaffected — only
inert payload fields change.

Notifications, and why there is no ``notification`` kind here
-------------------------------------------------------------
kimi-code has no notification wire record. Notifications reach the model as an
injected ``turn.prompt`` whose text is a ``<notification … Severity: info>``
block (see the ``live_probe/background`` fixture), so the shared
``notification_severe`` rule (``errors.py:103-115``, blueprint §4 P8) has no
source on this arm and four of the five rules fire instead. Synthesizing a
``notification`` event from that text was considered and rejected: every
notification observed on the real CLI is ``Severity: info`` — which the rule
ignores anyway — and an extra synthesized event would shift ``seq`` away from
the offline converter, giving up the byte-identity property above for no
detection we can demonstrate. If it is ever wanted, the hook is a
``turn.prompt`` whose ``origin.kind == "task"`` and whose joined input text
matches ``Severity:\\s*(warning|error)``.
"""

from __future__ import annotations

from typing import Any

# Imported from the offline converter on purpose — see the module docstring.
# The private helpers are pure functions over one record; sharing them is what
# keeps the live dashboard and the post-run AgentTrace describing the same run
# in the same words. A rename upstream breaks this import loudly at collection
# time (``aos_bench/monitor/tests/test_kimi_code_parser.py``), which is the
# intended failure mode — a silently forked copy would drift instead.
from aos_bench.trace.kimi_code import (
    MONITOR_KIND_OVERRIDES,
    _absorb_task_record,
    _absorb_tool_result,
    _event_extra,
    _event_metrics,
    _event_payload,
    _event_phase,
    _ms_to_seconds,
    _split_token_usage,
    _synthesize_spawn_events,
    map_wire_kind,
    parse_kimi_code_wire_line,
)

__all__ = ["IncrementalKimiCodeParser", "parse_kimi_code_line"]


def parse_kimi_code_line(line: str) -> dict[str, Any] | None:
    """Parse one ``wire.jsonl`` line into a flat record, or ``None`` to skip.

    Delegates to the trace module so the live tail and the offline reader accept
    and reject exactly the same lines (blank, non-JSON, non-dict, or missing the
    ``type`` field — the last of which is how a torn final line looks; kimi-code
    appends with ``O_APPEND`` + fsync, so only the last line can ever be torn).

    Unlike ``parse_wire_line`` (``parser.py:241-258``) the ``metadata`` header is
    **not** dropped here: it carries ``protocol_version`` (the legacy-vs-default
    engine tell) and ``created_at``, which :class:`IncrementalKimiCodeParser`
    records as run metadata. It still never becomes an event —
    ``map_wire_kind`` maps it to ``""``.
    """

    return parse_kimi_code_wire_line(line)


class IncrementalKimiCodeParser:
    """Feed kimi-code wire records and emit normalized monitor events.

    One parser per *run*: the root (``agents/main`` → ``run_root_001``) or one
    subagent directory (``agents/agent-0`` → ``run_sub_agent-0``). kimi-code
    splits agents *physically* — there is no ``isSidechain`` flag inside records
    — so the directory is the run boundary and no sidechain routing is needed
    (contrast ``claude_parser.py:28-33``).

    State carried across :meth:`feed` calls:

    - ``seq`` — monotonic event sequence within the run (drives ``event_id``).
    - ``step_n`` / ``step_id`` — our own running step counter. The native
      ``event.step`` restarts at 1 on every turn, so it cannot label steps; it is
      preserved as ``payload.native_step`` by the shared payload builder.
    - ``prompt_tokens`` / ``completion_tokens`` — from ``step.end`` plus any
      ``usage.record`` whose scope is not ``turn``. A turn-scoped
      ``usage.record`` repeats the identical ``TokenUsage`` object, so charging
      both would double every token in ``state.py:331-337``; an out-of-turn one
      (full compaction) has no ``step.end`` and would otherwise be lost.
    - ``n_turns`` / ``last_turn_reason`` / ``last_finish_reason`` — run status
      inputs for the tail (``turn.ended`` is the authority; the legacy engine
      never writes one, hence the finish-reason fallback). Both reasons are
      **per-turn**: a new ``turn.prompt`` / ``turn.steer`` / ``step.begin``
      clears them, so a lane that finished turn N and started turn N+1 reads
      ``running`` again instead of latching to ``completed``.
    - ``profile_name`` / ``model_alias`` / ``model_name`` / ``model_provider`` —
      lane label + model identity (blueprint §4 P3), so the tail does not have to
      re-read the journal to label a lane.
    - ``protocol_version`` / ``created_at`` — from the ``metadata`` header.
    - ``mcp_servers`` — server names from ``mcp.tools_discovered``. Note that the
      default (v2) engine emits **no** such record even when the server connects,
      so an empty list is not evidence that ``aos_pool`` failed to wire up.
    - ``tool_call_owners`` — ``{tool_call_id: run_id}``, mirroring
      ``parser.py:98``.
    - ``spawn_events`` / ``child_launch_modes`` / ``child_subagent_types`` /
      ``child_swarm_items`` / ``background_tasks`` — exact join results keyed by
      child agent id (and task id), filled in as soon as a tool result or a
      ``task.started`` names the child. The tail reads these to wire a freshly
      discovered ``agents/<id>/`` directory to the spawn that created it, the way
      ``CodexSessionTail`` drains ``parser.spawn_map`` (``codex.py:249``).
    - ``first_ts`` / ``last_ts`` — first and last seen timestamps; ``last_ts``
      doubles as the carry-forward value for records with no ``time``.
    """

    def __init__(
        self, *, run_id: str, synthesize_subagent_spawns: bool = False
    ) -> None:
        self.run_id = run_id
        self.synthesize = synthesize_subagent_spawns
        self.seq = 0
        self.step_n = 0
        self.step_id: str | None = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.n_turns = 0
        self.last_turn_reason: str | None = None
        self.last_finish_reason: str | None = None
        self.profile_name: str | None = None
        self.model_alias: str | None = None
        self.model_name: str | None = None
        self.model_provider: str | None = None
        self.protocol_version: str | None = None
        self.created_at: float | None = None
        self.mcp_servers: list[str] = []
        self.tool_call_owners: dict[str, str] = {}
        self.first_ts: float | None = None
        self.last_ts: float | None = None

        # Exact tool-call → child-agent joins. These are the dicts the shared
        # ``_absorb_*`` helpers write into; ``_join_info`` is a *view* onto them
        # (the helpers only mutate the nested dicts, never rebind the keys), so
        # the public attributes stay live without a copy step.
        self.spawn_events: dict[str, dict[str, Any]] = {}
        self.child_launch_modes: dict[str, str] = {}
        self.child_subagent_types: dict[str, str] = {}
        self.child_swarm_items: dict[str, Any] = {}
        self.background_tasks: dict[str, dict[str, Any]] = {}
        self._join_info: dict[str, Any] = {
            "spawn_events": self.spawn_events,
            "child_launch_modes": self.child_launch_modes,
            "child_subagent_types": self.child_subagent_types,
            "child_swarm_items": self.child_swarm_items,
            "background_tasks": self.background_tasks,
        }
        # tool-call id -> the spawn events awaiting a child agent id.
        self._pending_spawns: dict[str, list[dict[str, Any]]] = {}
        # Loop-event uuids (or tool-call ids, for events without one) we already
        # synthesized spawn events for. A replay after file truncation must not
        # draw a second arrow for the same call (``parser.py:172-180``,
        # ``claude_parser.py:309-315``).
        self.synthesized_spawns: set[str] = set()

    def feed(self, raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert raw wire records to normalized monitor events.

        Returns only the newly produced events; the caller pushes them into
        ``MonitorState`` and hands them to the error detector. Records that
        ``map_wire_kind`` maps to ``""`` (the ``metadata`` header,
        ``llm.request`` / ``llm.tools_snapshot`` — ~95 KB of a 101 KB journal —
        and ``context.append_message`` with ``role == "assistant"``, which the
        loop events already cover) are consumed for state and **do not consume a
        ``seq``**, which is what keeps the numbering aligned with the offline
        converter.
        """

        out: list[dict[str, Any]] = []
        for record in raw_records:
            record_type = str(record.get("type") or "")

            if record_type == "metadata":
                # Header line: run metadata, never an event.
                self.created_at = _ms_to_seconds(record.get("created_at"))
                version = record.get("protocol_version")
                if isinstance(version, str):
                    self.protocol_version = version
                continue

            ts = _ms_to_seconds(record.get("time"))
            if ts is None:
                ts = self.last_ts  # ``time`` is optional on WireRecord
            else:
                self.last_ts = ts
            if self.first_ts is None and ts is not None:
                self.first_ts = ts

            loop_event = (
                record.get("event")
                if record_type == "context.append_loop_event"
                else None
            )
            loop_type = loop_event.get("type") if isinstance(loop_event, dict) else None

            # ── side effects, before any drop decision ───────────────────
            if record_type in {"profile.bind", "config.update"}:
                # v2 binds a profile; the legacy engine spells it config.update.
                # Either way this is the lane-label source (blueprint §4 P3).
                profile_name = record.get("profileName")
                if isinstance(profile_name, str) and profile_name:
                    self.profile_name = profile_name
                alias = record.get("modelAlias")
                if isinstance(alias, str) and alias:
                    self.model_alias = alias
            elif record_type == "llm.request":
                model = record.get("model")
                if isinstance(model, str) and model:
                    self.model_name = model
                provider = record.get("provider")
                if isinstance(provider, str) and provider:
                    self.model_provider = provider
            elif record_type == "mcp.tools_discovered":
                server = record.get("serverName")
                if (
                    isinstance(server, str)
                    and server
                    and server not in self.mcp_servers
                ):
                    self.mcp_servers.append(server)
            elif record_type == "usage.record":
                # Only an out-of-turn record is real extra spend: a
                # ``usageScope == "turn"`` record repeats its ``step.end``'s
                # ``TokenUsage`` byte-for-byte, while a compaction request
                # (scope ``session``) never produces a ``step.end`` at all. Same
                # rule as the offline collector — see
                # ``normalize_kimi_code_records``.
                if record.get("usageScope") != "turn":
                    prompt_tokens, completion_tokens = _split_token_usage(
                        record.get("usage")
                    )
                    self.prompt_tokens += prompt_tokens
                    self.completion_tokens += completion_tokens
            elif record_type == "turn.ended":
                reason = record.get("reason")
                if isinstance(reason, str):
                    self.last_turn_reason = reason
                self.n_turns += 1
            elif record_type in {"turn.prompt", "turn.steer"}:
                # A new / steered turn: the two status signals describe the turn
                # that just ended, not the run. ``turn.ended`` fires once per
                # TURN, so without this reset the lane latches to ``completed``
                # at the first turn boundary and never comes back — see
                # ``normalize_kimi_code_records`` and blueprint §4 P4.
                self.last_turn_reason = None
                self.last_finish_reason = None
            elif record_type in {"task.started", "task.terminated"}:
                _absorb_task_record(
                    record, info=self._join_info, pending_spawns=self._pending_spawns
                )
            elif loop_type == "step.begin":
                # ``step_begin`` is mandatory: ``n_llm_calls`` derives from it
                # (state.py:327-328) and a parser without it shows llm=0 in every
                # lane header (app.js:1698).
                self.step_n += 1
                self.step_id = f"{self.run_id}_step_{self.step_n}"
                # Same per-turn reset as ``turn.prompt``: new work started, so
                # the previous turn's / step's terminal reasons no longer
                # describe this lane (the legacy engine writes no
                # ``turn.ended`` at all, so this is its only reset).
                self.last_turn_reason = None
                self.last_finish_reason = None
            elif loop_type == "step.end":
                prompt_tokens, completion_tokens = _split_token_usage(
                    loop_event.get("usage") if isinstance(loop_event, dict) else None
                )
                self.prompt_tokens += prompt_tokens
                self.completion_tokens += completion_tokens
                finish_reason = (
                    loop_event.get("finishReason")
                    if isinstance(loop_event, dict)
                    else None
                )
                if isinstance(finish_reason, str):
                    self.last_finish_reason = finish_reason
            elif loop_type == "tool.call" and isinstance(loop_event, dict):
                tool_call_id = loop_event.get("toolCallId")
                if isinstance(tool_call_id, str) and tool_call_id:
                    self.tool_call_owners[tool_call_id] = self.run_id
                if self.synthesize:
                    out.extend(self._synthesize_spawns(loop_event, ts=ts))
            elif loop_type == "tool.result" and isinstance(loop_event, dict):
                # Exact back-fill of the child agent id into the spawn event(s)
                # this result answers — no nearest-earlier-time heuristic.
                _absorb_tool_result(
                    loop_event,
                    info=self._join_info,
                    pending_spawns=self._pending_spawns,
                )

            agent_kind = map_wire_kind(record)
            if not agent_kind:
                continue
            # Only ``kind`` is downgraded for the dashboard; ``phase`` keeps the
            # AgentTrace kind's begin/update/completed semantics.
            kind = MONITOR_KIND_OVERRIDES.get(agent_kind, agent_kind)

            self.seq += 1
            out.append(
                {
                    "event_id": f"{self.run_id}_evt_{self.seq:04d}",
                    "seq": self.seq,
                    "timestamp": ts,
                    "kind": kind,
                    "phase": _event_phase(record_type, loop_type, agent_kind),
                    "step_id": self.step_id,
                    "parent_event_id": None,
                    "payload": _event_payload(
                        record, kind=agent_kind, step_n=self.step_n
                    ),
                    "metrics": _event_metrics(agent_kind, loop_event, record),
                    "raw_ref": {
                        "source": "kimi-code-wire",
                        "path": None,
                        "message_type": loop_type or record_type,
                    },
                    "extra": _event_extra(record, loop_event),
                }
            )
        return out

    def _synthesize_spawns(
        self, loop_event: dict[str, Any], *, ts: float | None
    ) -> list[dict[str, Any]]:
        """``subagent_spawn`` events for one ``Agent`` / ``AgentSwarm`` call.

        ``AgentSwarm`` fans out one child per ``items[]`` entry, so it gets one
        spawn per item and the lane count matches. Returns ``[]`` for every other
        tool, and for a call we already spawned off — the offline reader sees
        each record once, but ``_FileTail`` re-reads from byte 0 after a
        truncation, so the live parser must dedup.

        The dedup key is the loop event's ``uuid`` (the loop service mints one
        per step) and only falls back to ``toolCallId``: the id is
        provider-generated and only unique *within* a step, so keying the dedup
        on it made a second, genuinely different ``Agent`` call that reused
        ``call_0`` disappear — one arrow drawn, pointing at the wrong child.
        ``uuid`` is stable across a replay of the same bytes, which is the only
        property the dedup actually needs.
        """

        uuid_value = loop_event.get("uuid")
        call_id = loop_event.get("toolCallId")
        dedup_key = uuid_value if isinstance(uuid_value, str) and uuid_value else None
        if dedup_key is None:
            dedup_key = call_id if isinstance(call_id, str) else ""
        call_key = call_id if isinstance(call_id, str) else ""
        if dedup_key and dedup_key in self.synthesized_spawns:
            return []

        spawned = _synthesize_spawn_events(
            loop_event,
            run_id=self.run_id,
            timestamp=ts,
            step_id=self.step_id,
            seq_start=self.seq,
        )
        if not spawned:
            return []

        self.seq += len(spawned)
        if dedup_key:
            # A call with neither uuid nor id cannot be deduped (and cannot be
            # back-filled either) — better a duplicate arrow than a dropped
            # lane.
            self.synthesized_spawns.add(dedup_key)
        # Park them so the matching ``tool.result`` / ``task.started`` can
        # back-fill ``child_run_id`` in place (see the module docstring).
        self._pending_spawns.setdefault(call_key, []).extend(spawned)
        return spawned
