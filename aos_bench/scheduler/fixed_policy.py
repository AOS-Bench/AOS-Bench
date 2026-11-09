"""Fixed scheduling policies as same-system baseline arms.

Why this exists
---------------
AOS-Bench measures *agent-native management*: the agent is handed a pool of
arriving tasks (``todo_next`` / ``todo_list`` / ``todo_update``) and
organises execution itself. That number is uninterpretable on its own —
"the agent scored 0.60" invites the reply "compared to what?". This module
supplies the two trivial comparators:

    serial-fifo   one task at a time, FIFO by arrival
    fixed-k-fifo  at most k tasks in flight, refill on completion

Together with the two existing agent arms (``main-glm-stream-heavy`` =
goal-only prompt, ``skill-glm-stream-heavy`` = goal-only + resource-manager
skill) they answer: is the agent's self-organisation worth anything over a
scheduler you could write in twenty lines?

The invariant that makes the comparison honest
----------------------------------------------
**Only the scheduling decision differs. The solver is the same code path,
the same model, the same CLI, the same tools, the same budget.**

This is enforced structurally rather than by convention:

  * :class:`FixedPolicyAgent` *subclasses*
    :class:`harbor.agents.installed.claude_code.ClaudeCode`. It inherits
    ``install``, ``setup``, and every solver-side builder
    (``build_claude_env``, ``build_claude_setup_command``,
    ``build_claude_run_command``). It overrides exactly one method that
    affects execution: :meth:`run`. So each task is solved by literally
    the same ``claude --verbose --output-format=stream-json <flags>
    --print -- <instruction>`` invocation, with the same
    ``ANTHROPIC_MODEL`` / ``ANTHROPIC_BASE_URL`` /
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` env, that the agent arms run. If
    someone changes the solver, both arms move together; there is no
    second copy to drift.
  * The per-task instruction text is the pool's own ``instruction`` field,
    byte-identical to what ``todo_next`` hands the agent arm.
  * The wall-clock budget T is taken from the same
    ``agent_exec_timeout_sec`` the agent arms are bounded by, so "under
    the same budget T" is true by construction rather than by matching
    numbers in two config files.
  * Model, tools, resources, task set and arrival schedule all come from
    the wave config + stream.toml, which are copied from the agent arm's
    config with only ``agent.import_path`` and ``agent.kwargs.max_in_flight``
    changed.

What the driver is NOT allowed to know
--------------------------------------
A fixed policy still may not see the future. A scheduler that peeks at
unarrived tasks collapses ``stream`` into ``burst`` and measures nothing —
this exact failure already happened once by a different route (pool v0.1
leaked the whole schedule into the agent container and an agent was
observed working from it for a full 10-task stream).

So the driver discovers work **only** through the pool MCP protocol, the
same interface the agent gets:

  * It never reads ``stream.toml``, the schedule dict, or
    ``StreamTask.arrivals``. It does not receive them — it locates the pool
    from the ``aos_pool`` entry harbor appended to ``self.mcp_servers`` and
    talks streamable-http to the dispatcher.
  * ``todo_next`` blocks until a task's ``arrive_wall_time`` has actually
    passed; ``todo_list`` hides ``PENDING`` tasks and strips ``at_sec``
    (see ``TaskPool.todo_list_agent_view``). Arrival-time enforcement is
    therefore the *pool's* invariant, inherited rather than reimplemented,
    which is the only version of it worth trusting.
  * :meth:`_assert_no_schedule_foreknowledge` fails the trial at startup
    if the driver was somehow handed arrival data anyway.

Concurrency is enforced by claiming late
----------------------------------------
``todo_next`` marks a task ``IN_PROGRESS`` at claim time, and
``scheduling_metrics.json`` derives concurrency from
``claimed_at -> terminal_at``. So the semaphore is acquired **before**
``todo_next`` is called, never after. Claiming eagerly and queueing
internally would report k-way concurrency while running serially — the
metric would look right and mean nothing. With claim-late,
``peak_realized_concurrency <= max_in_flight`` holds by construction, and
the smoke check (serial peak=1, k=2 peak=2) is a real test of the policy
rather than a restatement of the config.

The solver does not get the pool tools
--------------------------------------
Each solver session is launched with an MCP config that has the
``aos_pool`` entry removed (:meth:`_solver_mcp_servers`). If a solver could
call ``todo_next`` it could claim a task the scheduler did not schedule,
breaching k without anything in the config looking wrong. Scheduling
authority lives in exactly one place: this driver.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.agents.installed.claude_code import (
    _SUBAGENT_TRAJ_DIRNAME,
    ClaudeCode,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

logger = logging.getLogger(__name__)


# ── per-task solver preamble ───────────────────────────────────────────────
#
# Versioned by the same ``<concept>-<YYMMDD>`` convention as
# ``harbor.trial.aos_pool.STREAM_PREAMBLE_VERSIONS``. Do NOT edit a
# published version in place — experiment results reference it by string.
#
# This wrapper deliberately carries NO scheduling content: no mention of
# other tasks, no parallelism advice, no throughput framing, no task count.
# All of that is the driver's job in these arms, and leaking it here would
# reintroduce the very management signal the baseline is supposed to lack.
# What remains is only the sandbox facts the agent arms' prompt also states
# (root, /workspace, the CPU/memory budget) plus the task text itself.
SOLVER_PREAMBLE_TASK_ONLY_260923 = """\
# Task

