# AgentTrace Schema v0.1

## 1. Goals

This document defines AOS-Bench's unified `AgentTrace` schema, which normalizes execution records from different agent runtimes into a single analyzable format.

Design goals:

1. Support different implementation styles
   - CLI agents
   - SDK agents
   - runtimes with explicit subagents
   - runtimes with implicit child sessions or delegated sessions

2. Serve two kinds of consumers
   - debugging, visualization, and post-hoc review
   - benchmark metric extraction and resource-scheduling analysis

3. Keep three layers of data distinct
   - the normalized trace
   - references to raw events
   - references to external artifacts

4. Support multiple agents and subagents natively
   - parent-child sessions
   - delegated tasks and subtasks
   - foreground and background execution

5. Do not force every runtime to produce the same granularity
   - the schema allows an agent that only has steps
   - it also allows an agent that has raw events, tokens, cost, tool timings, and child traces

6. Support resource limits and resource-usage accounting natively
   - root sandbox quotas
   - per-subagent quotas
   - actual wall time, CPU, memory, and I/O usage
   - analysis of resource scheduling across concurrent subagents

## 2. Design Principles

### 2.1 Relationship to Harbor ATIF

This schema draws on Harbor's [RFC 0001: ATIF](../../harbor/rfcs/0001-trajectory-format.md), but does not adopt ATIF as its only format.

The reasons:

- ATIF fits a linear "trajectory as steps" model well
- AOS-Bench also needs to represent explicitly:
  - session trees
  - subagent lifecycles
  - background tasks
  - raw runtime events
  - task-scheduling metadata

The approach is therefore:

- **AgentTrace** as the unified internal format of AOS-Bench
- an **ATIF-compatible projection** as a downstream export layer

That is:

```text
runtime-native trace
    -> AgentTrace
    -> optional ATIF export
```

### 2.2 Sessions, runs, and events at the core

The unified format does not treat the "step" as its only central object. It is organized in three layers:

1. `trace`
   The complete record of one benchmark run

2. `runs`
   One or more independently executing, directly attributable agent runtime units
   - root agent run
   - subagent run
   - resumed subagent run

3. `events`
   The structured event stream inside each run

A `run` is closest to:

- an independent agent loop
- an execution unit whose tokens, wall time, and resource usage can be counted separately
- a node that a parent run can spawn, resume, terminate, and be attributed to

This lets the format represent both:

- `kimi-cli`'s `wire.jsonl + subagents/<id>/...`
- `opencode`'s `parent session + child session + message parts`

## 3. Top-Level Structure

```json
{
  "schema_version": "AgentTrace-v0.1",
  "trace_id": "...",
  "benchmark": { "...": "..." },
  "agent_system": { "...": "..." },
  "root_run_id": "run_root_001",
  "runs": [],
  "artifacts": [],
  "summary": {},
  "notes": "...",
  "extra": {}
}
```

## 4. Top-Level Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | string | yes | Format version, e.g. `AgentTrace-v0.1` |
| `trace_id` | string | yes | Globally unique ID of one benchmark trace |
| `benchmark` | object | no | Benchmark run context |
| `agent_system` | object | yes | Agent runtime identification |
| `root_run_id` | string | yes | ID of the root agent run |
| `runs` | array | yes | All run nodes |
| `artifacts` | array | no | References to external files |
| `summary` | object | no | Aggregate metrics |
| `notes` | string | no | Free-form notes |
| `extra` | object | no | Other custom fields |

## 5. `benchmark`

Suggested fields:

```json
{
  "suite": "AOS-Bench",
  "task_type": "stream",
  "task_id": "aos-bench-sample-v1",
  "trial_id": "trial_001",
  "job_id": "job_001",
  "vm_id": "vm_001",
  "sandbox": {
    "kind": "vm+docker",
    "resource_limits": {
      "cpus": 4,
      "memory_mb": 8192,
      "storage_mb": 30720,
      "pids": null,
      "network": "disabled"
    },
    "allow_internet": false
  }
}
```

