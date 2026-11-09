# Resource Monitor v0.2

## 1. Goals

This document defines the resource monitoring and resource attribution scheme for AOS-Bench. It is a companion to [agent_trace.md](./agent_trace.md).

Design goals:

1. Define how the benchmark records resource limits and resource usage
2. Define a `run`-level resource attribution model
3. Describe how resource collection is implemented for `kimi-cli`
4. Record the limitations of the current implementation, in particular the error bounds of the `process_group` approximation

Notes:

- The current implementation follows the principle "usable first, precise later"
- In environments without `cgroup` delegation, `process_group` may be used as the primary approximation

The following collection and failure semantics are implemented:

- The monitor uses `docker stats` to record trial/container-level CPU, memory, network,
  block I/O and PID count, written to
  `<trial_dir>/.monitor-resources.jsonl`. This supports both a live view and offline replay
- The monitor also samples PID, PPID, CPU%, RSS, elapsed time, command
  and args through `docker top`, written to `<trial_dir>/.monitor-processes.jsonl`. The API/UI keeps the per-timestamp snapshots
  and derives a per-process peak CPU/RSS table for the whole trial
- After Harbor starts the container, it writes a persistent cgroup sample that is independent of the monitor,
  `<trial_dir>/agent/.harbor-cgroup-memory.tsv`. When an agent exec fails, Harbor combines cgroup
  `memory.events`, Docker `State.OOMKilled`, this sample, and the monitor peak
- The same low-overhead Harbor probe writes
  `<trial_dir>/agent/.harbor-cgroup-resources.tsv`, nominally once per second: the probe loop
  sleeps 0.1 s per iteration, writes a row every 10 iterations, and samples the filesystem every
  100. The loop body reads many cgroup files, so under load the real interval is longer: in the
  paper's runs rows were 5.5 to 7 s apart. The TSV has a sample counter but no timestamp, so
  derive the interval from the trial duration divided by the number of rows instead of
  assuming one second. The monitor reads the latest row from the host bind mount
  and merges it into `.monitor-resources.jsonl`, so no extra Docker
  proxy RPC is needed per metric.

Each container sample records the following field groups:

| Field group | Metrics |
|---|---|
| Docker stats | CPU%, memory used/limit/%, network RX/TX, block read/write, PID count |
| cgroup CPU | usage/user/system time, quota/period, `nr_periods`, `nr_throttled`, `throttled_usec`, throttled-period % over the sampling window |
| cgroup memory | current/max/peak, swap current/max, low/high/max/oom/oom_kill/oom_group_kill events |
| cgroup I/O | cumulative read/write bytes and read/write operations (summed over all block devices) |
| PID controller | current/max, `pids.events:max` |
| PSI | some/full `avg10` pressure for CPU/memory/I/O |
| filesystem | `/workspace` filesystem size/used/available/% |

Unavailable controllers and unlimited limits are recorded as `null`, never as `0`. cgroup times, I/O
and events are cumulative counters since container start; the Monitor UI shows the increment over the current time window
as the difference between the first and last sample. CPU throttled-period % between adjacent samples is
`Δnr_throttled / Δnr_periods * 100`. It is the fraction of scheduling periods throttled by the quota, and it is not
CPU utilization.

`filesystem` is the actual mount capacity seen by `df /workspace` inside the container (falling back to `/` if it does not exist).
It is not the same as the logical `override_storage_mb` in the Harbor configuration. Some docker proxies
expose the host's backing overlay to the container, so this value may show a capacity far larger than the trial storage
budget. The UI uses it only as a filesystem usage trend and does not use it to judge the storage limit.

Once an OOM is confirmed, Harbor raises `AgentResourceLimitExceededError(resource=memory)`, skips the
verifier, and the default retry configuration excludes this exception. An OOM means the agent failed to schedule resources
within the given budget. It is an agent capability/resource-limit failure, not a resumable infrastructure failure.

Current process attribution rules:

- When a native run/thread id appears on the command line, the process is linked directly to that `run_id`;
- Descendants of an attributed process inherit its owner through the PPID tree;
- Claude Code sidechains usually share the same Claude/Node runtime and have no PID of their own,
  so they are explicitly marked `shared_runtime`. Only the CPU/RSS of the whole runtime can be recorded precisely,
  and no fabricated per-sidechain figures are produced. Precise subagent attribution requires either
  the harness giving each child run its own process/cgroup, or the runtime emitting
  thread/run-level CPU accounting directly.

### 1.1 Semantics of OOM and exit 137

`137 = 128 + SIGKILL(9)` is the generic Linux/shell/Docker exit code convention. It only says that
the executing process received `SIGKILL`; by itself it does not prove an OOM. A manual `kill -9`, a forced kill after a timeout, and
platform reclamation can also produce 137.

A docker proxy used in some deployments has the following specific behavior:

- cgroup v2 `memory.oom.group=1`, so when the memory limit is reached the whole pod/cgroup is killed together;
- the cgroup is mounted read-only, so the value cannot be changed to `0` from inside the sandbox;
- the proxy deletes dead pods quickly, so afterwards Harbor often can no longer read Docker
  `State.OOMKilled` and only gets `no such pod`;
- because the whole group dies at once, the in-container sampler may not get to write the incremented
  `memory.events.oom_kill`.

A controlled test on this proxy used a dummy allocator under 4 CPUs / 8 GiB, without
Claude Code. Memory grew from a low level to
`8,498,577,408 / 8,589,934,592` bytes, after which `docker compose exec` likewise returned
137 and the pod disappeared immediately. So 137 is the SIGKILL return code propagated by compose exec, not a
return code specific to Claude Code.

Harbor uses the following tiers and classification rules:

| Tier | Required evidence | Outcome |
|---|---|---|
| confirmed OOM | `memory.events.oom_kill > 0`, or Docker `OOMKilled=true` | `AgentResourceLimitExceededError(resource=memory)` |
| probable OOM (docker proxy fallback) | agent exec=137, and the main container runtime is no longer reachable, and a fresh sample shows `memory.oom.group=1`, and memory peak ≥ 97% of the limit | Same as above, but the exception detail is explicitly marked `probable OOM` |
| unknown SIGKILL | Only 137, or the combined evidence is incomplete | Kept as an ordinary `NonZeroAgentExitCodeError`; no OOM is assumed |

The 97% threshold compensates for discrete sampling missing the final transient peak. It cannot trigger an OOM classification on its own;
it must hold together with
137, the group-OOM mode, and an unreachable runtime. `docker compose ps -a` may briefly return a stale container ID after
the backing pod has disappeared, so whether a fresh `exec main` succeeds is used
as the runtime availability signal. When reading the monitor curve, use the peak over the whole fresh window,
not the last sample. The last sample may just be a `0 MiB` recorded after the pod disappeared.

Both confirmed and probable OOM mean the agent did not complete its scheduling within the given resource budget. They are agent
capability/resource-limit failures. Harbor must keep this exception and skip the verifier, and the default
retry configuration (on both the model path and the CLI path) must not retry or resume. This prevents a later
`AddTestsDirError` from `docker compose cp: no such pod` from masking the real resource failure.

## 2. Resource Schema

### 2.1 `resource_limits`

`resource_limits` records the budget or hard limits assigned to an execution unit.

Suggested fields:

```json
{
  "cpus": 1,
  "memory_mb": 2048,
  "storage_mb": 10240,
  "pids": 256,
  "network": "disabled",
  "timeout_s": 900,
  "max_steps": 50
}
```

Field descriptions:

| Field | Type | Description |
|---|---|---|
| `cpus` | number\|null | Available CPU quota; fractional values such as `0.5` are allowed |
| `memory_mb` | integer\|null | Memory limit |
| `storage_mb` | integer\|null | Disk/workspace limit |
| `pids` | integer\|null | Process count limit |
| `network` | string\|null | `enabled` / `disabled` / `restricted` |
| `timeout_s` | integer\|null | Time limit |
| `max_steps` | integer\|null | Agent step limit |

### 2.2 `resource_usage`

`resource_usage` records actual consumption.

Suggested fields:

