# AOS-Bench documentation

Start with the repository [README](../README.md), which covers installation, a first run and how to
reproduce the paper. The getting-started guide gives the full setup on a plain Docker host
([English](./getting_started.md), [中文](./getting_started_zh.md)).

## Architecture and protocol

| Topic | Document |
|---|---|
| System overview: directories, one trial end to end, where to change what | [`arch.md`](./arch.md) |
| Stream task format: `stream.toml`, sub-task layout, arrival schedule | [`tasks/task_stream.md`](./tasks/task_stream.md) |
| Agent contract: pool tools, termination, the verifier boundary | [`framework/agent_contract.md`](./framework/agent_contract.md) |
| Dispatcher and task assignment (pool v0.3) | [`framework/task_assign.md`](./framework/task_assign.md) |
| Verification: per-task graders, rewards, parallel grading | [`framework/verification.md`](./framework/verification.md) |

## Tasks and workloads

| Topic | Document |
|---|---|
| Task pool design and the construction pipeline | [`tasks/task_design.md`](./tasks/task_design.md) |

## Trajectories and telemetry

| Topic | Document |
|---|---|
| AgentTrace: one trajectory schema across harnesses | [`framework/agent_trace.md`](./framework/agent_trace.md) |
| Resource telemetry: cgroup sampling and attribution | [`framework/resource_monitor.md`](./framework/resource_monitor.md) |
| Live web monitor for running trials | [`framework/monitor.md`](./framework/monitor.md) |
