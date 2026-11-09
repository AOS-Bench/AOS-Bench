# AOS-Bench Architecture Overview

> For new contributors: after **30 minutes** with this document you should have a
> mental map of the whole framework. You will know what each directory does, what
> happens during one trial, and where to go to change a given behavior. For details,
> see the [documentation index](./README.md).

---

## 1. Scope and motivation

**AOS-Bench evaluates "an agent's scheduling decisions under a concurrent task stream".**

- Input: a *stream* of 5-30 tasks, each arriving at its predefined `arrive_wall_time`
- Agent role: it must be able to do the individual tasks, and also decide which to do first, how many to run concurrently, and when to spawn sub-agents
- Scoring: each task is verified independently (reward 0/1), plus the overall completion time (throughput)

### Why this benchmark is needed

Existing agent benchmarks (Terminal-Bench, SkillsBench, PinchBench, and others) only
measure "one agent completes one task", which corresponds to the "can do the work"
dimension. Real Agent OS scenarios contain a harder problem that nobody measures:
**when multiple tasks arrive concurrently and sub-agents compete for the same
resources, how good are the orchestrator's scheduling decisions?** This is
essentially the scheduling and resource allocation problem from classic OS research,
but in the LLM agent setting there is no dedicated benchmark for it. AOS-Bench takes
human-validated tasks such as those in Terminal-Bench as "atomic units of work" and
adds a *task stream + orchestrator* layer on top, built specifically to measure this.
It is a signal that single-task benchmarks cannot produce by combination.

---

## 2. Three-pillar architecture

```
┌─────────────────────────── AOS-Bench ────────────────────────────┐
│                                                                  │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────┐ │
│  │   tasks      │   │  framework   │   │     agents           │ │
│  │              │   │              │   │                      │ │
│  │ which tasks, │──>│ how the agent│──>│ how the agent under  │ │
│  │ when they    │   │ runs in the  │   │ test plugs into the  │ │
│  │ arrive, how  │   │ sandbox, how │   │ framework (adapter)  │ │
│  │ to grade them│   │ it's verified│   │                      │ │
│  └──────────────┘   └──────────────┘   └──────────────────────┘ │
│        ↑                  ↑                       ↑              │
│  data/stream/        aos_bench/pool/         harbor/agents/      │
│  task_construction/  harbor/{trial,            installed/        │
│                        environments,           aos_bench/        │
│                        verifier}/              harness/          │
└──────────────────────────────────────────────────────────────────┘

Jump to the document for the pillar you care about:
   how tasks are built      →  docs/tasks/task_design.md
   how the framework runs   →  docs/framework/task_assign.md  (dispatcher)
                               docs/framework/verification.md (verifier)
   how agents plug in       →  docs/framework/agent_contract.md
```

---

## 2.5 What happens in one evaluation (data flow)

```
   Task stream                                  Sandbox (one trial container)        Verifier
   (stream.toml)                                                                     (post-exit)
                       ┌──────────────────┐
   t=0   task_A  ─┐    │   Dispatcher     │     ┌─────────────────────────────┐
   t=60  task_B  ─┤    │  (separate       │     │ ┌─────────────────────────┐ │    ┌──────────────┐
   t=120 task_C  ─┼─▶  │   container,     │◀MCP▶│ │    LLM Agent (CLI)      │ │    │ for each     │
   t=180 task_D  ─┤load│   per-job)       │ HTTP│ │  todo_next() one task   │ │    │  task:       │
   t=240 task_E  ─┘ at │ holds schedule + │     │ │  at a time, runs bash,  │ ├───▶│  test.sh →   │
                   job │ stream state →   │     │ │  edits /workspace       │ │    │  reward 0/1  │
   (never enters start │ /<stream_id>/mcp │     │ └─────────────────────────┘ │    │              │
    sandbox FS)        └──────────────────┘     │ ┌─────────────────────────┐ │    │ aggregate →  │
                                                │ │   Linux (kernel + fs)   │ │    │  reward.json │
                                                │ │   shared /workspace     │ │    └──────────────┘
                                                │ └─────────────────────────┘ │
                                                └─────────────────────────────┘
```

Key points of the diagram (these are also the key differences from the individual setting):

- **One stream = one sandbox**: the N tasks share one docker container, one
  `/workspace`, and one LLM agent process. It is not "one container per task".
