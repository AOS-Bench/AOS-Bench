# AOS-Bench Task Stream Format v1.1

## 1. Overview

The core evaluation unit of AOS-Bench is a **Task Stream**: a set of tasks injected into a single agent session according to a predefined step schedule.

Compared with the previous version, the main changes in v1.1 are:

- **Tasks no longer get their own workspace**
- **There is no longer a distinction between task workspaces and a shared workspace**
- **All initial files are placed under `/workspace/`**
- Task arrival only "unlocks the instruction"; it does not create a new directory tree

This makes a task stream behave more like a shared OS environment:

- The agent sees all visible files through the same `/workspace/`
- Each task identifies the paths it cares about only through its instruction
- The benchmark no longer requires the agent to learn an extra path protocol such as `/workspace/{task_id}`

---

## 2. Directory Structure

```text
<stream_name>/
├── stream.toml
├── environment/
│   ├── Dockerfile
│   └── ...                    # files needed to initialize the shared environment
└── tasks/
    ├── <task_id_1>/
    │   ├── instruction.md
    │   └── tests/
    │       ├── test.sh
    │       └── test_outputs.py
    └── <task_id_2>/
        ├── instruction.md
        └── tests/
            ├── test.sh
            └── test_outputs.py
```

**Key constraints:**

- `environment/Dockerfile` is built once, and all tasks run in the same container
- `environment/` is responsible for placing all initial files the stream needs under `/workspace/`
- `tasks/<task_id>/` holds only that task's instruction and verifier
- **Tasks no longer own a dedicated directory**
- There is no benchmark convention for a `/workspace/<task_id>/` level

---

## 3. Runtime Semantics

### 3.1 Shared Workspace

After the container starts, the agent sees a single shared workspace:

```text
/workspace/
  ...
```

All task-related files live directly at paths under `/workspace/`, for example:

- `/workspace/personal-site/...`
- `/workspace/plus_comm.v`
- `/workspace/ssl/server.crt`

Task authors should keep the paths used by different tasks from colliding as far as possible. If a collision is intentional, the instruction should state it explicitly.

### 3.2 Meaning of Arrival

`arrival.step` means:

- the task's **instruction** is injected into the agent at that step
- it does not mean the task's files first appear at that step

Some task-related files can therefore already be present in `/workspace/` when the session starts. The agent may inspect the file system in advance, but it receives the formal task description only after arrival.

### 3.3 Verifier Scope

Verifiers still run per task:

- `tasks/<task_id>/tests/test.sh`
- `tasks/<task_id>/tests/test_outputs.py`

However, a verifier checks target paths in the shared workspace, not a private task workspace.

---

## 4. stream.toml

```toml
version = "1.0"

[metadata]
name        = "aos-bench-sample-v1"
description = "Three-task sample stream for AOS-Bench v1.1"
author      = "AOS-Bench"

[environment]
cpus           = 2
memory_mb      = 4096
allow_internet = false

[session]
max_steps = 50

[[arrivals]]
step          = 0
task_id       = "fix-git"

[[arrivals]]
step          = 6
task_id       = "prove-plus-comm"

[[arrivals]]
step          = 10
task_id       = "openssl-selfsigned-cert"
```

> **Deprecated**: Earlier versions supported `[[arrivals]].deadline_step` (v1.0) and
> `[[arrivals]].deadline_wall_time` (v1.1) as per-task soft limits. Both have been
> removed. Scoring is now based on "number of tasks passed + overall completion time".
> The prompt no longer exposes any deadline concept to the agent, which is expected
> to finish as quickly as it can on its own.

### 4.1 Fields

| Field | Type | Description |
|------|------|------|
| `[environment]` | — | Shared sandbox configuration for the stream |
| `[session].max_steps` | int | Maximum number of LLM calls in the session |
| `[[arrivals]].step` | int | Step at which the instruction is injected |
| `[[arrivals]].task_id` | str | Corresponds to `tasks/<task_id>/` |

---

## 5. instruction.md

The instruction describes only the task goal. It does not require the agent to enter a task-specific workspace first.

```markdown
# Task: openssl-selfsigned-cert

Create a self-signed certificate using OpenSSL.

Required output paths:
- /workspace/ssl/server.key
- /workspace/ssl/server.crt
- /workspace/ssl/server.pem
- /workspace/ssl/verification.txt
- /workspace/check_cert.py

When finished, call `todo_update("openssl-selfsigned-cert", "complete")`.
```

Here a task is defined by a "set of target paths", not by a "directory mapping".

---

## 6. tests/test.sh

Each task verifier runs independently and reads the target files from the shared workspace.

```bash
#!/usr/bin/env bash
set -euo pipefail

apt-get update
apt-get install -y curl
curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh
source "$HOME/.local/bin/env"

uvx \
  -p 3.13 \
  -w pytest==8.4.1 \
  -w pytest-json-ctrf==0.3.5 \
  pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA

if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
```

---

## 7. Path Conventions

### 7.1 Paths Visible to the Agent