Notes:

- Benchmark metadata is not part of the agent itself, but it matters for trace analysis
- This object records the execution environment, not step content

## 6. `agent_system`

Identifies the integrated agent:

```json
{
  "name": "kimi-cli",
  "version": "1.39.0",
  "entrypoint": "kimi_cli.__main__:main",
  "default_model": "kimi-k2",
  "trace_source": "native",
  "extra": {}
}
```

Suggested fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Agent system name, e.g. `kimi-cli` / `opencode` |
| `version` | string | yes | Agent version |
| `entrypoint` | string | no | Executable entry point |
| `default_model` | string | no | Default model |
| `trace_source` | string | no | `native` / `derived` / `mixed` |
| `extra` | object | no | Additional runtime information |

## 7. `runs`

`runs` is the core of the schema. Each `run` represents an independently executing, directly attributable agent runtime.

A `run` is not:

- an OS process
- a thread
- a tool call
- a background task

Nor does a `run` map directly to one fixed runtime concept. For example:

- in `kimi-cli`, a `run` is closest to a subagent instance
- in `opencode`, a `run` is closest to a child session

The schema therefore uses `run` as a unified abstraction layer and distinguishes cases with fields:

- who executes: `role`
- how it is scheduled: `launch_mode`
- which native session container it runs in: `session_mode`

### 7.1 Run structure

```json
{
  "run_id": "run_sub_001",
  "parent_run_id": "run_root_001",
  "source_event_id": "evt_123",
  "role": "subagent",
  "launch_mode": "background",
  "session_mode": "same_session",
  "agent_name": "explore",
  "model": {
    "provider": "moonshot",
    "name": "kimi-k2"
  },
  "status": "completed",
  "task": {
    "description": "search api handlers",
    "prompt": "find all API entrypoints",
    "task_id": "a123",
    "subagent_type": "explore"
  },
  "timing": {
    "created_at": 0,
    "started_at": 0,
    "ended_at": 0
  },
  "resource_limits": {},
  "resource_usage": {},
  "metrics": {},
  "events": [],
  "artifacts": [],
  "extra": {}
}
```

### 7.2 Run fields

| Field | Type | Required | Description |
|---|---|---|---|
| `run_id` | string | yes | Unique ID of this run |
| `parent_run_id` | string\|null | no | Parent run ID; may be null for the root run |
| `source_event_id` | string\|null | no | ID of the parent event that triggered this run |
| `role` | string | yes | `root` / `subagent` / `system` |
| `launch_mode` | string | no | `foreground` / `background` / `inline` |
| `session_mode` | string | no | `same_session` / `child_session` / `unknown` |
| `agent_name` | string | yes | Logical agent name, e.g. `build` / `explore` |
| `model` | object | no | Model actually used |
| `status` | string | yes | `created` / `running` / `completed` / `failed` / `killed` / `idle` |
| `task` | object | no | Description of the task or subtask this run handles |
| `timing` | object | no | Creation, start, and end times |
| `resource_limits` | object | no | Resource quotas and limits available to this run |
| `resource_usage` | object | no | Actual resource consumption of this run |
| `metrics` | object | no | Aggregate metrics |
| `events` | array | yes | Event stream of this run |
| `artifacts` | array | no | File references of this run |
| `extra` | object | no | Other fields |

### 7.3 Why `run` is more general than `session`

Runtimes use different concepts:

- `kimi-cli` is closer to subagent instances
- `opencode` is closer to child sessions
- the Claude SDK may later look more like internal delegated execution

`run` is the unified abstraction; each runtime can keep its native fields in `extra`:

```json
{
  "session_mode": "child_session",
  "extra": {
    "native_session_id": "...",
    "native_agent_id": "...",
    "native_task_id": "..."
  }
}
```

`session_mode` states which session boundary hosts this `run` in the native runtime:

- `same_session`
  - the run shares one native session with its parent
  - typical example: a `kimi-cli` subagent
- `child_session`
  - the run executes in a separate child session
  - typical example: the `opencode` task tool