- **The schedule is not in the sandbox**: the task table for the whole stream
  (`stream.toml`) is loaded at job start into a **separate dispatcher container**
  (per-job, isolated from the sandbox), which exposes `/<stream_id>/mcp` over HTTP
  MCP. The agent container holds **no** scheduling files at all (`grep -r
  instruction.md` in the image returns nothing); the agent can only call
  `todo_next()` to ask the dispatcher for one task at a time. This layer is the
  "scheduler kernel" vs. "userland" isolation of a streaming OS. The full threat
  model and design trade-offs are in
  [`framework/task_assign.md`](./framework/task_assign.md) §1-2.
- **Tasks arrive by wall clock**, not all ready at t=0: the dispatcher controls when
  each task becomes visible according to `arrive_wall_time`, so the agent must poll
  (`todo_list` / `todo_next`) or it will miss newly arrived tasks.
- **The verifier runs post-exit**: once the agent decides that `stream_done = true`
  and exits, the verifier enters the container and runs each task's
  `tests/<task_id>/test.sh`. Each produces 0/1, and the results are aggregated into
  `reward.json`.
- **Underneath is ordinary Linux**, just inside a docker container with cgroup
  CPU/memory limits. From the agent's point of view it is no different from bare
  metal (except that `/workspace` is its working area).

What is measured: the pass rate of the N tasks in `reward.json` plus the total
wall-clock time of the whole stream. Doing well on both means the agent can both
solve individual tasks and schedule them.

---

## 3. Data model

### Stream (the input of one trial)

Physical layout (`data/stream/V0-1/<stream_name>/`):

```
skillsbench-data-10-44/
├── stream.toml                  # metadata + arrivals schedule
├── solve.sh                     # Oracle: runs every task in order (used to check the data itself is solvable)
├── environment/
│   ├── Dockerfile               # merged image (union of the N tasks' dependencies)
│   ├── _oracle/                 # scripts + ground truth for the oracle solve (not in the agent image)
│   └── _sources/                # backup of the original tasks' environments (used by the merger)
└── tasks/
    ├── court-form-filling/
    │   ├── instruction.md       # task description shown to the agent
    │   ├── solution/            # oracle solution (for reference)
    │   └── tests/test.sh        # verifier entry point: writes reward to /logs/verifier/<task_id>/
    ├── offer-letter-generator/
    └── ...
```

### `stream.toml` schema (v1.1)

```toml
version = "1.0"
[metadata]
name        = "skillsbench-data-10-44"

[environment]
cpus           = 4              # default only; launcher --override-cpus can override it
memory_mb      = 8192           #         same as above
allow_internet = true
docker_image   = "registry.example.com/terminal-rl/skillsbench-data-10-44:sweep"

[session]
max_steps = 120                 # max number of LLM calls within a single agent prompt

[[arrivals]]
arrive_wall_time = 0            # seconds, counted from trial t=0
task_id          = "court-form-filling"

[[arrivals]]
arrive_wall_time = 60
task_id          = "offer-letter-generator"
```

Note that the `deadline_wall_time` field **has been removed entirely**;
the scoring metrics are now "number of tasks passed + overall completion time".

### Reward format (written by the verifier)

One file per task: `/logs/verifier/<task_id>/reward.txt`, containing `0` or `1`.
After running all tasks, the verifier aggregates them into a single `reward.json`:

```json
{"court-form-filling": 1.0, "offer-letter-generator": 0.0, ...}
```

---

## 4. End-to-end timeline of one trial

Using the most common configuration as an example: **single agent + single stream + docker backend + v0.3 dispatcher**.

