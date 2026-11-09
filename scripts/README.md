# scripts/

Executable helpers, grouped by purpose.

| Directory | Purpose | Scripts |
|---|---|---|
| [`sandbox/`](./sandbox/) | End-to-end smoke tests: run one small stream on a backend and check the result | `docker_stream_smoke.sh` (Docker), `docker_kimi_code_smoke.sh` (Kimi Code adapter on Docker), `vm_stream_smoke.sh` (QEMU VM backend) |
| [`data/`](./data/) | Data utilities | `audit_v1_0_trajectories.py`: audits finished stream trials for infrastructure failures (MCP disconnects, API errors, missing trajectories) |
| [`ops/`](./ops/) | Watching long runs | `monitor_aos_stream.py`: follows a running stream trial's dispatcher state and can stop it at a wall-clock limit |

For running the benchmark itself, use `harbor run` as described in the
[README](../README.md) and the [getting-started guide](../docs/getting_started.md).
