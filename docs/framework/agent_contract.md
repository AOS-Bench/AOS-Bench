# Agent Contract: System Prompt, Dispatcher, Termination, Verifier

> **Covers:** stream tasks (`--task-type stream`) with the v0.3 universal
> dispatcher (`--aos-pool-v03`). Single-task non-stream runs follow the
> upstream Harbor contract and are out of scope here.

This doc is the **agent's perspective**: what the agent sees, what it
must do, when it's done, and what gets scored. For dispatcher-internal
mechanics see [`task_assign.md`](task_assign.md); for verifier internals
see [`verification.md`](verification.md).

---

## 1. The four pieces

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│   ┌──────────────┐   stream of prompts    ┌──────────────────────┐   │
│   │  Dispatcher  │ ──── todo_next ────▶   │   Agent (LLM CLI)    │   │
│   │  (per-job,   │ ◀─── todo_update ───   │   running in trial   │   │
│   │   HTTP MCP)  │                        │   container          │   │
│   └──────────────┘                        └──────────────────────┘   │
│        │                                            │                │
│        │ when stream_done flips,                    │ tool calls,    │
│        │ the next todo_next returns                 │ shell, file I/O│
│        │ task_id == "<FINISH>"                      │                │
│        ▼                                            ▼                │
│   ┌──────────────┐                        ┌──────────────────────┐   │
│   │ termination  │                        │      /workspace      │   │
│   │ agent sees   │                        │   shared filesystem  │   │
│   │ <FINISH>     │                        │                      │   │
│   │   OR         │                        └──────────────────────┘   │
│   │ harbor       │                                    │              │
│   │ agent timeout│                                    │              │
│   └──────────────┘                                    ▼              │
│                                              ┌──────────────────┐    │
│                                              │     Verifier     │    │
│                                              │ (post-exit, per  │    │
│                                              │  task test.sh)   │    │
│                                              └──────────────────┘    │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

| Piece | Lives in | Source |
|---|---|---|
| **System prompt** | `STREAM_PREAMBLE` string, injected into first JSON-RPC `prompt` | `STREAM_PREAMBLE` in `harbor/src/harbor/trial/aos_pool.py` |
| **Dispatcher MCP tools** | HTTP MCP server in a sibling container | `aos_bench/pool/server.py` + `aos_bench/pool/pool.py` |
| **Termination contract** | `<FINISH>` sentinel returned by `todo_next`; agent self-exits | `aos_bench/pool/pool.py:todo_next` + prompt above |
| **Verifier** | Post-exit, per-task `test.sh` runs that emit reward 0/1 | `harbor.verifier.stream_verifier` |

There is **no agent-framework-specific wrapper** in the termination
path. The dispatcher hands back `<FINISH>`; the agent sees it in the
same JSON it polls every turn; the agent exits on its own. Per-agent
wrappers are kept as thin as possible: for kimi-cli the wrapper is
literally `echo <prompt> | kimi --wire | tee >> log`.

---

## 2. System prompt: what the agent reads on turn 1

`STREAM_PREAMBLE` is the entire user input for the agent's first
prompt. Two placeholders are filled in by
`format_stream_preamble(n_core, memory_mb)` at trial start, using the
container's post-override CPU + memory budget. Reproduced verbatim
below (with placeholders filled for the common 4-core / 8 GB
configuration):

```
# Mission

You are a CLI agent running as the natural-language interface to a
sandbox container. User work arrives over time as a **stream of
tasks** with no fixed count and no preview of what's coming. Your job
is to **complete as many tasks as possible, as fast as possible**,
inside a single wall-clock budget.

# Resources

This container is provisioned with: **4 CPU cores · 8 GB
memory**. Anything you choose to do — serial work, parallel work,
heavy compute, light I/O — shares this single budget. Treat it as
the only hardware constraint that matters.

# Sandbox

You run as root inside the container. `/workspace/` is yours.

# Tools (in addition to your normal toolbox)

  todo_next(timeout_sec)  — block up to N seconds for the next
                            ready task; may return a real task or
                            the FINISH sentinel (see Termination)
  todo_list()             — snapshot of tasks currently visible to
                            you and their status
  todo_status()           — high-level state; check `stream_done`
  todo_update(task_id, "completed" | "failed", note="...")
                          — report a final outcome, exactly once per
                            real task

You only see tasks once they become ready. There is no preview of
future arrivals and no fixed task count.

# Termination

Exit when, and only when, `todo_next` returns a task whose `task_id`
is exactly `<FINISH>`.

# Evaluation Metrics

  - **Tasks completed** — verifier-checked, post-exit; failed or
    unclaimed tasks count as zero
  - **Per-task wall time** — credited as `t_done − t_arrive`; lower
    is better. Wait time spent before you start working on a task
    counts against it in full
  - **Throughput** — across the whole stream, fewer wall-clock
    seconds per completed task is better. Idle cores while work is
    waiting is wall-clock time you don't get back

# Parallelism

Multiple agents working in parallel is generally faster than one
agent grinding tasks one after another. You decide how, and how
many, but throughput is what's being scored.
```