```
[launcher process]                       [docker cluster]
─────────────                            ───────────────
$ harbor run --task-type stream \
      --agent kimi-cli --model openai/kimi-k2.6 \
      -p data/stream/V0-1/<stream>/

(1) Job.run(): harbor starts
        │
        ├─ setup_aos_dispatcher()                   ┌─ container `aos-disp-<job_id>`
        │     ① docker network create aos-net-<jobid>   │  (image aos-bench/dispatcher:v1)
        │     ② docker run -d aos-bench/dispatcher:v1 ──┤  uvicorn :8765
        │     ③ POST /admin/register {stream_id, schedule, token}
        │                                            └─ pool TaskPool in an in-memory dict
        │
        └─ for trial in trials:
              Trial.run()
                  │
                  ├─ env.start(): start the trial's main container
                  │     docker compose up -d main      ┌─ container `<stream>__<TrialId>-main-1`
                  │     attached to network aos-net    │  /workspace = the trial's working directory
                  │     NO_PROXY=aos_dispatcher set    │  runs as agent_user (uid 1000)
                  │
                  ├─ agent.setup(env)                  │
                  │     adapter installs kimi-cli      │
                  │     writes /tmp/kimi-config.json   │
                  │     writes /tmp/kimi-mcp.json:     │
                  │       mcpServers.aos_pool.url =    │
                  │       http://aos_dispatcher:8765/  │
                  │         <stream_id>/mcp            │
                  │
                  ├─ agent.run(STREAM_PREAMBLE, env)   │
                  │     env.exec(`kimi --config ... --mcp-config-file ...`)
                  │                                    ├─ kimi-cli main process
                  │                                    │     │
                  │                                    │     ├─ loop {
                  │                                    │     │   r = MCP todo_next()
                  │                                    │     │   if r.stream_done: break
                  │                                    │     │   do r.task (write /workspace/...)
                  │                                    │     │   MCP todo_update(task_id, completed)
                  │                                    │     │ }
                  │                                    │     │
                  │                                    │     └─ exit(0)  ◄─ natural exit
                  │
                  ├─ verifier.verify(): runs after the agent exits
                  │     StreamVerifier uploads tests/<task_id>/ into the container
                  │     for task in tasks (parallel):
                  │         env.exec(`bash /tests/<task_id>/test.sh`)
                  │     aggregates each task's reward.txt → reward.json
                  │
                  └─ env.stop(delete=True/False)
                        docker compose down

(2) Job.run finalizer
        teardown_aos_dispatcher()
        docker rm -f aos-disp-<job_id>; docker network rm aos-net-<job_id>
        Trial results written to result.json (per-trial reward summary + runtime)
```

**Key invariants:**

- The agent container **cannot see** stream.toml or any future task. All information
  can only be obtained one task at a time through MCP.
- The dispatcher is a per-job container (not per-trial). Trials share the same one
  and are isolated by a `stream_id` UUID.
- `STREAM_PREAMBLE` (in `harbor/src/harbor/trial/aos_pool.py`) is the "you are
  working inside a streaming OS" guidance given to the agent. All agents use the
  same text.

---

## 5. Codebase layout

### Top level

```
AgentOSBench/
├── aos_bench/                  # main repository code (this branch)
├── harbor/                     # submodule: harness framework (a modified fork)
├── data/                       # submodule: task and stream data
├── scripts/                    # experiment scripts / sweeps / batch launchers
├── streams/                    # legacy demo streams (superseded by data/stream/V0/)
└── docs/                       # where this document lives
```

### `aos_bench/`: AOS-Bench main repository features

| Submodule | Responsibility | Key files |
|---|---|---|
| `pool/` | **Task pool MCP server**: the 5 MCP tools that expose the schedule to the agent, plus the dispatcher | `pool.py` (state machine) `server.py` (FastMCP) `dispatcher.py` (HTTP gateway) `Dockerfile.dispatcher` |
| `task_construction/` | **Stream construction pipeline**: merges N single Harbor tasks into one stream | `assemble/` (main flow) `scripts/generate_streams.py` (batch sampling) `scripts/oracle_sweep.py` (full validation) |
| `trace/` | harness-native logs → AgentTrace (kimi/OpenCode/Claude Code/Codex) | `kimi_cli.py` `claude_code.py` `codex.py` `batch_convert.py` |
| `verifier/` | Reward aggregation (per-task → stream-level) | `pool.py` `result.py` |
| `visualization/` | trajectory → SVG (run graph + per-run swimlane) | `agent_trace.py` |
| `runner/` | CLI that runs a schedule directly (outside harbor, for debugging) | `run.py` |
| `harness/` | **Locally modified agent sources**: kimi-cli / opencode / octopus | (each its own submodule) |
| `sandbox/` | Sandbox helper functions | — |
| `tests/` | aos_bench's own unit tests | — |

### `harbor/`: harness (forked from https://github.com/harbor-framework/harbor)

| Submodule | What AOS-Bench does here |
|---|---|
| `src/harbor/trial/aos_pool.py` | `STREAM_PREAMBLE` + agent ↔ pool MCP wiring + schedule builder |
| `src/harbor/trial/aos_dispatcher.py` | dispatcher container lifecycle |
| `src/harbor/trial/trial.py` | Trial scheduling (including the stream-mode branch) |
| `src/harbor/verifier/stream_verifier.py` | per-task parallel verification + tests/ upload |
| `src/harbor/environments/docker/` | Docker backend (`docker compose` + cgroup resource limits) |
| `src/harbor/environments/vm/` | VM backend (qemu + cloud-init, an alternative for paper-grade reproduction) |
| `src/harbor/agents/installed/` | Agent adapters: `kimi_cli.py` `opencode.py` `claude_code.py` `codex.py`, etc. |
| `src/harbor/models/stream/` | StreamTask / ArrivalSchedule pydantic models |
| `src/harbor/cli/` | `harbor run` CLI (typer) |
| `src/harbor/orchestrators/` | Trial orchestration |