```text
/workspace/                  # file system view shared by all tasks
/tasks/                      # read-only mount holding instructions / verifiers
/logs/                       # standard Harbor log directory
```

### 7.2 Verifier Output

Recommended output for a single-task verifier:

| Path | Description |
|------|------|
| `/logs/verifier/<task_id>/reward.txt` | Required; 0, 1, or a float |
| `/logs/verifier/<task_id>/stdout.txt` | Optional; diagnostic output |
| `/logs/verifier/<task_id>/reward.json` | Optional; multi-metric output |

If the harness still reuses Harbor's global `/logs/verifier/reward.txt`, results can be aggregated into it after the task verifiers finish.

---

## 8. Injected Message Format

When the step reaches `arrival.step`, the harness injects the following into the agent:

```text
[NEW TASK: prove-plus-comm]
Fix the incomplete proof in /workspace/plus_comm.v and compile it with coqc.

Success target:
- /workspace/plus_comm.v
- /workspace/plus_comm.vo

When finished, call todo_update("prove-plus-comm", "complete")
Deadline: step 18
```

When a task is injected, the agent is no longer told "which folder belongs to this task", because the benchmark no longer models task-specific workspaces.

---

## 9. Solvability oracle (`solve.sh`)

Every task in the source harbor repository has a `<task>/solution/solve.sh`: an oracle
implementation that, when run, makes the single task's `tests/test.sh` return reward=1.
After a stream merges N tasks into one stream in arrival order, it must still guarantee
that "solving each task in this order leaves every task's verifier passing". This is the
role of the stream-level `solve.sh`. Its purpose is to prove that a stream **is solvable**
before any agent runs on it (a feasibility check).

### 9.1 Responsibilities

- **Reproducibility check**: Given a stream image, after `solve.sh` finishes, every
  `tasks/<id>/tests/test.sh` returns reward=1. A failure indicates a problem in stream
  merging, sampling or path rewriting, or a hidden dependency conflict between tasks.
- **Order assertion**: solve.sh runs tasks strictly in ascending order of the
  `arrive_wall_time` in `[[arrivals]]`. If task B can only be solved after task A,
  stream.toml must reflect that order.
- **Oracle baseline**: The control group of a perfect agent, giving the reward upper bound.

### 9.2 Directory Structure (v1.1 extension)

```text
<stream_name>/
├── stream.toml
├── solve.sh                        # ← stream-level orchestrator
├── environment/
│   └── Dockerfile                  # also COPYs solve.sh and tasks/ into the image
└── tasks/
    └── <task_id>/
        ├── instruction.md
        ├── solution/
        │   └── solve.sh            # ← per-task oracle (NEW, copied from the source harbor task + path rewriting)
        └── tests/
            ├── test.sh
            └── test_outputs.py
```

### 9.3 Per-task `solution/solve.sh`

`assemble.py` reads the source harbor task's `<source>/solution/solve.sh` and
**applies exactly the same `slot_rules` as for `instruction.md` / `tests/test.sh`**:
home_root → namespace rewriting, `/app/...` → `/workspace/<id>/...`, URL mirror
substitution (`github.com` → `kkgithub.com`, `huggingface.co` → `hf-mirror.com`, etc.),
and pass-through of relative paths. It then writes the result to
`tasks/<task_id>/solution/solve.sh` with mode 0o755.

If the source task has no solve.sh (WildClawBench ships none at all, and some individual
SkillsBench tasks may also lack one), this step is skipped, and the corresponding task is
marked `no_solver` in the top-level `solve.sh`.

### 9.4 Top-level `solve.sh`

Generated by `assemble.py` in Phase 5, together with `stream.toml`:

```bash
#!/usr/bin/env bash
# AOS-Bench stream oracle — solve every task in arrival order, then verify.
# Generated by aos_bench.task_construction.assemble; do not edit by hand.
set -uo pipefail

STREAM_TASKS_DIR="${STREAM_TASKS_DIR:-/tasks}"
ORACLE_REWARD_DIR="${ORACLE_REWARD_DIR:-/tmp/oracle-reward}"
mkdir -p "$ORACLE_REWARD_DIR"

# Sorted by arrive_wall_time (ascending). Generator pulls from stream.toml.
ARRIVALS=(
  "build-pmars"
  "compile-compcert"
  "nginx-request-logging"
)

failed=()
solved=()
no_solver=()

for task_id in "${ARRIVALS[@]}"; do
  echo "=== oracle solve: $task_id ==="
  solve="${STREAM_TASKS_DIR}/${task_id}/solution/solve.sh"
  if [ ! -f "$solve" ]; then
    no_solver+=("$task_id")
    echo "  [skip] no per-task solve.sh"
    continue
  fi

  # cwd matches the task's namespace home so relative paths resolve correctly.
  cd "/workspace/${task_id}" 2>/dev/null || cd /workspace
  if ! bash "$solve"; then
    failed+=("${task_id} (solve.sh exit≠0)")
    continue
  fi

  # Verify via the task's own test.sh; reuse the reward shim env contract.
  test_sh="${STREAM_TASKS_DIR}/${task_id}/tests/test.sh"
  if [ -f "$test_sh" ]; then
    task_reward_dir="${ORACLE_REWARD_DIR}/${task_id}"
    mkdir -p "$task_reward_dir"
    AOS_REWARD_DIR="$task_reward_dir" bash "$test_sh" || true
    r=$(cat "$task_reward_dir/reward.txt" 2>/dev/null || echo 0)
    if [ "$r" = "1" ] || [ "$r" = "1.0" ]; then
      solved+=("$task_id")
    else
      failed+=("${task_id} (test.sh reward=$r)")
    fi
  else
    solved+=("${task_id} (no verifier)")
  fi
done

echo
echo "=== oracle summary ==="
echo "solved (${#solved[@]}):    ${solved[*]:-—}"
echo "no_solver (${#no_solver[@]}): ${no_solver[*]:-—}"
echo "failed (${#failed[@]}):    ${failed[*]:-—}"
[ ${#failed[@]} -eq 0 ]
```