- `unknown`
  - the runtime cannot determine this reliably, or the concept does not apply

### 7.4 Why `resource_limits` and `resource_usage` are separate

This distinction is central to benchmark analysis.

- `resource_limits`
  states how much the run is allowed to use
- `resource_usage`
  states how much it actually used

Mixing the two would make it impossible to answer later:

- whether a subagent was constrained by its limits
- whether a subagent actually saturated the resources available to it
- whether the orchestrator allocated the concurrency budget sensibly

For example:

```json
{
  "resource_limits": {
    "cpus": 1,
    "memory_mb": 2048
  },
  "resource_usage": {
    "wall_time_ms": 18342,
    "cpu_time_ms": 9211,
    "memory_peak_mb": 612
  }
}
```

## 7A. Resource Monitoring

The `AgentTrace` schema keeps the `resource_limits` and `resource_usage` fields, but the resource-monitoring design itself is specified in a separate document:

- [resource_monitor.md](./resource_monitor.md)

That document covers:

- suggested fields for `resource_limits` / `resource_usage`
- the run-level resource attribution model
- the `subagent -> shell -> child processes -> resource scope` relationship diagram
- three sampling paths: `cgroup`, `process_group`, and `PID tree`
- limitations of the `process_group` approximation
- implementation guidance for resource collection in `kimi-cli`

One minimal set of principles applies here:

1. `resource_limits` states how much may be used
2. `resource_usage` states how much was actually used
3. resources are ultimately attributed to a `run`
4. a parent run does not, by default, recursively aggregate the resources of its child runs

## 8. `events`

`events` is the structured event stream inside a run.  
Not every runtime is required to emit the same detailed set of event kinds, but all share a common event envelope.

### 8.1 Event structure

```json
{
  "event_id": "evt_001",
  "seq": 1,
  "timestamp": 1777026174.27,
  "kind": "tool_call",
  "phase": "completed",
  "step_id": "step_001",
  "parent_event_id": null,
  "payload": {},
  "metrics": {},
  "raw_ref": null,
  "extra": {}
}
```

### 8.2 Common fields

| Field | Type | Required | Description |
|---|---|---|---|
| `event_id` | string | yes | Unique event ID |
| `seq` | integer | yes | Monotonically increasing sequence number within the run |
| `timestamp` | number | no | Unix timestamp |
| `kind` | string | yes | Event kind |
| `phase` | string | no | `begin` / `update` / `completed` / `error` |
| `step_id` | string\|null | no | ID of the step this event belongs to |
| `parent_event_id` | string\|null | no | ID of the enclosing event |
| `payload` | object | yes | Normalized payload |
| `metrics` | object | no | Event-level metrics |
| `raw_ref` | object\|null | no | Pointer to the raw event |
| `extra` | object | no | Extension fields |

### 8.3 Recommended event kinds

| `kind` | Meaning |
|---|---|
| `turn_begin` | An agent turn begins |
| `turn_end` | An agent turn ends |
| `step_begin` | A step begins |
| `step_end` | A step ends |
| `message` | user/assistant/system text message |
| `reasoning` | reasoning/thinking content |
| `tool_call` | Tool call |
| `tool_result` | Tool result |
| `subagent_spawn` | A child agent is spawned |
| `subagent_event` | Event bridged from a child agent |
| `status` | context/tokens/status update |
| `approval_request` | Permission approval request |
| `approval_response` | Permission approval result |
| `background_task` | Background task status event |
| `hook` | Hook triggered and completed |
| `compaction` | Context compaction |
| `error` | Error |

A runtime need not support all of these kinds, but the kinds it does support should use this shared namespace.

## 9. `step_id`

To accommodate runtimes that have both steps and events, the schema lets events belong to a step.

### 9.1 Steps are not a top-level array

`steps` is not placed at the root. Instead, it is an optional grouping key on events. The reasons:

- `kimi-cli`'s wire stream is naturally event-driven
- `opencode`'s messages and parts also behave like an event sequence
- requiring every runtime to reconstruct steps first and then attach events would lose information