```json
{
  "wall_time_ms": 18342,
  "cpu_time_ms": 9211,
  "memory_peak_mb": 612,
  "storage_written_mb": 31,
  "read_bytes": 120312,
  "write_bytes": 99310,
  "net_rx_bytes": 0,
  "net_tx_bytes": 0,
  "prompt_tokens": 3210,
  "completion_tokens": 640,
  "cost_usd": 0.0123
}
```

Field descriptions:

| Field | Type | Description |
|---|---|---|
| `wall_time_ms` | integer\|null | Actual wall-clock time |
| `cpu_time_ms` | integer\|null | Actual CPU time |
| `memory_peak_mb` | integer\|null | Peak memory |
| `storage_written_mb` | number\|null | Disk volume newly written or grown |
| `read_bytes` | integer\|null | Filesystem bytes read |
| `write_bytes` | integer\|null | Filesystem bytes written |
| `net_rx_bytes` | integer\|null | Network bytes received |
| `net_tx_bytes` | integer\|null | Network bytes sent |
| `prompt_tokens` | integer\|null | Input token count |
| `completion_tokens` | integer\|null | Output token count |
| `cost_usd` | number\|null | Actual cost |

### 2.3 Granularity

Resource accounting allows three granularities:

1. Root sandbox granularity
   - the whole VM / container / trial
2. Run granularity
   - root agent
   - subagent
3. Event / step granularity
   - a single tool call
   - a single LLM request

Not every runtime has to provide all three levels, but at least the following must be supported:

- root sandbox `resource_limits`
- run-level `resource_usage`

## 3. Resource Attribution Model

To analyze the orchestrator's resource scheduling ability, AOS-Bench cannot only count "how much the whole VM used". It also has to answer:

- how much a given subagent consumed itself
- how much the shells / tools / background jobs triggered by a given subagent consumed
- whether these resources should be charged to the parent run or the child run

### 3.1 Attribution target

Resources are ultimately attributed to a `run`.

- Resources of the root orchestrator go to the root run
- Resources of a foreground subagent go to that subagent's run
- Resources of a background subagent go to that background subagent's run
- Tool calls, shell commands and background programs are not themselves the final accounting entities
  - Their resources are preferably merged into "the run that launched them"
  - If needed, local usage can also be recorded separately at the event level

In one line:

```text
program/process usage -> tool/event usage -> owning run usage
```

### 3.2 Attribution boundary

The resource usage of a `run` should include:

1. The run's own LLM/token usage
2. Tool calls executed directly by the run
3. Foreground child process trees triggered by the run
4. Background jobs / background child process trees triggered by the run

It should not double count:

1. The resources of a subagent that already has its own child run
2. Resources of further independent runs spawned by a child run

That is:

- `parent_run.resource_usage`
  records the parent's own resources by default and does not recursively aggregate child runs
- If the total consumption of the whole delegation tree is needed, the analysis layer aggregates it separately

### 3.3 Attribution methods

Resource attribution is implemented in tiers, ordered by strength:

1. `cgroup / sandbox scope`
   - most reliable
   - each run gets its own cgroup / job object / resource scope
   - all processes in the run and their children naturally fall under that scope

2. `process_group`
   - current recommended default approximation
   - record the `pgid` of each shell / background task
   - aggregate CPU / memory / I/O by process group

3. `PID tree attribution`
   - weaker but more general
   - record the root PID when a tool or background task starts
   - recursively collect the resources of its child process tree
   - accumulate into the owning run when it ends

4. `event-level approximation`
   - weakest
   - only wall time / exit code / token usage / output size
   - suitable for runtimes without process-level observability

### 3.4 Collection definitions

The following definitions should be used consistently:

- `wall_time_ms`
  - wall-clock time of the run from `started_at` to `ended_at`
- `cpu_time_ms`
  - total user+system CPU time of the run itself and the child process trees attributed to it
- `memory_peak_mb`
  - peak RSS of the attributed process tree over the run's lifetime
- `read_bytes` / `write_bytes`
  - cumulative filesystem I/O bytes
- `net_rx_bytes` / `net_tx_bytes`
  - cumulative network bytes received/sent
- `prompt_tokens` / `completion_tokens`
  - LLM token ledger attributed to the run

### 3.5 Inheritance and override

`resource_limits` should support "inherit + override" semantics:

- root sandbox limits come from the benchmark / VM / container
- a subagent run inherits the parent run's budget or the runtime default budget by default
- if the runtime sets a separate limit for a subagent, it overrides on the child run

### 3.6 Minimum requirements

If a runtime cannot yet do full resource attribution, the minimum requirements are:

1. root sandbox-level `resource_limits`
2. run-level `wall_time_ms`
3. run-level token usage
4. traceable PID / PGID / task_id / parent run links for background tasks

This at least allows resource attribution to be completed in post-processing.

## 4. `subagent -> shell -> child processes -> resource scope` relationship diagram

The resource attribution chain is best understood as the following diagram:

```text
run_sub_001  (AgentTrace.run)
  role = subagent
  launch_mode = foreground | background
  session_mode = same_session | child_session
        |
        | owns
        v
  tool_call: Shell / background_task / Agent
        |
        | launches
        v
  root process
    - shell pid
    - worker pid
    - child pid
        |
        | forks / execs
        v
  process tree / process group
    - grep
    - python
    - pytest
    - cargo
    - node
        |
        | grouped into
        v
  cgroup / resource scope
    - cpu.stat
    - memory.current
    - memory.peak
    - io.stat
```

The correspondence is:

- `run`
  - the logical attribution unit
- `tool_call` / `background_task`
  - execution events within a run
- `process tree` / `process group`
  - the OS-level entities that actually consume resources

## 5. How the OS accounts for CPU / memory, and how the agent reuses it

The underlying data for resource attribution is not "invented" by the agent. It reuses statistics the operating system already maintains.

Note, however:

- The OS only knows `pid` / `tid` / `pgid` / `cgroup`
- The OS does not know which process belongs to which `run` / `subagent`
- The agent has to add a layer of semantic attribution on top of the OS statistics

### 5.1 CPU accounting

While scheduling processes or threads, the operating system continuously records how much CPU time they actually used.

Common Linux sources:

- `/proc/<pid>/stat`
- `/proc/<pid>/task/<tid>/stat`
- `cgroup v2` `cpu.stat`

Typical fields:

- `utime`
  - user mode CPU time
- `stime`
  - kernel mode CPU time

`cpu_time_ms` is usually defined as:

```text
cpu_time_ms = cumulative (utime + stime)
```

The common `%CPU` is not a field the kernel stores directly. Tools compute it from two samples:

```text
CPU usage % ≈ ΔCPU_time / Δwall_time
```

### 5.2 Memory accounting

The operating system's memory management subsystem knows how many pages each process has mapped and how many physical pages are currently resident.

Common Linux sources:

- `/proc/<pid>/status`
- `/proc/<pid>/statm`
- `/proc/<pid>/smaps`
- `cgroup v2` `memory.current`
- `cgroup v2` `memory.peak`

Common definitions:

- `RSS`
  - currently resident physical memory
- `VSZ`
  - virtual address space size
- `PSS`
  - approximation with shared pages split proportionally
- `memory.current`
  - current memory usage of the cgroup
- `memory.peak`
  - peak over the cgroup's lifetime

In the benchmark, the most directly usable fields are:

- `cpu_time_ms`
- `memory_peak_kb` / `memory_peak_mb`

because these two are the easiest to unify across runtimes.

### 5.3 Why OS statistics are stronger than the agent's own inference

The kernel already does this work:

- the scheduler knows who ran for how long
- memory management knows who holds how many pages
- the `cgroup` subsystem knows the cumulative resources of a whole group of processes

So:

- the OS side does "direct accounting"
- the agent side mostly "reads these ledgers and maps them back to its own execution units"

### 5.4 How the agent reuses these statistics

The agent can reuse OS-side statistics interfaces directly, for example:

- `/proc/<pid>/...`
- `psutil`
- `pidstat`
- `/sys/fs/cgroup/...`

But the agent also has to maintain its own semantic mapping:

```text
kernel stats
  -> pid / pgid / cgroup
  -> tool_call / background_task
  -> owning run
  -> AgentTrace.resource_usage
```

That is:

- the OS tells us "which processes used how much"
- the agent tells us "which subagent / run these processes belong to"

### 5.5 Why OS statistics alone cannot complete attribution