**Design intent:** the prompt only states the goal, the budget, the
measurement, and the termination contract. It deliberately avoids
operational advice ("spawn sub-agent per task", "poll every 60s"),
because such advice biases agent behavior measurably and the agent
should decide its own strategy.

**Why hide the task count + future arrivals:** the agent should respond
to the stream as it unfolds, not plan against a known schedule. See
§3.3 below.

**Why the explicit `<FINISH>` contract:** the alternative is to rely on
`stream_done: true` in the JSON-RPC response plus a per-agent
re-prompting wrapper that keeps the agent alive when it emits bare text.
That couples the harness to each agent framework's protocol details.
This design folds termination into the existing `todo_next` channel:
the agent is already polling it, and the dispatcher writes back a
special task_id when it's done. No wrapper is needed. If an agent stops
calling `todo_next` early, that is an instruction-following capability
gap of the model, not infrastructure to paper over.

---

## 3. Dispatcher: what the agent can ask

The dispatcher is a separate container (`aos-disp-<hash>`) reachable
inside the trial container as `http://aos_dispatcher:8765/<stream_id>/mcp`.
Tools exposed via MCP:

### 3.1 `todo_next(timeout_sec=60)`

Block up to `timeout_sec` for the next **ready** task. Three return
shapes:

```json
// A real task is ready
{"task_id": "abc-123", "instruction": "...", "stream_done": false}

// Stream is over: exit
{"task_id": "<FINISH>", "instruction": "<FINISH>", "stream_done": true}

// No task ready right now, but more may arrive: call again
{"task_id": null, "stream_done": false, "timed_out": true}
```

The agent owns the returned task until it calls `todo_update`. Multiple
`todo_next` calls in quick succession claim different tasks (one each).
The `<FINISH>` sentinel is NOT a real task: do not call `todo_update`
on it; just exit.

### 3.2 `todo_update(task_id, status, note="")`

Report the task's final outcome exactly once. `status` must be
`"completed"` or `"failed"`. `note` is free-form (especially useful for
`failed` so a postmortem reader knows why). The dispatcher records the
completion timestamp; you don't report intermediate progress.

### 3.3 `todo_list()` and `todo_status()`: **agent-filtered views**

These are intentionally narrower than what the dispatcher tracks
internally:

```python
# aos_bench/pool/pool.py

def todo_list_agent_view(self) -> list[dict]:
    """Agent-facing snapshot. Hides PENDING tasks (future arrivals)
    and strips at_sec (planned arrival time) so the agent cannot peek
    at future schedule entries.
    Returned entries cover only tasks already ready / in-flight /
    completed / failed / timed-out.
    """

def todo_status_agent_view(self) -> dict:
    """Agent-facing status. Hides n_total and the PENDING count from
    counts_by_status so the agent cannot infer how many tasks are
    queued for the future.
    """
```

So `todo_list()` returns whatever the agent already has visibility on:
ready tasks, claimed tasks, terminal tasks. A pending task at
`at_sec=120` is invisible until t=120 actually elapses and the
dispatcher promotes it.

`todo_status()` returns `now_sec`, `stream_done`, and `counts_by_status`
minus the pending bucket. No `n_total`.

Internal callers (dispatcher, monitor) keep using the unfiltered
`todo_list()` / `todo_status()` for telemetry; only the MCP tool
endpoints route through the `_agent_view` versions.

### 3.4 Status state machine

```
PENDING ──── tick (now_sec ≥ at_sec) ────▶ READY
                                              │
                                       todo_next claim
                                              │
                                              ▼
                                        IN_PROGRESS
                                              │
                              todo_update(completed | failed)
                                              │
                                              ▼
                                  COMPLETED  /  FAILED
```