`harbor/` is a separate git repository (submodule) with its own `pyproject.toml` and its own tests.

### `data/`: task + stream data (submodule)

```
data/
├── stream/
│   ├── V0/          # legacy (standalone tasks, superseded by V0-1)
│   ├── V0-1/        # recommended subset: 15 streams, all fully solve-ready
│   ├── V0-2/        # large cross-dataset mixed-N streams (light/medium/heavy)
│   └── candidates/  # candidate streams (not frozen; the pool streams pass through before entering V0-1/V0-2)
├── harbor/          # original Harbor task repositories
│   ├── terminal-bench-2/
│   ├── wildclawbench-converted/
│   └── ...(other dataset submodules)
├── oracle_blacklist.txt   # tasks verified to never pass
├── valid_harbor_tasks.json # pool of usable tasks validated by the oracle
└── UPSTREAM_PINS.md       # upstream commit pin for each dataset
```

### `scripts/`: one-off / experiment scripts

| Script | Purpose |
|---|---|
| `sandbox/docker_stream_smoke.sh` | Minimal local end-to-end run of one stream on Docker |
| `sandbox/docker_kimi_code_smoke.sh` | The same smoke run for the Kimi Code adapter |
| `data/audit_v1_0_trajectories.py` | Audits finished stream trials for infrastructure failures |
| `ops/monitor_aos_stream.py` | Monitoring and wall-clock kill of long trials |

---

## 6. Three key workflows

### 6.1 Running an experiment (what developers do most often)

```
1. pick a stream  data/stream/V0-1/<name>/
2. pick an agent  kimi-cli / opencode / claude-code / codex
3. harbor run     QUICK_START.md
4. check reward   <jobs-dir>/<job>/<trial>/verifier/reward.json
5. trajectory     <trial>/agent/kimi-cli.txt (raw wire log)
                  <trial>/agent/trajectory.json(Harbor ATIF compatibility output)
                  <trial>/agent/agent_trace.json(AOS-Bench AgentTrace run tree)
                  <trial>/agent/agent_trace.svg (visualization)
```

### 6.2 Adding a new task / new stream (extending the data)

```
1. Write a single task in Harbor format
   <task_dir>/
     ├── task.toml
     ├── instruction.md
     ├── environment/Dockerfile
     ├── solution/solve.sh         # oracle
     └── tests/test.sh             # verifier
2. Put it under data/harbor/<dataset>/, or keep it standalone
3. Register in the manifest   aos_bench/task_construction/manifests/<pool>.toml
4. Generate streams           aos_bench/task_construction/scripts/generate_streams.py
                              → written to data/stream/candidates/
5. Oracle validation          run the oracle agent on the stream (confirms the tasks are solvable)
6. Promote to V0-1/V0-2       git mv + update docker_image
```

See [`tasks/task_design.md`](./tasks/task_design.md) §6 (sampling pipeline).

### 6.3 Integrating a new agent (extending the agent matrix)

```
1. harbor/src/harbor/agents/installed/<agent>.py
   implement BaseInstalledAgent:
     - name() statically returns the agent name
     - install(env) installs the agent CLI into the container
     - run(instruction, env, context) actually runs the agent
     - _build_register_config_command() writes the MCP config (pool connection)
2. Register it in the AgentName enum in harbor/src/harbor/models/agent/name.py
3. Follow docs/framework/agent_contract.md (pool tools, termination, verifier boundary)
4. Run the smoke test (`scripts/sandbox/docker_stream_smoke.sh --agent <new>`)
```

Minimal reference implementation: `harbor/src/harbor/agents/installed/kimi_cli.py`,
plus the agent contract: [`framework/agent_contract.md`](./framework/agent_contract.md).

---

## 7. The 5 MCP tools (agent ↔ pool interface)

This is the only agent-pool communication channel. Defined in `aos_bench/pool/server.py`:

| Tool | Input | Output | Purpose |
|---|---|---|---|
| `todo_next` | `timeout_sec` | `{task_id, instruction, stream_done}` or `{stream_done: true}` | Blocks until 1 task is READY, then claims it |
| `todo_list` | — | `[{task_id, status, claimed_at, ...}]` | Snapshot of the full schedule |
| `todo_status` | — | `{n_total, n_ready, n_done, ...}` | High-level counts |
| `todo_update` | `task_id, status, note` | `{ok: true}` | Reports a task's terminal state, `completed`/`failed` |
| `todo_get` | `task_id` | Details of a single task | For debugging |