Because the OS does not know:

- which `pid` was launched by the root orchestrator
- which `pid` belongs to subagent A
- which `pid` corresponds to a particular `Shell` tool call
- which run a background process should ultimately be charged to

So resource monitoring naturally has two layers:

1. **OS accounting layer**
   - resource usage of processes, threads, process groups and cgroups
2. **Agent attribution layer**
   - which tool call / task / run owns those resources

### 5.6 Which layer the current implementation covers

For the current `kimi-cli` implementation:

- OS accounting layer
  - mainly from `/proc` sampling
  - currently prefers `process_group`
  - tries `cgroup` when the environment allows
- Agent attribution layer
  - via `tool_call_id`
  - `owner_role`
  - `owner_agent_id`
  - `task_id`
  - `parent_tool_call_id`
  - mapped back to `run`

The current resource accounting flow is therefore:

```text
shell / background task
  -> pid / pgid
  -> process_group resource sample
  -> shell_resource_usage / runtime.json
  -> owning run.resource_usage
```

### 5.7 Recommended direction

The best long-term approach is:

1. When the agent starts a subagent / task / shell, create a dedicated `cgroup` for it
2. All processes produced by that task enter this `cgroup`
3. At the end, read directly:
   - `cpu.stat`
   - `memory.current`
   - `memory.peak`
   - `io.stat`
4. Then map the results to the `run`

This combines:

- the OS's precise accounting
- the agent's high-level semantic attribution
- `cgroup`
  - is the resource container that limits, accounts for and attributes a group of processes together

The recommended accounting principles are therefore:

1. `run` carries the final resource attribution
2. `tool_call` / `background_task` records local resource events
3. Actual sampling is preferably done at the `cgroup` level
4. Without `cgroup`, fall back to `process_group` first
5. If `process_group` is not feasible either, fall back to `PID tree`

## 5. `process_group` approximation

### 5.1 Principle

The core of the `process_group` approach is:

- record the `pgid` of each shell / background task
- scan the system process table for all processes with that `pgid`
- aggregate the CPU / memory / I/O of that group
- write the result back to the owning `run`

It can be understood as:

```text
task/run
  -> root child process
  -> child_pgid
  -> all processes with the same pgid
  -> aggregated resource usage
```

This is clearly more sensible than "watching a single pid", because the real load launched by a shell is often in later child processes.

### 5.2 Current role

In the current AOS-Bench implementation:

- `cgroup` is the preferred approach
- but many ordinary user sessions have no `cgroup` delegation
- so `process_group` is the more practical default approximation for now

It is especially suitable for:

- foreground shell commands
- background bash tasks
- short chains of tool executions launched from a shell

### 5.3 Limitation

`process_group` is not a strict resource container. Its limitations must be stated explicitly:

1. It is not a kernel-level resource ledger
   - it is only a process grouping in the job control sense
   - unlike `cgroup`, it is not naturally suited to limiting and metering

2. It may miss processes that leave the original group
   - a child process that changes its own `pgid`
   - or enters a new session / process group
   - may escape the current accounting scope

3. It is unreliable for short-lived processes
   - it depends on scanning `/proc` within a sampling window
   - very short-lived children may exit between two samples

4. It cannot express strict concurrent isolation
   - suitable for "approximate estimation"
   - not suitable as a strongly enforced quota implementation

5. It is sensitive to the runtime environment
   - different shells / exec models / job control settings
   - affect whether the `pgid` boundary is stable

Conclusion:

- `process_group` is sufficient as the benchmark's current approximate resource estimation
- but its results should be marked `approximate`
- if the environment allows, `cgroup` should still be preferred

## 6. `kimi-cli` implementation

### 6.1 Natively collectable objects

`kimi-cli` can currently read the following natively and reliably:

1. root session trace
   - `wire.jsonl`
2. subagent instance trace
   - `subagents/<agent_id>/wire.jsonl`
   - `subagents/<agent_id>/meta.json`
   - `subagents/<agent_id>/context.jsonl`
   - `subagents/<agent_id>/prompt.txt`
   - `subagents/<agent_id>/output`
3. background task metadata
   - `tasks/<task_id>/spec.json`
   - `tasks/<task_id>/runtime.json`
   - `tasks/<task_id>/output.log`