`TIMEOUT` is a terminal status the dispatcher sets if `max_wall_sec`
elapses with the task still in-flight; in practice this is never hit
because the schedule's `max_wall_sec` is set to INT32_MAX seconds
(~68 years) and wall control is delegated entirely to Harbor's agent
timeout. See [`task_assign.md`](task_assign.md) §3.8 for the rationale.

When every task is terminal AND no more arrivals are pending, the
dispatcher flips `stream_done` to true and the next `todo_next` call
returns `<FINISH>` (see 3.1).

---

## 4. Termination: when does the agent exit

Two conditions, OR-combined. Whichever fires first wins.

### 4.1 Condition A: `<FINISH>` from `todo_next`

The dispatcher flips `stream_done` to true when:

- Every task has reached a terminal state (`COMPLETED` /
  `FAILED` / `TIMEOUT`), AND
- No more arrivals are pending.

The very next `todo_next` call then returns:

```json
{"task_id": "<FINISH>", "instruction": "<FINISH>", "stream_done": true}
```

The agent is told (in the system prompt) that this is the only valid
exit signal. When it sees `<FINISH>`, it should produce a brief
acknowledgment text and exit its loop. The CLI process then naturally
terminates (e.g. `kimi --wire` reads stdin EOF and exits).

### 4.2 Condition B: Harbor agent timeout

Harbor's `Trial._execute_agent` wraps `agent.run()` in
`asyncio.wait_for(..., timeout=self._agent_timeout_sec)`. The timeout
is computed from `task.config.agent.timeout_sec` (or
`config.agent.override_timeout_sec`, set by `--agent-timeout-sec`),
capped by `max_timeout_sec`, multiplied by `agent_timeout_multiplier`.

When it fires, `AgentTimeoutError` is raised and the execution phase
ends. Tasks the agent never claimed sit in `PENDING` / `READY` and
score 0 in the verifier.

### 4.3 End of the execution phase

However the execution phase ends (normal exit, failure or time limit),
Harbor closes the dispatcher for the stream before verification: from
then on `todo_next` returns the `<FINISH>` sentinel and `todo_update` is
rejected, so an agent process that outlives its limit cannot change the
recorded task states. Harbor then stops every process the agent started
and writes the outcome to `agent_stop.json` in the trial directory.

### 4.4 What is NOT a termination signal

- The agent emitting a final text response without a tool_call. The
  CLI's stdin stays open (kimi-cli will block waiting for more JSON-RPC
  prompts that never arrive, until the outer asyncio.wait_for fires).
  This is intentional: a premature "I'm done" is considered an
  agent-side instruction-following failure, not something for Harbor
  to work around.
- An empty `todo_list()`. The dispatcher may have more arrivals
  scheduled later; the agent should call `todo_next` again with a
  generous `timeout_sec`.
- `stream_done: true` in `todo_status()` (without yet calling
  `todo_next`). The canonical termination signal is `<FINISH>` from
  `todo_next`; `todo_status` is just a status check.
- A claimed task failing. Report it via `todo_update("...", "failed",
  note=...)` and move on.

### 4.5 No multi-prompt re-prompting wrapper

The harness does not use a per-agent wrapper that polls the dispatcher
after every JSON-RPC response and re-prompts the agent while the stream
is not done. The dispatcher's `<FINISH>` sentinel makes such a wrapper
unnecessary: the agent learns about termination through the same
channel it's already polling.

The kimi-cli wrapper is a one-line shell pipe:

```bash
(echo <initial_prompt>; sleep 86400) | kimi --wire --yolo … \
  | tee -a /logs/agent/kimi-cli.txt > /dev/null
```

The `sleep 86400` keeps stdin alive so kimi-cli's wire-mode RPC
reader doesn't see EOF mid-turn and exit before the LLM even runs.
When the agent receives `<FINISH>` it emits a final response and
stops issuing tool_calls; kimi then sits idle waiting for the next
prompt (which never comes) until Harbor's agent timeout fires and
Harbor stops the agent's processes (§4.3), usually after the work is
long done.

The trade-off is wasted container wall time between "agent finished"
and "agent timeout fires", but the win is no per-agent exit-detection
logic in the harness. Premature exit by the model is visible as
instruction-following failure directly in the metric, not papered over
by a re-prompting wrapper.

### 4.6 Persistent-LLM-error gate (skip verifier on poisoned trajectories)

> Enabled for the **claude-code** adapter. Toggle off via
> `CLAUDE_CODE_SKIP_LLM_ERROR_GATE=1` (debug only).