`STREAM_PREAMBLE` teaches the agent how to use these 5 tools: "finish every task → todo_update → the next todo_next returns stream_done → exit".

---

## 8. Wall-clock control (single-layer model)

There is only one wall: **the launcher's `timeout` command**.

```bash
timeout --kill-after=30 3h python -m harbor.cli.main run ...
                          │
                          ├─ 3h: SIGTERM (agent has 30s to flush its trajectory and exit)
                          └─ 3h+30s: SIGKILL (fallback)
```

Inside the pool, `max_wall_sec` is effectively ∞ (set to `2**31-1` ≈ 68 years), so it never triggers TIMEOUT on its own.


The agent exits naturally via `stream_done = all_completed` (every task has been moved to a terminal state with `todo_update`).

---

## 9. Glossary

| Term | Meaning |
|---|---|
| **stream** | A set of tasks that arrive over time; equivalent to "the input of one trial" |
| **arrival** | One entry in `[[arrivals]]` of stream.toml: `(task_id, arrive_wall_time)` |
| **pool** | The in-memory state machine that tracks task state (PENDING→READY→CLAIMED→COMPLETED/FAILED/TIMEOUT) |
| **dispatcher** | A separate docker container that exposes the pool to the trial container over HTTP MCP |
| **trial** | One (stream, agent, attempt) combination; produces one reward.json |
| **job** | A batch of 1+ trials; one `harbor run` invocation = one job |
| **stream_id** | The UUID of each stream trial, used as the routing key inside the dispatcher so that an agent cannot guess another stream's URL |
| **STREAM_PREAMBLE** | The agent instructions in `harbor/src/harbor/trial/aos_pool.py`, describing how an OS-style scheduler should operate |
| **Oracle agent** | Runs `/oracle/solve.sh` (each task's solution), used to check that the data itself is solvable |
| **verifier** | Runs each task's `tests/test.sh` after the trial finishes, producing reward.txt → reward.json |
| **setting** (stream / burst / individual) | Task visibility policy: tasks become ready progressively / all ready at t=0 / one container per task |

---

## 10. Recommended onboarding path

**Day 1 (2 hours): get one trial running**

1. Read this document (arch.md) + the [`README.md`](./README.md) index
2. Run `scripts/sandbox/docker_stream_smoke.sh`
3. Look at the resulting `<trial>/verifier/reward.json` and `agent/kimi-cli.txt`

**Day 2 (half a day): get to know the framework**

1. Read [`framework/task_assign.md`](./framework/task_assign.md) §2-3 (v0.3 dispatcher design)
2. Read [`framework/verification.md`](./framework/verification.md) (verifier flow)
3. Skim [`tasks/task_stream.md`](./tasks/task_stream.md) (stream protocol v1.1)

**Day 3 onward: go deeper depending on your goal**

| Goal | Read |
|---|---|
| Add a new task | [`tasks/task_design.md`](./tasks/task_design.md) §3 Pool candidate list |
| Add a new agent | [`framework/agent_contract.md`](./framework/agent_contract.md) and the adapters in `harbor/src/harbor/agents/installed/` |
| Change the verifier | [`framework/verification.md`](./framework/verification.md) §3-4 |
| Change the dispatcher | [`framework/task_assign.md`](./framework/task_assign.md) §3 detailed design |

---

## 11. FAQ

**Q: Why not just use Harbor / Terminal-Bench?**
A: They only evaluate single tasks. AOS-Bench measures "several tasks arrive at once;
how do you schedule them?" That signal cannot be obtained by combining single-task
benchmarks.

**Q: Why can't the agent see the schedule?**
A: If it could, the problem would degenerate into batch planning and lose the
streaming signal.

**Q: How long does a trial take?**
A: V0-1 streams usually take 20-60 minutes (N=5 to N=10), depending on agent speed
and task difficulty. The default launcher wall is 3h (stream/individual) / 10h (burst).

**Q: How is data/ managed?**
A: As a submodule. Upstream is [`AOS-Bench/AOS-Bench-Task`](https://github.com/AOS-Bench/AOS-Bench-Task).

**Q: Is harbor a fork?**
A: Yes. The submodule points to `AOS-Bench/Harbor-AOS`, which differs from upstream
[`harbor-framework/harbor`](https://github.com/harbor-framework/harbor) by a number
of AOS-Bench-specific changes (stream task type, aos_pool integration, stream_verifier, etc.).