### 9.2 When the runtime has native steps

For example:

- `kimi-cli`: `StepBegin`
- `opencode`: `step-start` / `step-finish`

In that case:

- generate one `step_id` per step
- assign the same `step_id` to all events that belong to that step

## 10. `payload` Specification

To accommodate the different `kind` values, `payload` is a discriminated object.

### 10.1 `message`

```json
{
  "role": "assistant",
  "content": "..."
}
```

### 10.2 `tool_call`

```json
{
  "tool_call_id": "call_001",
  "tool_name": "Read",
  "arguments": {},
  "title": "Read file"
}
```

### 10.3 `tool_result`

```json
{
  "tool_call_id": "call_001",
  "tool_name": "Read",
  "output": "...",
  "status": "completed"
}
```

### 10.4 `subagent_spawn`

This is one of the most important events in the unified schema.

```json
{
  "child_run_id": "run_sub_001",
  "subagent_type": "explore",
  "description": "search api handlers",
  "prompt": "find all API entrypoints",
  "launch_mode": "foreground",
  "task_id": "native_task_or_session_id"
}
```

### 10.5 `subagent_event`

```json
{
  "child_run_id": "run_sub_001",
  "child_event_id": "evt_sub_012",
  "forwarded_kind": "tool_call"
}
```

### 10.6 `status`

```json
{
  "context_tokens": 12345,
  "max_context_tokens": 200000,
  "plan_mode": false
}
```

## 10A. Runtime Implementation

This section describes how each runtime is mapped into `AgentTrace`.

### 10A.1 kimi-cli

`kimi-cli` already provides good observability of subagent events.

The design and implementation that relate directly to resource monitoring are specified separately in:

- [resource_monitor.md](./resource_monitor.md)

This section covers only the trace mapping and event modeling. Resource attribution, the `cgroup` / `process_group` / `PID tree` approaches, and the resource-collection hooks for `kimi-cli` are all maintained in `resource_monitor.md`, so that the two documents do not drift apart.

#### 10A.1.1 Natively collectable objects

`kimi-cli` natively and reliably exposes:

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

This data is sufficient to reconstruct:

- the run tree
- the `parent_tool_call_id -> child subagent run` relationship
- foreground / background launch mode
- token usage and context usage
- task lifecycles and PID information

#### 10A.1.2 `AgentTrace.runs` mapping

Suggested mapping rules:

1. root session -> one root run
2. each `subagents/<agent_id>` -> one subagent run
3. if the same `agent_id` is resumed:
   - a simple implementation may still map it to the same run
   - if a precise lifecycle is needed, it can be split into multiple run segments
4. background agent tasks
   - still belong to the run of the corresponding `agent_id`
   - `tasks/<task_id>` serves as an artifact and event source of that run

For `kimi-cli`, `session_mode` should default to:

```json
{
  "session_mode": "same_session"
}
```

because its subagent runtime reuses the parent session instead of creating a child session.

Recommended native fields to keep:

```json
{
  "extra": {
    "native_session_id": "...",
    "native_agent_id": "a1234567",
    "native_task_id": "agent-001",
    "native_paths": {
      "wire": ".../wire.jsonl",
      "meta": ".../meta.json",
      "task_runtime": ".../runtime.json"
    }
  }
}
```

#### 10A.1.3 `events` mapping

For `kimi-cli`, map by the message type in `wire.jsonl`:

- `TurnBegin` -> `turn_begin`
- `TurnEnd` -> `turn_end`
- `StepBegin` -> `step_begin`
- `StatusUpdate` -> `status`
- `ToolCall` -> `tool_call`
- `ToolResult` -> `tool_result`
- `SubagentEvent` -> `subagent_event`
- `HookTriggered` / `HookResolved` -> `hook`
- background task status changes -> `background_task`

`SubagentEvent` records are kept as bridged events, and during trace construction they are also converted into:

- one `subagent_spawn` on the parent run
- an independent event stream on the child run