The verifier runs on every task in the stream regardless of whether the
agent claimed it (§5). When a session is poisoned by an unrecovered LLM
error (AUP refusal, 402 quota, persistent rate-limit, server 5xx,
refusal `stop_reason`), the agent might exit while half its tasks are
still in `READY` state. The verifier doesn't know that, runs each
test.sh against whatever happens to be on disk, and any task that
*incidentally* passes (leftover files from another task, default repo
state, etc.) gets credit the agent never earned.

Without a gate, reward tables are inflated by these unearned passes.
So the adapter's `populate_context_post_run` scans the session JSONL
for these patterns:

| label | regex (case-insensitive) |
|---|---|
| `aup_refusal` | `violate our Usage Policy` |
| `quota_402` | `API Error: 402.*quota \| quota is exhausted` |
| `server_5xx` | `API Error: 50[023]\b` |
| `rate_limit_persistent` | `API Error: 429.{0,40}rate.?limit` |
| `anthropic_refusal` | `"stop_reason"\s*:\s*"refusal"` |
| `auth_failed` | `API Error: 40[13]\b \| "error":"authentication_failed"` |

If any pattern matches anywhere in the session log, the adapter raises
`PersistentLLMError` **on the first `populate_context_post_run` call**
and sets an instance flag so subsequent calls (Harbor invokes the hook
twice: success path + outer-except diagnostic path) rebuild
trajectory but don't re-raise. Harbor's outer trial loop catches the
first raise, marks the trial **failed**, writes
`result.json.exception_info` with the full traceback, and **skips the
verifier**. `trajectory.json`, `result.json`, and per-step token/cost
metrics are still populated so the trial is fully diagnosable.

Why these patterns are "persistent": the claude-code CLI does its
own internal retries on rate-limits and transient 5xx; if the message
shows up in the wire log, the CLI's recovery already failed. The
agent process's exit status is the same as a clean exit, so the gate
has to read the trace and decide.

Adapter side (claude-code):
- `harbor/src/harbor/agents/installed/claude_code.py`:
  `_PERSISTENT_LLM_ERROR_PATTERNS`, `_detect_persistent_llm_errors()`,
  raise at end of `populate_context_post_run`.
- Exception class `PersistentLLMError` lives in
  `harbor/src/harbor/agents/installed/base.py`.

---

## 5. Verifier: what reward each task gets

After the execution phase ends (cleanly or via timeout) and Harbor has
closed the dispatcher and stopped the agent's processes (§4.3), Harbor
runs `StreamVerifier`. Each task in the stream is graded independently:

```
for each task in stream:
    workspace = restore_per_task_snapshot(task.task_id)   # see below
    run task's test.sh in workspace
    reward = read /logs/verifier/reward.txt  →  0.0 or 1.0
    record { task_id: reward }

aggregate:
    mean_reward = sum(rewards) / n_tasks
    pass_at_k, reward_stats, etc.
```

Key points:

- **Per-task workspace snapshot**: the verifier resets `/workspace/` to
  a known state for each task before running `test.sh`. Without this,
  one task's edits leak into the next task's test, a common cause of
  false positives where a later task passes because an earlier task
  happened to leave the right file behind.
- **Reward is binary per task** (0.0 or 1.0). Aggregation produces the
  mean across the stream.
- Tasks the agent never claimed (status `READY` / `PENDING` at exit)
  still get verified, and almost always score 0 because the agent
  didn't do anything. **Exception:** if the trial was killed by the
  persistent-LLM-error gate (§4.6), the verifier is skipped entirely
  for trial integrity (otherwise leftover artifacts pollute the
  reward).
- Tasks the agent reported `completed` via `todo_update` are still
  re-verified: the agent's self-report is advisory; the test.sh is
  authoritative. A `completed` report with a failing test.sh scores 0.
- Tasks the agent reported `failed` are still verified; sometimes
  the test.sh passes anyway (agent gave up prematurely).

### 5.1 Strict pass-rate

Because the verifier runs every task regardless of claim, a task can
score 1.0 from leftover artifacts the agent never actually produced
(image default state, files written by an earlier task, etc.). To
expose this "luck inflation", the verifier emits a sibling file
alongside `reward.json`:

```
<trial>/verifier/
├── reward.json              ← {task_id: float}
└── reward_breakdown.json    ← lenient vs. strict breakdown
```