You are a CLI agent working inside a sandbox container. You run as root
and `/workspace/` is yours. This container is provisioned with
**{n_core} CPU cores · {mem}**.

Complete the following task. When you are done, stop.

{instruction}
"""

SOLVER_PREAMBLE_VERSIONS: dict[str, str] = {
    "task-only-260923": SOLVER_PREAMBLE_TASK_ONLY_260923,
}
DEFAULT_SOLVER_PREAMBLE_VERSION = "task-only-260923"

# Policy labels recorded in the result context so an arm is self-describing.
POLICY_SERIAL_FIFO = "serial-fifo"
POLICY_FIXED_K_FIFO = "fixed-k-fifo"

# How long each ``todo_next`` call blocks. Short enough that a completion
# reported by a sibling task is not stuck behind a long poll, long enough
# that we are not hammering the dispatcher. Waiting for the next arrival is
# the NORMAL state under the stream setting, not an edge case.
TODO_NEXT_POLL_SEC = 10.0

# Bound on a single pool MCP call. The dispatcher offloads the blocking
# ``todo_next`` to a worker thread, so a hung call means the dispatcher is
# in trouble; failing fast is better than pinning the arm.
POOL_CALL_TIMEOUT_SEC = 120.0

FINISH_SENTINEL = "<FINISH>"


def format_solver_preamble(
    *,
    instruction: str,
    n_core: int,
    memory_mb: int,
    version: str = DEFAULT_SOLVER_PREAMBLE_VERSION,
) -> str:
    """Render the per-task solver prompt.

    Mirrors ``harbor.trial.aos_pool.format_stream_preamble``'s memory
    formatting (``8192 -> "8 GB"``) so the resource line the solver reads is
    worded exactly as the agent arms' line.
    """
    template = SOLVER_PREAMBLE_VERSIONS.get(version)
    if template is None:
        raise ValueError(
            f"Unknown solver-preamble version {version!r}; "
            f"known: {sorted(SOLVER_PREAMBLE_VERSIONS)}"
        )
    mem = f"{memory_mb // 1024} GB" if memory_mb % 1024 == 0 else f"{memory_mb} MB"
    return template.format(instruction=instruction, n_core=int(n_core), mem=mem)


class SchedulerConfigError(RuntimeError):
    """Raised when the arm cannot run as a *valid* baseline.

    Deliberately fatal rather than degrade-and-continue. Every failure mode
    guarded by this exception (no pool MCP entry, schedule foreknowledge,
    k < 1) still produces a plausible-looking ``reward.json``, so a soft
    failure would yield a quotable number that measures the wrong thing.
    """


class FixedPolicyAgent(ClaudeCode):
    """Outer driver that owns scheduling and delegates solving to ClaudeCode.

    Registered with harbor via ``agent.import_path``::

        "import_path": "aos_bench.scheduler.fixed_policy:FixedPolicyAgent"

    Agent kwargs:
      max_in_flight     int, k. 1 = serial FIFO. Default 1.
      per_task_timeout_sec
                        float | None. Cap on one solver session. Default
                        None = "whatever is left of the global budget",
                        which is the pure policy: the budget is global and
                        a task that overruns is a real property of serial
                        FIFO, not something to paper over. Tune on a
                        development sample ONLY.
      solver_preamble_version
                        str, default "task-only-260923".
      total_budget_sec  float | None. Overrides the inherited T. Leave
                        unset so T comes from the same
                        ``agent_exec_timeout_sec`` as the agent arms.
    """

    # The driver never claims ATIF support on the root session because there
    # is no single root transcript; it publishes one trajectory per solved
    # task instead (see populate_context_post_run).
    SUPPORTS_ATIF: bool = True

    def __init__(
        self,
        logs_dir: Path,
        *args,
        max_in_flight: int = 1,
        per_task_timeout_sec: float | None = None,
        solver_preamble_version: str = DEFAULT_SOLVER_PREAMBLE_VERSION,
        total_budget_sec: float | None = None,
        **kwargs,
    ):
        k = int(max_in_flight)
        if k < 1:
            raise SchedulerConfigError(
                f"max_in_flight must be >= 1, got {max_in_flight!r}"
            )
        self.max_in_flight = k
        self.per_task_timeout_sec = (
            float(per_task_timeout_sec) if per_task_timeout_sec else None
        )
        if solver_preamble_version not in SOLVER_PREAMBLE_VERSIONS:
            raise SchedulerConfigError(
                f"unknown solver_preamble_version {solver_preamble_version!r}; "
                f"registered: {sorted(SOLVER_PREAMBLE_VERSIONS)}"
            )
        self.solver_preamble_version = solver_preamble_version
        self._total_budget_override = (
            float(total_budget_sec) if total_budget_sec else None
        )
        # Per-task bookkeeping, surfaced in AgentContext.extra so an arm's
        # behaviour is readable straight from result.json.
        self._task_records: list[dict[str, Any]] = []
        self._session_subdirs: dict[str, str] = {}
        super().__init__(logs_dir, *args, **kwargs)

    @staticmethod
    def name() -> str:
        # Reuse the claude-code adapter identity: the solver *is* claude-code,
        # and AgentName is a closed enum. The arm is distinguished by
        # import_path + the policy fields recorded in AgentContext.extra.
        return ClaudeCode.name()

    @property
    def policy(self) -> str:
        return POLICY_SERIAL_FIFO if self.max_in_flight == 1 else POLICY_FIXED_K_FIFO

    # ── invariant guards ──────────────────────────────────────────────────

    def _pool_mcp_url(self) -> str:
        """Locate the pool MCP endpoint harbor appended to self.mcp_servers.

        This is the only channel through which the driver learns about work,
        and it is also how the driver avoids needing any new plumbing:
        ``harbor.trial.aos_pool._setup_aos_pool_v03`` already appends the
        per-stream entry before ``run()`` is called.
        """
        for server in self.mcp_servers or []:
            if server.name == "aos_pool":
                url = getattr(server, "url", None)
                if not url:
                    raise SchedulerConfigError(
                        "aos_pool MCP entry has no url; the fixed-policy arm "
                        "requires pool v0.3 (streamable-http). Set "
                        "aos_pool_v03=true in the job config."
                    )
                return str(url)
        raise SchedulerConfigError(
            "no aos_pool MCP server was registered for this trial, so the "
            "scheduler has no way to discover arriving tasks. The "
            "fixed-policy arms require a stream task with aos_pool_v03=true."
        )

    def _scrub_schedule_foreknowledge(self, context: AgentContext) -> dict[str, Any]:
        """Delete the arrival schedule from the driver's reachable state.

        This is the load-bearing enforcement of "a fixed policy may not see
        the future", and it is a real hazard rather than a hypothetical one.
        The agent arms are safe structurally: the agent lives inside the
        container and ``AgentContext`` is a host-side Python object it can
        never read. This driver runs on the *host* and harbor hands it that
        object directly — and ``Trial._populate_stream_context``
        unconditionally fills ``extra["aos_arrivals"]`` with every task's
        ``arrive_wall_time``, ``deadline_wall_time`` and full instruction
        text, for pool trials too. Verified on an existing glm heavy trial:
        20 arrivals, times ``[0, 32, 101, 172, 235, 303, ...]``.

        So the driver is *handed* exactly the foreknowledge that would
        collapse ``stream`` into ``burst``. Asserting "I promise not to read
        it" would be worth nothing — a later edit to the claim loop could
        start reading it and every artifact would still look normal. Instead
        we ``pop`` it before the scheduling loop exists, so the data is
        physically absent from the only reference the driver holds. Arrival
        times are then obtainable in exactly one way: waiting for
        ``todo_next`` to return, which is the pool's own clock.

        A digest (task count + a hash of the task-id list, never the times)
        is kept for provenance; ``stream.toml`` on disk remains the record
        of the schedule itself.
        """
        extra = dict(context.extra or {})
        if not extra.get("aos_pool_active"):
            raise SchedulerConfigError(
                "aos_pool is not active for this trial; the fixed-policy "
                "arms discover work only through the pool protocol and "
                "would otherwise do nothing at all."
            )
        if not extra.get("aos_pool_v03"):
            raise SchedulerConfigError(
                "the fixed-policy arms require aos_pool v0.3 (the "
                "out-of-container dispatcher). v0.1 writes the schedule into "
                "the agent container and v0.2 has no HTTP endpoint the "
                "host-side driver can reach."
            )

        digest: dict[str, Any] = {}
        for key in ("aos_arrivals", "aos_schedule"):
            value = extra.pop(key, None)
            if not value:
                continue
            try:
                task_ids = [str(a.get("task_id", "")) for a in value]
            except (AttributeError, TypeError):
                task_ids = []
            digest[key] = {
                "n": len(value),
                "task_ids_sha256": _sha256("\n".join(task_ids)),
                "scrubbed": True,
            }
        extra.pop("aos_max_steps", None)

        # Post-condition: nothing left in the driver's view mentions an
        # arrival time. Checked, not assumed — a future harbor change could
        # add a differently-named field carrying the same data.
        for key, value in extra.items():
            blob = json.dumps(value, default=str)
            if "arrive_wall_time" in blob or "deadline_wall_time" in blob:
                raise SchedulerConfigError(
                    f"AgentContext.extra[{key!r}] still carries arrival "
                    "times after scrubbing. A scheduler that can see "
                    "unarrived tasks measures nothing while still emitting a "
                    "normal reward.json. Refusing to run."
                )

        context.extra = extra
        if digest:
            logger.info(
                "scrubbed schedule foreknowledge from driver state: %s",
                {k: v["n"] for k, v in digest.items()},
            )
        return digest

    def _solver_mcp_servers(self) -> list[Any]:
        """MCP servers for solver sessions: everything EXCEPT aos_pool.

        Scheduling authority must live in exactly one place. A solver that
        could call ``todo_next`` could claim work the scheduler never
        scheduled, breaching ``max_in_flight`` with nothing in the config
        looking wrong.
        """
        return [s for s in (self.mcp_servers or []) if s.name != "aos_pool"]

    # ── budget ────────────────────────────────────────────────────────────

    def _total_budget_sec(self) -> float:
        """The wall budget T, inherited from the agent arms by default.

        ``agent_exec_timeout_sec`` is what actually bounds the agent arms:
        their whole run is a single ``exec`` and the sandbox environment's exec
        clamps it to that value (the 2026-09-19/20 sweep shows agent runs
        clipped at exactly the per-tier request_timeout). Taking the same
        number here is what makes "under the same budget T" structural.
        """
        if self._total_budget_override is not None:
            return self._total_budget_override
        if self.agent_exec_timeout_sec:
            return float(self.agent_exec_timeout_sec)
        raise SchedulerConfigError(
            "no wall budget available: agent_exec_timeout_sec is unset and "
            "no total_budget_sec was configured. Without a budget the arm "
            "is not comparable to the agent arms under 'same budget T'. Set "
            "environment.kwargs.request_timeout (as the agent arms do) or "
            "agent.kwargs.total_budget_sec."
        )

    # ── pool protocol ─────────────────────────────────────────────────────

    async def _call_pool(
        self, session: Any, tool: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call one pool MCP tool and parse its JSON payload."""
        result = await asyncio.wait_for(
            session.call_tool(tool, args or {}), timeout=POOL_CALL_TIMEOUT_SEC
        )
        text = "".join(
            (getattr(part, "text", "") or "") for part in (result.content or [])
        )
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("pool %s returned non-JSON payload: %r", tool, text[:300])
            return {}

    # ── solver ────────────────────────────────────────────────────────────

    def _session_dir_for(self, index: int, task_id: str) -> str:
        """Per-task ``CLAUDE_CONFIG_DIR``, one session per solved task.

        Sessions must not share a directory: ``_get_session_dir`` requires a
        unique session dir and the ATIF converter keys steps by session, so
        two concurrent solvers writing one transcript would interleave into
        an unparseable trajectory and silently cost the arm its token/cost
        numbers.
        """
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", task_id)[:80]
        return f"{index:03d}__{slug}"

    async def _solve_one(
        self,
        environment: BaseEnvironment,
        *,
        index: int,
        task_id: str,
        instruction: str,
        timeout_sec: float,
    ) -> tuple[bool, str]:
        """Run ONE solver session for one task. Returns (ok, note).

        Uses the inherited builders, so this is the same solver invocation
        the agent arms run — only the prompt body (this task, no scheduling
        content) and the session/transcript directory differ.
        """
        subdir = self._session_dir_for(index, task_id)
        self._session_subdirs[task_id] = subdir
        config_dir = (
            EnvironmentPaths.agent_dir / "sessions_tasks" / subdir
        ).as_posix()
        transcript = (
            EnvironmentPaths.agent_dir / "solver_logs" / f"{subdir}.txt"
        ).as_posix()

        env_cfg = self._task_env_config()
        prompt = format_solver_preamble(
            instruction=instruction,
            n_core=env_cfg[0],
            memory_mb=env_cfg[1],
            version=self.solver_preamble_version,
        )

        env = self.build_claude_env(config_dir=config_dir)

        # Temporarily hide the pool entry so the inherited setup command
        # writes a solver MCP config without it.
        saved = self.mcp_servers
        self.mcp_servers = self._solver_mcp_servers()
        try:
            setup_command = self.build_claude_setup_command()
        finally:
            self.mcp_servers = saved

        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(os.path.dirname(transcript))} && "
                + setup_command
            ),
            env=env,
            timeout_sec=600,
        )

        run_command = self.build_claude_run_command(
            prompt, transcript_path=transcript
        )
        try:
            await self.exec_as_agent(
                environment,
                command=run_command,
                env=env,
                timeout_sec=int(timeout_sec),
            )
        except NonZeroAgentExitCodeError as e:
            # Same tolerance the agent arms apply: the claude CLI exits
            # non-zero on benign end-of-stream conditions, and the
            # transcript is already captured. The verifier decides whether
            # the task actually passed, so a non-zero exit is reported as a
            # failed *claim* but never treated as a harness error.
            note = f"solver exited non-zero: {str(e)[:200]}"
            logger.info("task %s: %s", task_id, note)
            return False, note
        except asyncio.TimeoutError:
            return False, "solver timed out"
        except Exception as e:  # noqa: BLE001 - one task must not kill the arm
            logger.warning("task %s solver raised %s", task_id, e)
            return False, f"solver error: {type(e).__name__}: {str(e)[:200]}"
        return True, ""

    def _task_env_config(self) -> tuple[int, int]:
        """(cpus, memory_mb) for the resource line in the solver prompt."""
        cpus = os.environ.get("HARBOR_CPUS")
        mem = os.environ.get("HARBOR_MEMORY_MB")
        return (int(cpus) if cpus else 4, int(mem) if mem else 8192)

    # ── driver ────────────────────────────────────────────────────────────

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        """Own the scheduling; delegate every task to the inherited solver.

        ``instruction`` is the stream preamble harbor renders for the agent
        arms (the goal-only "organise this yourself" prompt). It is
        deliberately ignored: in these arms the management decision is the
        driver's, and handing the solver a management prompt is exactly the
        confound the baseline exists to remove. It is recorded in
        ``context.extra`` so the arm remains reproducible.
        """
        scrubbed = self._scrub_schedule_foreknowledge(context)
        pool_url = self._pool_mcp_url()
        budget = self._total_budget_sec()
        started = time.monotonic()
        deadline = started + budget

        logger.info(
            "fixed-policy arm: policy=%s k=%d budget=%.0fs pool=%s",
            self.policy,
            self.max_in_flight,
            budget,
            pool_url,
        )

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        semaphore = asyncio.Semaphore(self.max_in_flight)
        in_flight: set[asyncio.Task] = set()
        claimed_index = 0
        stream_done = False
        stop_reason = "unknown"

        # sse_read_timeout must exceed the whole arm: the driver holds one
        # MCP session for the full budget and a shorter read timeout would
        # drop it mid-stream, after which every remaining task goes
        # unclaimed and the arm looks like a scheduling failure instead of
        # a transport one.
        async with streamablehttp_client(
            pool_url,
            timeout=POOL_CALL_TIMEOUT_SEC,
            sse_read_timeout=budget + 600.0,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=60.0)
                tools = await asyncio.wait_for(session.list_tools(), timeout=60.0)
                tool_names = sorted(t.name for t in tools.tools)
                logger.info("pool tools: %s", tool_names)
                if "todo_next" not in tool_names:
                    raise SchedulerConfigError(
                        f"pool MCP server exposes {tool_names}, missing "
                        "todo_next; the scheduler cannot claim work."
                    )

                async def solve_and_report(
                    idx: int, task_id: str, task_instruction: str
                ) -> None:
                    """Solve one task, report its outcome, release the slot.

                    The semaphore is released only here, after
                    ``todo_update``, so a slot is never reusable while the
                    pool still counts the task as in flight. That keeps the
                    realised-concurrency curve and ``max_in_flight`` in
                    agreement.
                    """
                    t_claim = time.monotonic()
                    ok, note = False, "not attempted"
                    try:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            note = "budget exhausted before solver start"
                        else:
                            per_task = (
                                min(self.per_task_timeout_sec, remaining)
                                if self.per_task_timeout_sec
                                else remaining
                            )
                            ok, note = await self._solve_one(
                                environment,
                                index=idx,
                                task_id=task_id,
                                instruction=task_instruction,
                                timeout_sec=per_task,
                            )
                    except asyncio.CancelledError:
                        # The drain phase cancels solvers still running at the
                        # hard deadline. Report the task as failed anyway
                        # instead of letting the cancellation propagate
                        # straight out: without a terminal timestamp the pool
                        # keeps counting it claimed-but-not-terminal, which
                        # `stream_metrics` reads as "still in flight forever"
                        # and which inflates mean_realized_concurrency — the
                        # exact number these arms exist to report honestly.
                        #
                        # `asyncio.shield` in the finally block below is what
                        # actually lets that report go out: a bare `await` in a
                        # cancelled task re-raises CancelledError at the first
                        # suspension point, so the todo_update would never
                        # reach the dispatcher.
                        ok, note = False, "cancelled at budget deadline"
                    finally:
                        status = "completed" if ok else "failed"
                        try:
                            # Shielded so a cancellation (drain at the hard
                            # deadline) cannot abort the report itself.
                            await asyncio.shield(
                                self._call_pool(
                                    session,
                                    "todo_update",
                                    {
                                        "task_id": task_id,
                                        "status": status,
                                        "note": note[:500],
                                    },
                                )
                            )
                        except (Exception, asyncio.CancelledError) as e:
                            # CancelledError is a BaseException, so it must be
                            # named explicitly: `except Exception` alone would
                            # let a cancellation delivered *during* the
                            # shielded await skip the log and the record
                            # below, silently costing the arm one task's
                            # terminal timestamp.
                            #
                            # A lost todo_update leaves the task counted as
                            # permanently in flight and inflates
                            # mean_realized_concurrency, so it is logged at
                            # ERROR and recorded in the per-task record
                            # rather than swallowed.
                            logger.error(
                                "todo_update(%s, %s) failed: %s — task will "
                                "appear in-flight in scheduling_metrics",
                                task_id,
                                status,
                                e,
                            )
                            note = f"{note} [todo_update failed: {e!r}]"
                        self._task_records.append(
                            {
                                "index": idx,
                                "task_id": task_id,
                                "reported_status": status,
                                "note": note[:500],
                                "claimed_at_rel_sec": round(t_claim - started, 3),
                                "terminal_at_rel_sec": round(
                                    time.monotonic() - started, 3
                                ),
                                "session_subdir": self._session_subdirs.get(task_id),
                            }
                        )
                        semaphore.release()

                # ── the policy itself ─────────────────────────────────────
                #
                # acquire -> todo_next -> dispatch. Acquiring FIRST is what
                # makes the policy real: todo_next marks the task
                # IN_PROGRESS the instant it returns, so claiming before we
                # have a free slot would report k-way concurrency while
                # running serially.
                while True:
                    if time.monotonic() >= deadline:
                        stop_reason = "budget_exhausted"
                        break

                    await semaphore.acquire()

                    if time.monotonic() >= deadline:
                        semaphore.release()
                        stop_reason = "budget_exhausted"
                        break

                    try:
                        payload = await self._call_pool(
                            session,
                            "todo_next",
                            {"timeout_sec": int(TODO_NEXT_POLL_SEC)},
                        )
                    except Exception as e:  # noqa: BLE001
                        semaphore.release()
                        logger.warning("todo_next failed: %s; retrying", e)
                        await asyncio.sleep(2.0)
                        continue

                    task_id = payload.get("task_id")

                    if payload.get("stream_done") or task_id == FINISH_SENTINEL:
                        # Do NOT todo_update the sentinel; it is not a task.
                        semaphore.release()
                        stream_done = True
                        stop_reason = "stream_done"
                        break

                    if not task_id:
                        # Timed out with no ready task. Under the stream
                        # setting this is the normal state, not an error:
                        # the next arrival simply has not happened yet.
                        semaphore.release()
                        continue

                    claimed_index += 1
                    task = asyncio.create_task(
                        solve_and_report(
                            claimed_index,
                            str(task_id),
                            str(payload.get("instruction") or ""),
                        )
                    )
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)

                # Drain: let everything already claimed finish and report,
                # bounded by whatever budget is left. Tasks still running at
                # the hard deadline are cancelled and reported failed rather
                # than left dangling, so no task stays "in flight" forever
                # in the concurrency curve.
                if in_flight:
                    remaining = max(0.0, deadline - time.monotonic())
                    logger.info(
                        "draining %d in-flight task(s), %.0fs left",
                        len(in_flight),
                        remaining,
                    )
                    done, pending = await asyncio.wait(
                        list(in_flight), timeout=remaining
                    )
                    for t in pending:
                        t.cancel()
                    if pending:
                        with contextlib.suppress(Exception):
                            await asyncio.wait(list(pending), timeout=120.0)

        elapsed = time.monotonic() - started
        by_status: dict[str, int] = {}
        for rec in self._task_records:
            by_status[rec["reported_status"]] = (
                by_status.get(rec["reported_status"], 0) + 1
            )
        summary = {
            "policy": self.policy,
            "max_in_flight": self.max_in_flight,
            "solver_preamble_version": self.solver_preamble_version,
            "total_budget_sec": budget,
            "elapsed_sec": round(elapsed, 3),
            "stop_reason": stop_reason,
            "stream_done": stream_done,
            "n_claimed": claimed_index,
            "reported_by_status": by_status,
            "per_task_timeout_sec": self.per_task_timeout_sec,
            # Recorded, not used: proof of what the driver ignored.
            "ignored_stream_preamble_sha256": _sha256(instruction),
            "scrubbed_foreknowledge": scrubbed,
            "tasks": self._task_records,
        }
        logger.info(
            "fixed-policy arm done: policy=%s claimed=%d %s elapsed=%.0fs reason=%s",
            self.policy,
            claimed_index,
            by_status,
            elapsed,
            stop_reason,
        )
        context.extra = {**(context.extra or {}), "aos_fixed_policy": summary}

    # ── metrics ───────────────────────────────────────────────────────────

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Roll token/cost metrics up across every per-task solver session.

        ``ClaudeCode.populate_context_post_run`` assumes a single session
        directory, so it would find none here and the arm would report zero
        tokens — a silent loss of the cost metric the comparison needs.
        This walks each per-task session instead, writes one ATIF
        trajectory per task, and sums the totals.

        The inherited persistent-LLM-error gate is applied across all
        sessions too: the verifier scores every sub-task whether or not the
        scheduler claimed it, so a session poisoned by an unrecovered
        gateway error can otherwise score leftover-artifact "passes" the
        arm never earned. Keeping the gate means the baseline and the agent
        arms are held to the same integrity standard.
        """
        sessions_root = self.logs_dir / "sessions_tasks"
        if not sessions_root.is_dir():
            self.logger.debug("fixed-policy: no per-task session dirs found")
            return

        totals = {"prompt": 0, "cached": 0, "completion": 0}
        cost_total = 0.0
        saw_cost = False
        n_sessions = 0
        llm_errors: list[str] = []

        out_dir = self.logs_dir / _SUBAGENT_TRAJ_DIRNAME
        for task_dir in sorted(p for p in sessions_root.iterdir() if p.is_dir()):
            # Layout mirrors ClaudeCode: <config_dir>/projects/<slug>/*.jsonl
            project_root = task_dir / "projects"
            if not project_root.is_dir():
                continue
            session_dirs = sorted(
                {
                    f.parent
                    for f in project_root.rglob("*.jsonl")
                    if "subagents" not in f.parent.parts
                }
            )
            for session_dir in session_dirs:
                try:
                    root_traj, subs = self._convert_events_to_trajectory(session_dir)
                except Exception as exc:  # noqa: BLE001
                    self.logger.debug(
                        "fixed-policy: trajectory conversion failed for %s: %s",
                        session_dir,
                        exc,
                    )
                    continue
                for traj in ([root_traj] if root_traj else []) + list(subs.values()):
                    fm = traj.final_metrics
                    if fm is None:
                        continue
                    totals["prompt"] += int(fm.total_prompt_tokens or 0)
                    totals["cached"] += int(fm.total_cached_tokens or 0)
                    totals["completion"] += int(fm.total_completion_tokens or 0)
                    if fm.total_cost_usd is not None:
                        cost_total += float(fm.total_cost_usd)
                        saw_cost = True
                if root_traj is not None:
                    n_sessions += 1
                    out_dir.mkdir(parents=True, exist_ok=True)
                    dest = out_dir / f"{task_dir.name}.json"
                    try:
                        dest.write_text(
                            json.dumps(
                                root_traj.to_json_dict(), indent=2, ensure_ascii=False
                            ),
                            encoding="utf-8",
                        )
                    except OSError as exc:
                        self.logger.debug(
                            "fixed-policy: could not write %s: %s", dest, exc
                        )
                try:
                    llm_errors.extend(self._detect_persistent_llm_errors(session_dir))
                except Exception:  # noqa: BLE001
                    pass

        context.n_input_tokens = totals["prompt"]
        context.n_cache_tokens = totals["cached"]
        context.n_output_tokens = totals["completion"]
        context.cost_usd = cost_total if saw_cost else None

        extra = dict(context.extra or {})
        policy_info = dict(extra.get("aos_fixed_policy") or {})
        policy_info["n_solver_sessions"] = n_sessions
        policy_info["token_totals"] = dict(totals)
        if llm_errors:
            policy_info["persistent_llm_errors"] = sorted(set(llm_errors))
        extra["aos_fixed_policy"] = policy_info
        context.extra = extra

        if (
            llm_errors
            and not self._llm_error_gate_fired
            and os.environ.get("CLAUDE_CODE_SKIP_LLM_ERROR_GATE", "").strip() != "1"
        ):
            from harbor.agents.installed.base import PersistentLLMError

            self._llm_error_gate_fired = True
            raise PersistentLLMError(
                "fixed-policy arm: solver session(s) carry unrecovered LLM "
                "error(s); trajectories are poisoned and the verifier was "
                "skipped:\n  - " + "\n  - ".join(sorted(set(llm_errors)))
            )


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