#### 10A.1.4 Recommended output

For `kimi-cli`, the final export should have two layers:

1. the normalized `AgentTrace`
2. references to the native trace

For example:

```json
{
  "artifacts": [
    {
      "kind": "native_wire",
      "path": "sessions/.../wire.jsonl"
    },
    {
      "kind": "native_subagent_wire",
      "path": "sessions/.../subagents/a123/wire.jsonl"
    },
    {
      "kind": "native_background_task_runtime",
      "path": "sessions/.../tasks/bash-001/runtime.json"
    }
  ]
}
```

This keeps the native `kimi-cli` on-disk format reachable for later debugging and review.

### 10A.2 Claude Code

The source of truth for Claude Code is
`agent/sessions/projects/<project>/<session>/*.jsonl`. The root and sidechains may be
written to the same transcript, and sidechains may also be mirrored to `subagents/agent-*.jsonl`.

- `isSidechain=false` -> `run_root_001`
- `isSidechain=true` + `agentId` -> `run_sub_<agentId>`
- `Task` / `Agent` tool -> `subagent_spawn` on the parent run
- `assistant` / `user` / `tool_use` / `tool_result` -> the unified event shape

The offline collector is `aos_bench.trace.claude_code`; the live monitor uses the same
`IncrementalClaudeParser`. The collector deduplicates by native UUID, keeps the transcript
as an artifact, and links each spawn event's `child_run_id` to its sidechain run.

### 10A.3 Codex

The source of truth for Codex is
`agent/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl`. Each `codex exec` invocation and each
`spawn_agent` worker writes its own rollout.

- rollout without `parent_thread_id` -> `run_root_001`
- any other rollout -> `run_sub_<full thread id>`
- `parent_thread_id` -> `parent_run_id`
- `agent_id` in the `spawn_agent` output -> `subagent_spawn.child_run_id`

The offline collector is `aos_bench.trace.codex`, which shares
`IncrementalCodexParser` with the live monitor. Run IDs must keep the full thread UUID
without truncation; otherwise spawn events cannot be linked to their child rollouts.

### 10A.4 Storage and consumption

The unified pipeline is:

```text
harness-native logs
  -> shared incremental parser
  -> canonical run/event semantics
       |-> live MonitorState -> unified monitor
       `-> AgentTrace-v0.1 (agent/agent_trace.json) -> offline SVG