`reward_breakdown.json` joins each task's verifier score with the
dispatcher's per-task lifecycle (claimed_at / completed_at /
agent_reported_status, captured into `<trial>/dispatcher_state.json`
via the `GET /admin/dump` endpoint before verification).
The `summary` block reports two pass-rates:

| metric | definition |
|---|---|
| `pass_rate_lenient` | `n_passed / n_total`, same as `mean(reward.json)` |
| **`pass_rate_strict`** | **`n_claimed_completed_passed / n_total`**: only count tasks the agent explicitly `todo_update(...,"completed")` AND the verifier scored > 0 |

`n_unclaimed_passed` is reported separately so the operator can see
how much luck inflation exists in the lenient number. For an
agent that calls `todo_update` properly, strict ≈ lenient. For an
agent that exits early or never claims tasks, strict << lenient.

If `dispatcher_state.json` is missing (legacy v0.2 path or teardown
crashed), the breakdown still renders but every task shows
`agent_reported_status: null` and `summary.dispatcher_state_present:
false`.

For verifier internals (timeout per task, parallel vs sequential
verification, test isolation), see
[`verification.md`](verification.md).

---

## 6. Putting it together: a typical lifecycle

```
t=0      harbor brings up container + dispatcher
t=2      agent_setup completes; kimi-cli wire mode starts
t=2.1    `echo <STREAM_PREAMBLE> | kimi --wire …`: agent reads the
         prompt, agent runs
t=3      orchestrator: todo_next() → task A
t=4      orchestrator: tool_calls / shell / sub-agents / whatever
…        (free-form work, multiple todo_next / todo_update calls)
t=7100   orchestrator: todo_next() → {"task_id": "<FINISH>", …}
t=7100.1 orchestrator emits a brief "stream is done, exiting" text;
         agent loop terminates; kimi reads stdin EOF and exits
t=7101   harbor sees agent process exit; closes the dispatcher,
         stops remaining agent processes, writes agent_stop.json;
         runs StreamVerifier over all tasks
t=7250   per-task rewards written to result.json
t=7252   trial done

Premature-exit failure mode (instruction-following capability gap):

t=4200   orchestrator emits "I think I'm done" text without seeing
         <FINISH>; agent loop terminates; kimi reads EOF and exits
t=4201   harbor runs verifier on whatever was claimed
         (un-claimed tasks score 0: instruction-following failure
          is reflected directly in the metric)
```

If the harness itself ended the run at the agent's first final text
response, the remaining tasks would never get a chance even though the
agent might have continued, and the loss would be attributable to the
harness. Under this contract, the same premature-exit failure shows up
identically in the score but is correctly attributable to the model.

---

## 7. Common questions

**Why is the system prompt so terse?**
Preambles that prescribe concurrency patterns ("spawn sub-agent per
task", "todo_list every 60-120s") bias the agent's behavior in
measurable, often unhelpful ways. The prompt states the goal + budget +
measurement + termination contract; the agent decides everything else.

**Can the agent see how many tasks are left?**
No. `todo_status()` shows the agent only what's already visible
(ready / in-flight / terminal). It cannot infer `n_total`.

**Can the agent see when future tasks will arrive?**
No. `todo_list()` doesn't include `PENDING` rows and strips `at_sec`.

**What if the agent stops calling todo_next prematurely?**
The harness does not intervene. The agent process stays idle until the
agent timeout fires; any unclaimed task scores 0 in the verifier.
Premature termination is treated as an instruction-following capability
gap of the model, not an infrastructure issue.

**What if the agent's response is taking forever?**
Harbor's `asyncio.wait_for(agent.run, timeout=...)` will fire. Harbor
closes the dispatcher and stops the agent's processes (§4.3).
Whatever was claimed gets graded; whatever wasn't scores 0.

**Where does the actual STREAM_PREAMBLE text live?**
`STREAM_PREAMBLE` in `harbor/src/harbor/trial/aos_pool.py` (constant) +
`format_stream_preamble(n_core, memory_mb)` formatter helper just
below.

**Where does the dispatcher's MCP server live?**
`aos_bench/pool/server.py` (MCP tool wiring) +
`aos_bench/pool/pool.py` (`TaskPool` class, the `_agent_view` filters,
the `<FINISH>` sentinel returned from `todo_next` when stream_done).

**Where does the kimi-cli wrapper live?**
`harbor/src/harbor/agents/installed/kimi_cli.py`: search for
`run_command =`. It is about 6 lines.

**Where does the verifier live?**
`harbor/src/harbor/verifier/stream_verifier.py`.