This data is enough to reconstruct:

- the run tree
- the `parent_tool_call_id -> child subagent run` relation
- foreground / background launch mode
- token usage / context usage
- task lifecycle and PID / PGID information

### 6.2 Resource fields directly collectable now

Without modifying the `kimi-cli` source, AOS-Bench can directly collect:

1. run-level token usage
   - from `StatusUpdate.token_usage`
2. run-level wall time
   - from the first and last timestamps of the root/subagent `wire.jsonl`
   - or from time fields in `meta.json` / the task runtime
3. background task PID information
   - `runtime.worker_pid`
   - `runtime.child_pid`
   - `runtime.child_pgid`
4. background task timing and exit status
   - `started_at`
   - `finished_at`
   - `exit_code`
   - `timed_out`

### 6.3 Current gaps

`kimi-cli` does not natively record these key metrics:

- per-run CPU time
- per-run peak RSS
- per-run filesystem I/O
- per-run network I/O
- cumulative resources of the whole child process tree launched by a shell/tool

In addition, `TaskSpec.owner_role` of a background bash task cannot be used alone as a reliable attribution signal. Attribution still has to link back through the tool call and the task metadata.

### 6.4 Recommended implementation

The recommended design for resource attribution is:

```text
subagent run
  -> shell/tool/background task event
  -> process group / process tree
  -> AgentTrace.run.resource_usage
```

Suggested steps:

1. Build the run ownership table
   - parse `SubagentEvent`
   - build `parent_tool_call_id -> agent_id -> run_id`
   - link background tasks back via `tasks/<task_id>/spec.json.tool_call_id`

2. Record task / process boundaries
   - for each background task, read:
     - `task_id`
     - `tool_call_id`
     - `worker_pid`
     - `child_pid`
     - `child_pgid`

3. Collect foreground and background resources
   - foreground shell:
     - try a dedicated `cgroup` first
     - fall back to `process_group` on failure
   - background task:
     - try a dedicated `cgroup` first
     - fall back to `process_group` on failure

4. Fixed sample fields
   - `cpu_time_ms`
   - `memory_current_kb`
   - `memory_peak_kb`
   - `read_bytes`
   - `write_bytes`

5. Write back two layers of data
   - write event-level `metrics` when a tool/task completes
   - aggregate into `run.resource_usage` when the run ends

### 6.5 `cgroup`-first approach

If the runtime environment allows it, `cgroup v2` is preferred.

Recommended model:

```text
root run
  -> /sys/fs/cgroup/aos/root/<run_id>

subagent run
  -> /sys/fs/cgroup/aos/subagent/<run_id>

background task
  -> inherits the cgroup of its owning run
```

Advantages:

- no need to maintain the whole child process tree recursively
- resources of shell / python / pytest / cargo / grep merge naturally
- better suited to uniform limits and uniform accounting in a benchmark

Read these first:

- `cpu.stat`
- `memory.current`
- `memory.peak`
- `io.stat`

### 6.6 Practical constraints

In many ordinary user sessions, the system has `cgroup v2` enabled, but the current process has no permission to create child cgroups.

This means:

- the `cgroup` path should always be kept in the design
- but the implementation must fall back reliably to `process_group`

The appropriate engineering strategy is therefore:

1. Make `process_group` sampling stable and usable first
2. Record explicitly in the trace:
   - `resource_scope = "process_group" | "cgroup" | "pid_tree" | "approximate"`
   - `cgroup_path`
   - the reason for the fallback
3. In VM / CI environments with delegation, make `cgroup` the default path

### 6.7 Recommended instrumentation points

If `kimi-cli` is modified or an adapter is added, the recommended hooks are:

- foreground shell command
  - aos_bench/harness/kimi-cli/src/kimi_cli/tools/shell/__init__.py
- background shell task worker
  - aos_bench/harness/kimi-cli/src/kimi_cli/background/worker.py
- subagent event bridge
  - aos_bench/harness/kimi-cli/src/kimi_cli/subagents/runner.py
- background task metadata
  - aos_bench/harness/kimi-cli/src/kimi_cli/background/models.py