```

The live monitor does not wait for the trial to finish or for `agent_trace.json` to be fully
written; it consumes incremental events from the shared parser directly. The offline collector
builds the complete AgentTrace with the same parser, so both paths agree on run-tree, event-shape,
and spawn-link semantics. The live monitor does not, however, depend on the AgentTrace JSON file.

Batch backfill command:

```bash
python -m aos_bench.trace.batch_convert <jobs-or-job-dir> [--force] [--svg]
```

The `agent/trajectory.json` that Harbor generates itself is an ATIF-compatible artifact and is
not the same as AgentTrace. ATIF can continue to serve linear verifier and metrics use cases;
for multi-run trees, AgentTrace and the native logs are authoritative.

## 11. `metrics`

`metrics` can appear at:

- run level
- event level
- the root `summary`

Recommended common fields:

```json
{
  "prompt_tokens": 0,
  "completion_tokens": 0,
  "reasoning_tokens": 0,
  "cached_tokens": 0,
  "cost_usd": 0.0,
  "tool_wall_time_ms": 0,
  "wall_time_ms": 0
}
```

### 11.1 Common fields

| Field | Description |
|---|---|
| `prompt_tokens` | Input tokens |
| `completion_tokens` | Output tokens |
| `reasoning_tokens` | Reasoning tokens, if the runtime reports them |
| `cached_tokens` | Cache-hit tokens |
| `cost_usd` | Cost |
| `wall_time_ms` | Total elapsed time |
| `tool_wall_time_ms` | Time spent in tools |
| `cpu_time_ms` | Optional, CPU time |
| `memory_peak_mb` | Optional, peak memory |

### 11.2 Additional summary fields

The top-level `summary` should also include:

```json
{
  "total_runs": 3,
  "total_events": 120,
  "subagent_count_total": 2,
  "subagent_count_background": 1,
  "peak_parallel_subagents": 2,
  "total_prompt_tokens": 0,
  "total_completion_tokens": 0,
  "total_cost_usd": 0.0,
  "total_wall_time_ms": 0
}
```

## 12. `raw_ref`

The unified schema should not discard the native trace.

Each event can therefore carry a `raw_ref`:

```json
{
  "source": "kimi-cli-wire",
  "path": "sessions/.../wire.jsonl",
  "offset": 123,
  "native_type": "SubagentEvent"
}
```

or:

```json
{
  "source": "opencode-db",
  "table": "message_parts",
  "id": "part_001",
  "native_type": "SubtaskPart"
}
```

This way the unified trace supports analysis and can still be traced back to the raw data.

## 13. `artifacts`

Stores references to unstructured external outputs.

```json
[
  {
    "artifact_id": "art_001",
    "run_id": "run_sub_001",
    "kind": "wire_log",
    "path": "subagents/a123/wire.jsonl"
  }
]
```

Common `kind` values:

- `wire_log`
- `context_log`
- `stdout`
- `stderr`
- `prompt`
- `snapshot`
- `patch`
- `session_export`

## 14. Mapping for `kimi-cli`

### 14.1 Top level

- one root session -> one root run
- each `subagents/<agent_id>` -> one child run

### 14.2 run

- `run_id` <- `agent_id` or the root session id
- `parent_run_id` <- the root run of the parent session
- `launch_mode` <- `running_foreground` / `running_background`
- `status` <- `meta.json.status`

### 14.3 event

- each record in `wire.jsonl` -> one or more `events`
- `SubagentEvent` -> `kind = subagent_event`
- `StepBegin` / `StepEnd` -> step grouping

### 14.4 artifacts

- `wire.jsonl`
- `context.jsonl`
- `prompt.txt`
- `output`

## 15. Mapping for `opencode`

### 15.1 Top level

- one parent session tree -> one trace

### 15.2 run

- each session -> one run
- `parent_run_id` <- `session.parentID`

### 15.3 event

- `message-v2 part` -> event
- `SubtaskPart` -> `kind = subagent_spawn`
- `StepStartPart` / `StepFinishPart` -> step grouping
- tool state transitions -> `tool_call` / `tool_result`

### 15.4 metrics

- `StepFinishPart.cost/tokens`
- `message metadata.tool.time`

## 16. Relationship to ATIF

`AgentTrace` can be exported to ATIF, but the mapping is not one-to-one or lossless.

### 16.1 Parts that map easily

- root trace -> trajectory
- step-grouped events within a run -> ATIF steps
- tool_call / tool_result
- run summary -> final_metrics

### 16.2 Parts that do not map directly

- multiple child runs
- background tasks
- the raw event bridge
- the parent-child run tree

The recommendation is therefore:

- `AgentTrace` as the primary specification
- `ATIF export` as a compatibility export view

## 17. Minimal Required Subset

To make integrating a harness easy, the schema defines a minimal required subset:

### 17.1 Top level

- `schema_version`
- `trace_id`
- `agent_system.name`
- `root_run_id`
- `runs`

### 17.2 Each run

- `run_id`
- `role`
- `agent_name`
- `status`
- `events`

### 17.3 Each event

- `event_id`
- `seq`
- `kind`
- `payload`

With this subset, even a minimal integration produces a trace in the unified analysis format.

## 18. Open Questions

Version 0.1 leaves the following questions open:

1. whether `run` should be renamed to `execution`
2. whether `step` should become an explicit object instead of an event attribute
3. whether `raw_ref` is sufficient, or an inline `raw_event` option is needed
4. whether `artifacts` should distinguish trace artifacts from benchmark artifacts
5. whether `summary` should be required to include subagent statistics
6. whether `resource_limits` / `resource_usage` are sufficient, or a finer-grained resource model is needed

## 19. Integration Guidance

1. All four integrated runtimes share the monitor parsers and the AgentTrace event semantics
2. For a new runtime, keep its native logs first, then implement the parser and collector
3. Narrow the field set based on practice, rather than constraining the schema too tightly from the start

The priority is not a perfect schema, but one that can:

- reliably represent the subagent tree
- unify tools, steps, and events
- preserve the ability to trace back to the raw trace
- explicitly express each subagent's resource limits and actual consumption

## 20. Complex Example

This section gives a more complex nested example that shows where a `run` sits in scenarios involving multiple agents, subagents, child sessions, and background tasks.

### 20.1 Scenario

Suppose a user gives the root orchestrator a compound task:

1. fix an API authentication bug
2. search the related code paths
3. run the tests and keep monitoring the results
4. summarize the changes

The example uses a generic runtime rather than tying itself to the specific implementation of `kimi-cli` or `opencode`.

The root agent schedules the work as follows:

- the root run handles overall planning and the final summary
- it dispatches a foreground subagent `explore` to inspect the routing, middleware, and authentication code
- it dispatches a background subagent `test-runner` to keep running the tests in the background
- during its analysis, `explore` spawns a new child run `docs-scan`
  - in some runtimes, this child run may be hosted in a separate child session
- `test-runner` starts a background shell task that runs `pytest -q`

### 20.2 Execution graph

```text
trace: trace_bugfix_001