### 9.5 Running It

After the stream image is built, run:

```
docker run --rm aos-bench/<stream-name> bash /oracle/solve.sh
```

`/oracle/solve.sh` is provided by
`COPY solve.sh /oracle/solve.sh && COPY tasks /tasks` in `environment/Dockerfile`.
If needed, the oracle can also be run through a host bind mount:

```
docker run --rm \
  -v "$(pwd)/data/stream/<stream>:/repo:ro" \
  aos-bench/<stream-name> bash /repo/solve.sh
```

### 9.6 Limitations and Caveats

1. **WildClawBench has no oracle at all**: WildClaw's `grade()` only judges results; it
   does not construct them, so it cannot be used in reverse as a solver. The wildclaw
   pool can therefore only be `no_solver` at the solve.sh level and cannot vouch for
   solvability. Running the oracle on a wildclaw stream yields `no_solver` for every
   task, which is expected.
2. **Solve order is tied to arrival order**: By default tasks are solved in ascending
   `arrive_wall_time`. When tasks have ordering dependencies, the stream author must
   make the arrival order match the dependency topology.
3. **Environment side effects**: Some source solve.sh scripts run `apt-get install ...`.
   The merger already promotes these apt packages into the global apt block, so
   re-running them in the oracle should be a no-op. If a source solve.sh installs a
   package the merger did not collect (meaning the promotion missed it), the oracle
   fails here, which is a useful signal.
4. **Reward threshold**: A task passes when its reward is `1` or `1.0`.
5. **Path collisions vs. solvability**: Under the ideal v1.1 "single `/workspace/`"
   model, tasks share one file system view. The current merger implementation gives
   each task its own `/workspace/<task_id>/` namespace to avoid hard collisions. Under
   both semantics, the oracle solve.sh sets its cwd by namespace
   (`cd /workspace/<task_id>`). Switching to a strict v1.1 "no namespace" mode only
   requires changing that line to `cd /workspace`; the rest of the framework is
   unchanged.
6. **Failure handling**: The oracle expects every task to pass. Any failure produces a
   nonzero exit status, so CI can use it directly as a gate. Internally, `|| true`
   isolates each task so the summary can list all failed tasks at once.

### 9.7 Implementation Hook

Phases of `aos_bench/task_construction/assemble/assemble.py`:

| Phase | What it does |
|---|---|
| Phase 4 | Writes instruction.md + tests/test.sh + tests/* for each task |
| Phase 4d | If the source task has `solution/solve.sh`, reads it → `rewrite_text(content, slot_rules)` → writes it to `tasks/<id>/solution/solve.sh` + `chmod 0o755`. If the source has no solve.sh, skips it and records the task in the spec's `_no_solver` list |
| Phase 5 | Renders `stream.toml` |
| Phase 5b | Renders the top-level `solve.sh`, filling the `ARRIVALS` array with task_ids from `slot_records` in ascending `arrive_wall_time`; also adds the `solve.sh` `COPY` line to the merged Dockerfile (`COPY solve.sh /oracle/solve.sh`) |

**Expected oracle results** for `docker run ... bash /oracle/solve.sh`:

- tb2 / skillsbench: every task has a source solve.sh, so the oracle should report all tasks as `solved`
- wildclaw: every task is `no_solver` (not reported as a failure, but solvability is not proven either)
- Any `failed` entry means the merger, path rewriting or apt promotion missed something and needs debugging

---

## 10. Intended Use

This format suits the orchestrator behavior AOS-Bench aims to measure:

- multiple tasks share one OS environment
- the agent works directly on a single file system
- tasks are defined by instructions and target paths
- sub-agents, shells and verifiers all operate on real paths rather than on an artificial benchmark path protocol

It is particularly well suited to merging tasks such as:

- a git repo repair task: `/workspace/personal-site/...`
- a Coq proof task: `/workspace/plus_comm.v`
- an OpenSSL configuration task: `/workspace/ssl/...`

These tasks can coexist in the same `/workspace/` without any explicit folder-to-task mapping.
