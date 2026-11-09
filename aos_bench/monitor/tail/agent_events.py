"""Live tail of the in-house SDK harness ``events.jsonl``.

``aos_bench/harness/claude_code/`` is a Claude-Agent-SDK solo harness. Unlike
harbor's native ``claude_code`` adapter — which lets Claude Code
write its own ``sessions/projects/<p>/<sess>/*.jsonl`` transcript — the SDK
harness records its trajectory through the ``Coordinator`` event log at
``<trial>/agent/events.jsonl`` (see
``aos_bench/harness/claude_code/core/coordinator.py:_log`` and
``aos_bench/harness/claude_code/engines/claude/session.py:drain_turn``). The SDK's own
``CLAUDE_CONFIG_DIR`` (``agent/sessions/<role>/projects/``) stays empty of
transcripts in this mode, so the CC ``.jsonl`` tail sees nothing — this tail
covers that gap.

Event kinds in ``events.jsonl`` (one JSON object per line, with ``ts`` epoch
seconds + ``iso``):

    solo_start    {model, pool, cpu_budget, mem_budget_mb}     → status (run meta)
    assistant_text{who, text}                                  → message
    tool_use      {who, name, input}                           → tool_call
    tool_result   {who, tool_use_id}                           → tool_result
    result        {who, subtype, num_turns, duration_ms,
                   total_cost_usd, usage}                       → status (+ tokens)
    solo_end      {wall_time_sec}                               → (finalize)
    send_message / deliver / spawn_worker (team modes)          → message / subagent_spawn

``who`` is the agent name: ``"solo"`` for solo runs; ``"lead"`` / ``"worker1"``
… for team runs. We route each record to a run by ``who`` — the lead/solo lane
is the root, every other ``who`` is a subagent lane spawned off it — so the same
dashboard renders an SDK run like a kimi / claude-code one. Each ``who`` gets one
:class:`IncrementalAgentEventsParser`.

Tail is poll-based (byte-offset re-read), identical to the other tails.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aos_bench.monitor.errors import ErrorDetector
from aos_bench.monitor.state import MonitorState
from aos_bench.monitor.tail.kimi import _FileTail
from aos_bench.monitor.tail.workflow import WorkflowFanoutTracker

ROOT_RUN_ID = "run_root_001"
# The lead / solo agent occupies the root lane; any other `who` is a subagent.
_ROOT_WHO = frozenset({"solo", "lead"})
# tool names that spawn a child agent in the SDK harness (team mode).
_SPAWN_TOOL_NAMES = frozenset({"spawn_worker"})


def _find_events_files(trial_dir: Path) -> list[Path]:
    """All ``events.jsonl`` under a trial dir (normally exactly one at
    ``agent/events.jsonl``; glob tolerates batch nesting)."""
    direct = trial_dir / "agent" / "events.jsonl"
    if direct.is_file():
        return [direct]
    return sorted(trial_dir.glob("**/agent/events.jsonl"))


class IncrementalAgentEventsParser:
    """Feed ``events.jsonl`` records for ONE run (``who``) → monitor events.

    Mirrors the shape produced by the kimi / claude parsers (``kind`` / ``phase``
    / ``step_id`` / ``payload`` / ``metrics``) so state, errors, and the SVG
    timeline render an SDK run unchanged. One ``result`` event per assistant
    turn doubles as the ``step_begin`` (LLM-call) boundary.
    """

    def __init__(self, *, run_id: str, synthesize_subagent_spawns: bool = False) -> None:
        self.run_id = run_id
        self.synthesize = synthesize_subagent_spawns
        self.seq = 0
        self.step_id: str | None = None
        self.step_n = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self.synthesized_spawns: set[str] = set()

    def _emit(
        self,
        *,
        kind: str,
        phase: str,
        ts: float | None,
        payload: dict[str, Any],
        metrics: dict[str, Any] | None,
        rec_kind: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.seq += 1
        return {
            "event_id": f"{self.run_id}_evt_{self.seq:04d}",
            "seq": self.seq,
            "timestamp": ts,
            "kind": kind,
            "phase": phase,
            "step_id": self.step_id,
            "parent_event_id": None,
            "payload": payload,
            "metrics": metrics or {},
            "raw_ref": {"source": "sdk-events-jsonl", "message_type": rec_kind},
            "extra": extra or {},
        }

    @staticmethod
    def _usage_metrics(usage: Any) -> dict[str, Any]:
        # ``result.usage`` is the Anthropic usage dict.
        if not isinstance(usage, dict):
            return {}
        prompt = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        )
        completion = int(usage.get("output_tokens", 0) or 0)
        return {"prompt_tokens": prompt, "completion_tokens": completion}

    def feed(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rec in records:
            kind = rec.get("kind")
            ts = rec.get("ts")
            ts = float(ts) if isinstance(ts, (int, float)) else None
            if ts is not None:
                if self.first_ts is None:
                    self.first_ts = ts
                self.last_ts = ts

            if kind == "assistant_text":
                # Each assistant utterance marks (the visible start of) one LLM
                # turn. The SDK's single end-of-session ResultMessage can't be
                # the per-turn boundary, so we open a step here — making
                # ``n_llm_calls`` track assistant turns, like the other tails.
                self.step_n += 1
                self.step_id = f"{self.run_id}_step_{self.step_n}"
                text = rec.get("text")
                out.append(
                    self._emit(
                        kind="step_begin",
                        phase="begin",
                        ts=ts,
                        payload={"n": self.step_n},
                        metrics=None,
                        rec_kind="assistant_text",
                    )
                )
                out.append(
                    self._emit(
                        kind="message",
                        phase="completed",
                        ts=ts,
                        payload={"type": "text", "text": text or "", "role": "assistant"},
                        metrics=None,
                        rec_kind="assistant_text",
                    )
                )
            elif kind == "tool_use":
                name = rec.get("name") or ""
                raw_input = rec.get("input")
                if isinstance(raw_input, str):
                    try:
                        args = json.loads(raw_input)
                    except (json.JSONDecodeError, ValueError):
                        args = {"raw": raw_input}
                elif isinstance(raw_input, dict):
                    args = raw_input
                else:
                    args = {}
                if (
                    self.synthesize
                    and name in _SPAWN_TOOL_NAMES
                    and name not in self.synthesized_spawns
                ):
                    out.append(
                        self._emit(
                            kind="subagent_spawn",
                            phase="begin",
                            ts=ts,
                            payload={
                                "child_run_id": None,
                                "subagent_type": "worker",
                                "task_id": None,
                                "launch_mode": "background",
                            },
                            metrics=None,
                            rec_kind="tool_use:spawn_worker",
                        )
                    )
                    self.synthesized_spawns.add(name)
                out.append(
                    self._emit(
                        kind="tool_call",
                        phase="begin",
                        ts=ts,
                        payload={
                            "id": None,
                            "tool_call_id": None,
                            "function": {"name": name, "arguments": args},
                            "name": name,
                        },
                        metrics=None,
                        rec_kind="tool_use",
                    )
                )
            elif kind == "tool_result":
                call_id = rec.get("tool_use_id")
                out.append(
                    self._emit(
                        kind="tool_result",
                        phase="completed",
                        ts=ts,
                        payload={
                            "id": call_id,
                            "tool_call_id": call_id,
                            "return_value": "",  # events.jsonl records id only, no body
                        },
                        metrics=None,
                        rec_kind="tool_result",
                    )
                )
            elif kind == "result":
                # The SDK emits ONE ResultMessage at end-of-session carrying the
                # cumulative usage + num_turns. It is NOT a per-turn boundary, so
                # don't open a step here (assistant_text does that) — just record
                # the cumulative token usage + run metadata as a status event.
                metrics = self._usage_metrics(rec.get("usage"))
                if metrics:
                    self.prompt_tokens += int(metrics.get("prompt_tokens", 0) or 0)
                    self.completion_tokens += int(metrics.get("completion_tokens", 0) or 0)
                out.append(
                    self._emit(
                        kind="status",
                        phase="update",
                        ts=ts,
                        payload={
                            "num_turns": rec.get("num_turns"),
                            "duration_ms": rec.get("duration_ms"),
                            "total_cost_usd": rec.get("total_cost_usd"),
                            "token_usage": rec.get("usage"),
                        },
                        metrics=metrics or None,
                        rec_kind="result",
                    )
                )
            elif kind in ("solo_start", "solo_end", "team_start", "team_end"):
                out.append(
                    self._emit(
                        kind="status",
                        phase="update",
                        ts=ts,
                        payload={k: v for k, v in rec.items() if k not in ("ts", "iso")},
                        metrics=None,
                        rec_kind=str(kind),
                    )
                )
            # send_message / deliver / spawn_worker (router events) carry the
            # coordination story; surface as plain messages so they show on the
            # lane without inventing kinds the frontend doesn't render.
            elif kind in ("send_message", "deliver", "spawn_worker"):
                out.append(
                    self._emit(
                        kind="message",
                        phase="completed",
                        ts=ts,
                        payload={
                            "type": "text",
                            "text": f"[{kind}] " + json.dumps(
                                {k: v for k, v in rec.items() if k not in ("ts", "iso", "kind")},
                                ensure_ascii=False,
                            )[:400],
                            "role": "system",
                        },
                        metrics=None,
                        rec_kind=str(kind),
                    )
                )
        return out


class AgentEventsTail:
    """Async tailer for one trial's SDK-harness ``events.jsonl``."""

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
        self._tails: dict[Path, _FileTail] = {}
        self._parsers: dict[str, IncrementalAgentEventsParser] = {}
        self._who_runs: dict[str, str] = {}
        # An SDK-harness agent can still drive Claude Code's Workflow tool, and
        # that fan-out is invisible in events.jsonl — the SDK only sees the one
        # `who` that made the call, so a 8-agent Workflow shows up as a single
        # lane. The transcripts are on disk under the container's
        # CLAUDE_CONFIG_DIR; this tracker turns them into real lanes.
        self._workflow = WorkflowFanoutTracker(
            trial_id=trial_id,
            state=state,
            error_detector=error_detector,
            parent_run_id=ROOT_RUN_ID,
        )
        self._reward_path: Path | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name=f"agentev-tail-{self.trial_id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        self.state.ensure_trial(self.trial_id, str(self.trial_dir), agent="claude-agent-sdk")
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

    def _run_id_for_who(self, who: str | None) -> str:
        name = who or "solo"
        if name in _ROOT_WHO:
            self._ensure_root_run()
            return ROOT_RUN_ID
        existing = self._who_runs.get(name)
        if existing is not None:
            return existing
        self._ensure_root_run()
        run_id = f"run_sub_{name}"
        self._who_runs[name] = run_id
        self._parsers[run_id] = IncrementalAgentEventsParser(run_id=run_id)
        self.state.ensure_run(
            self.trial_id, run_id, role="subagent", agent_name=name, parent_run_id=ROOT_RUN_ID
        )
        self.state.update_run_status(self.trial_id, run_id, "running")
        return run_id

    def _ensure_root_run(self) -> None:
        if ROOT_RUN_ID not in self._parsers:
            self._parsers[ROOT_RUN_ID] = IncrementalAgentEventsParser(
                run_id=ROOT_RUN_ID, synthesize_subagent_spawns=True
            )
            self._who_runs["solo"] = ROOT_RUN_ID
            self._who_runs["lead"] = ROOT_RUN_ID
            self.state.ensure_run(
                self.trial_id, ROOT_RUN_ID, role="root", agent_name="claude-agent-sdk"
            )

    def _tick(self) -> None:
        files = _find_events_files(self.trial_dir)
        if not files:
            self._maybe_detect_crashed()
            self._maybe_finalize_from_result_json()
            return
        self._ensure_root_run()
        if self.state.trials[self.trial_id].status == "discovered":
            self.state.set_trial_status(self.trial_id, "running")

        for path in files:
            tail = self._tails.get(path)
            if tail is None:
                tail = _FileTail(path)
                self._tails[path] = tail
            records = tail._read_new_lines()
            parsed = []
            for line in records:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and "kind" in rec:
                    parsed.append(rec)
            self._dispatch(parsed)

        # Scan the whole trial dir: this harness nests CLAUDE_CONFIG_DIR one
        # level deeper than the native adapter (agent/sessions/<role>/projects
        # vs agent/sessions/projects), so anchoring on a fixed path would miss it.
        self._workflow.tick([self.trial_dir])
        self._maybe_load_reward()
        self._maybe_finalize_from_result_json()

    def _dispatch(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        by_run: dict[str, list[dict]] = {}
        ordered: list[str] = []
        for rec in records:
            run_id = self._run_id_for_who(rec.get("who"))
            if run_id not in by_run:
                by_run[run_id] = []
                ordered.append(run_id)
            by_run[run_id].append(rec)
        for run_id in ordered:
            events = self._parsers[run_id].feed(by_run[run_id])
            if not events:
                continue
            self.state.push_events(self.trial_id, run_id, events)
            self.error_detector.scan(trial_id=self.trial_id, run_id=run_id, events=events)

    # ── finalization (mirrors the other tails) ──

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
            cursor: Path | None = self.trial_dir
            for _ in range(8):
                if cursor is None:
                    break
                for fname in ("reward.txt", "reward.json"):
                    for candidate in cursor.glob(f"**/verifier/{fname}"):
                        self._reward_path = candidate
                        break
                    if self._reward_path is not None:
                        break
                if self._reward_path is not None:
                    break
                cursor = cursor.parent if cursor != cursor.parent else None
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