run_root_001  (role=root, agent_name=orchestrator, launch_mode=inline)
  |
  |-- evt_010 subagent_spawn
  |      child_run_id=run_sub_101
  |
  |-- evt_011 subagent_spawn
  |      child_run_id=run_sub_102
  |
  `-- evt_040 message
         final summary to user

run_sub_101  (role=subagent, agent_name=explore, launch_mode=foreground, session_mode=child_session)
  |
  |-- evt_101 tool_call(Read/AuthMiddleware.py)
  |-- evt_102 tool_result(...)
  |-- evt_110 subagent_spawn
  |      child_run_id=run_sub_201
  `-- evt_130 message
         "root cause is missing tenant check"

run_sub_201  (role=subagent, agent_name=docs-scan, launch_mode=foreground, session_mode=child_session)
  |
  |-- evt_201 tool_call(Grep, "tenant auth")
  |-- evt_202 tool_result(...)
  `-- evt_210 message
         "RFC says tenant must be checked before session load"

run_sub_102  (role=subagent, agent_name=test-runner, launch_mode=background, session_mode=child_session)
  |
  |-- evt_301 background_task
  |      task_id=task_pytest_001
  |      child_pid=48231
  |-- evt_302 status
  |      "running tests in background"
  `-- evt_330 message
         "12 failed -> 2 failed after patch"

task_pytest_001  (not a run; artifact/event source owned by run_sub_102)
  |
  `-- process tree
         bash -c pytest -q
            `-- python -m pytest
                  |-- worker proc #1
                  `-- worker proc #2
```

### 20.3 The role of each object in the graph

- `trace`
  - the complete execution graph of one benchmark rollout
- `run_root_001`
  - the execution unit of the root orchestrator
- `run_sub_101`
  - a foreground subagent run dispatched by the root
- `run_sub_201`
  - a child run dispatched in turn by `run_sub_101`
  - in some runtimes it corresponds to a child session; in others it may be only an independent execution unit within the same session
- `run_sub_102`
  - a background subagent run dispatched by the root
- `task_pytest_001`
  - not a new run, but a background task started inside `run_sub_102`
- `process tree`
  - the low-level objects that actually consume OS resources; their resources are ultimately attributed to the owning run

The example shows that:

- a `run` is an execution node in the graph
- `subagent_spawn` / `parent_run_id` / `child_run_id` provide the edges between nodes
- a `task` and a `process tree` are usually not first-class execution nodes in the graph, unless the runtime explicitly models them as independent agent executions

### 20.4 Corresponding AgentTrace structure

The example can be abstracted into the following skeleton:

```json
{
  "trace_id": "trace_bugfix_001",
  "root_run_id": "run_root_001",
  "runs": [
    {
      "run_id": "run_root_001",
      "parent_run_id": null,
      "role": "root",
      "agent_name": "orchestrator",
      "launch_mode": "inline",
      "session_mode": "same_session"
    },
    {
      "run_id": "run_sub_101",
      "parent_run_id": "run_root_001",
      "source_event_id": "evt_010",
      "role": "subagent",
      "agent_name": "explore",
      "launch_mode": "foreground",
      "session_mode": "child_session"
    },
    {
      "run_id": "run_sub_201",
      "parent_run_id": "run_sub_101",
      "source_event_id": "evt_110",
      "role": "subagent",
      "agent_name": "docs-scan",
      "launch_mode": "foreground",
      "session_mode": "child_session"
    },
    {
      "run_id": "run_sub_102",
      "parent_run_id": "run_root_001",
      "source_event_id": "evt_011",
      "role": "subagent",
      "agent_name": "test-runner",
      "launch_mode": "background",
      "session_mode": "child_session"
    }
  ]
}
```

### 20.5 Resource attribution

The key point in this example is to avoid double counting.

#### 20.5.1 root run

`run_root_001.resource_usage` should include:

- the root's own token usage
- the root's own tool calls
- the root's own wall time while waiting and summarizing

It should not directly include the resources of these child runs:

- `run_sub_101`
- `run_sub_201`
- `run_sub_102`

Otherwise the root run and the child runs would be double counted.

#### 20.5.2 foreground explore subagent

`run_sub_101.resource_usage` should include:

- `explore`'s own token usage
- the resources of direct tool calls such as `Read/Grep/Edit`
- if it starts a foreground shell command itself, the resources of that command and its child process tree

It should not include:

- the independent resources of the child run `run_sub_201`

That is:

```text
run_sub_101 = explore self usage + owned tool usage
run_sub_201 = docs-scan self usage + owned tool usage
```

#### 20.5.3 background test-runner subagent

`run_sub_102.resource_usage` should include:

- `test-runner`'s own token usage
- the resources of the background task `task_pytest_001`
- the resources of the `pytest` process tree and its worker child processes

The key point here is:

```text
task_pytest_001 -> owned by run_sub_102
process tree of pytest -> owned by run_sub_102
```

That is:

- `task_pytest_001` is not an independent run
- but the resources of the programs it starts must be attributed to `run_sub_102`

#### 20.5.4 Delegation-tree total

An analyst who wants the total resources used by the whole task tree should aggregate at the analysis layer:

```text
total_tree_usage(run_root_001)
  = usage(run_root_001)
  + usage(run_sub_101)
  + usage(run_sub_201)
  + usage(run_sub_102)
```

rather than expecting `run_root_001.resource_usage` to carry this total by itself.

### 20.6 Benchmark questions this answers

With this graph and the corresponding trace, AOS-Bench can answer:

1. whether the root agent delegated independent tasks to suitable subagents
2. whether it used both foreground and background delegation
3. which run is the resource hotspot
4. whether the test task's resources are correctly attributed to `test-runner`
5. whether nested child sessions actually improved throughput, or only added token cost

### 20.7 Recommended derived metrics

Based on this example, the following metrics are recommended for derivation from `AgentTrace`:

- `subagent_count_total`
- `subagent_count_foreground`
- `subagent_count_background`
- `max_run_depth`
- `max_parallel_runs`
- `run_wall_time_ms_by_agent_name`
- `run_cpu_time_ms_by_agent_name`
- `background_task_count`
- `background_task_cpu_time_ms`
- `delegation_overhead_tokens`

Where:

- `max_run_depth`
  - indicates whether nested child sessions occurred
- `max_parallel_runs`
  - indicates whether the orchestrator actually scheduled work in parallel
- `delegation_overhead_tokens`
  - indicates the extra coordination cost paid for splitting the task
