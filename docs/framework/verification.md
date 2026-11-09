# AOS-Bench Verification Design

> This document covers two layers:
>
> 1. **Scheduling semantics** (§1-§3, §10-§13): online vs. final verify, the
>    Coordinator state machine, and the Watcher / Runner split. This is the
>    "when to verify, how many times, and how to record it" layer that AOS-Bench
>    adds on top of the harbor verifier.
> 2. **Physical isolation** (§4-§9): implementation details of image-level,
>    upload-time and cross-task isolation within a stream. It follows the
>    standard terminal-bench / harbor approach and defines the concrete changes
>    for wildclaw, tb2 and skillsbench.

---

## 1. Goals

AOS-Bench has to answer two different questions:

1. When the agent declares `complete` at some point in time, is the task actually done?
2. When the session ends, is the final deliverable correct?

Online verify alone would treat an intermediate snapshot as the ground truth. Final verify alone would lose the quality of the agent's online judgement. v1.1 therefore uses two-phase verification:

- **online verify**: listens for `todo_update(task_id, "complete")` and fires the first time the agent declares the task complete
- **final verify**: after the session ends, runs once more over all injected tasks

Of these:

- `final_result` is the benchmark ground truth
- `online_result` is used only for behavioral analysis and never overrides the final result

---

## 2. Overall architecture

```text
┌────────────────────────────────────────────────────────────────────┐
│ Session                                                           │
│                                                                    │
│  Main Agent ──▶ TodoStore ──▶ events.jsonl                         │
│                     │                                              │
│                     ▼                                              │
│               todos.json                                           │
│                                                                    │
│  VerificationCoordinator                                           │
│    ├── EventsWatcher  ── online trigger on complete                │
│    ├── VerificationRunner ── isolated test execution core          │
│    └── Final batch runner ── verify all arrived tasks at session end│
│                                                                    │
│  verify_results/                                                   │
│    ├── <task_id>/online_001.json                                   │
│    └── <task_id>/final.json                                        │
└────────────────────────────────────────────────────────────────────┘
```

Design principles:

- **Shared execution core**: online and final both call the same `VerificationRunner`
- **Separate results**: online and final results are stored separately so neither overwrites the other
- **Path isolation**: each verify job uses its own temporary directory and does not share `/tests`
- **Serial first**: in v1, online and final both run serially by default, favoring stability
- **Temporal isolation**: while the agent runs, `tests/`, `solution/` and `.gt/` do not
  physically exist in the image. The Coordinator uploads them only at verify time. See §4.

---

## 3. Semantics

### 3.1 Online verify

Online verify answers:

> At the moment the agent first says "I'm done", is the deliverable correct?

v1 rules:

- Listen only for `event == "complete"`
- Each task triggers online verify **once** by default
- If the agent never declares `complete`, the task may have no online result

This prevents the agent from probing the verifier through repeated `complete` events.

### 3.2 Final verify

Final verify answers:

> When the session ends, does the final state of `/workspace` pass verification?

v1 rules:

- Run final verify on every **injected** task
- Do not depend on whether the agent declared `complete`
- Run final verify in one batch after the session ends

> **Important**: "run on every task" means that even if the agent never claimed
> or touched a task, the verifier still runs `test.sh` against the current
> `/workspace`. The image's default state, or artifacts left by earlier tasks,
> can therefore earn the verifier's credit when they should not. The aggregate
> score uses a strict pass rate to exclude such "luck passes". See §3.4.

So if task 1 is done correctly midway and later broken by task 2, it is still recorded as `final fail`.

### 3.3 Ground-truth definition

- Leaderboards and headline metrics use `final_result`
- `online_result` is used only to analyze the quality of the agent's completion claims
- `online pass` does not mean final success
- Only `final pass` means the final deliverable is correct

### 3.4 Reward aggregation: lenient vs. strict pass rate

> See `harbor/src/harbor/verifier/stream_verifier.py:_write_reward_breakdown`
> and `agent_contract.md §5.1`.

After the stream verifier finishes, it writes two files to `<trial>/verifier/`:

| File | Schema | Purpose |
|---|---|---|
| `reward.json` | `{task_id: float}` (unchanged) | Raw per-task score; consumed directly by the existing harbor pipeline |
| `reward_breakdown.json` | `{tasks: {task_id: {...}}, summary: {...}}` (new) | Adds the strict pass rate, distinguishing tasks the agent **claimed and declared complete** from tasks the verifier **happened to pass by accident** |

**Why strict is needed**: because final verify "runs on every injected task" (§3.2),
the verifier also runs `test.sh` for tasks the agent never `claim`ed. If the
image's default state or an artifact left by a previous task happens to satisfy
the grader's conditions, the task scores 1.0. **The agent did no work but still
got the score.** This "luck inflation" is invisible in reward.json.

`reward_breakdown.json.summary`:

| Field | Computation | Meaning |
|---|---|---|
| `n_total` | `len(arrivals)` | Total number of tasks in the stream |
| `n_passed` | `sum(score>0 for ...)` | Number of tasks with verifier score > 0 |
| `n_claimed` | `count(status in {completed, failed})` | Number of tasks the agent moved to a terminal state via `todo_update` |
| `n_claimed_completed` | `count(status == completed)` | Tasks the agent actively declared "done" |
| `n_claimed_completed_passed` | `count(status==completed AND score>0)` | **Tasks that actually count: "did the work and got it right"** |
| `n_unclaimed_passed` | `count(NOT claimed AND score>0)` | Quantifies luck inflation (should be close to 0) |
| `pass_rate_lenient` | `n_passed / n_total` | Equivalent to the old `mean(reward.json)` |
| **`pass_rate_strict`** | **`n_claimed_completed_passed / n_total`** | **New headline metric**, excludes luck |
| `dispatcher_state_present` | bool | Whether `dispatcher_state.json` was written successfully |

Scoring semantics:
- agent `todo_update(tid, "completed")` + verifier passes → counts
- agent `todo_update(tid, "completed")` + verifier fails → does not count (the agent's self-report was wrong)
- agent `todo_update(tid, "failed")` + verifier passes → does not count (the agent gave up and should not be rewarded)
- no `todo_update` from the agent → does not count (even if score=1.0, it is pure luck)

For an ideal agent, `strict ≈ lenient`. The gap measures how lucky or sloppy
the agent is. We recommend using only strict for papers and product decisions.

`dispatcher_state.json` is produced by `teardown_aos_pool_v03`: at the end of
the trial, before unregistering, it GETs the dispatcher's `/admin/dump?stream_id=X`
and writes the result to the trial directory root. Schema:

```json
{
  "ok": true,
  "stream_id": "mixed-30-2__abc123",
  "stream_done": true,
  "tasks": [
    {"task_id": "vulnerable-secret",
     "status": "completed",
     "claimed_at": 4.4, "completed_at": 84.7,
     "ready_at": null, "at_sec": 0.0,
     "note": "Extracted FLAG{...}"},
    ...
  ]
}
```

If fetching from the dispatcher fails (network blip, or the dispatcher is down),
`reward_breakdown.json` is still generated, with `agent_reported_status` set to
null for every task and `summary.dispatcher_state_present: false`.

---

## 4. Temporal isolation

### 4.1 Motivation

Within a stream, the agent and the verifier share one container and
therefore share a filesystem. This creates a class of integrity holes:

- During its turns the agent can `cat` `/tests/test.sh`, `/oracle/solve.sh` and other
  scripts that should only be exposed during the verification or oracle phase, and use
  them to bypass the task logic and directly produce the artifacts the grader expects.
- The judge prompts of LLM-judge / VLM-judge tasks embed ground-truth strings
  (WildClawBench series 04 / 06). If the grader source is visible, the ground truth is effectively public.

**Harbor / terminal-bench prevent leaks along the time dimension rather than the
space dimension**: the image does not bake in tests, solution or gt at build time.
After the agent run, the verifier calls `upload_dir` to push `tests/` into the
container and only then starts grading (`harbor/src/harbor/verifier/verifier.py:85-88`).
The oracle is not in the image either; OracleAgent injects it (`agents/oracle.py:79-82`).
While the agent runs, those files **do not physically exist**, so no amount of
`ls -la / find / chmod` can uncover them.

AOS-Bench's stream verifier (`harbor/src/harbor/verifier/stream_verifier.py`)
already implements this upload protocol. The problem is not the verifier. The
problem is that our `assemble.py` **bakes too much into the image ahead of time**.

### 4.2 Stream-level single-container multi-task verify

The core loop of `StreamVerifier.verify()` (`stream_verifier.py:83-110`):

```
upload all tests/<id>/   →  /tests/<id>/        # once, at session start
for each arrival:
    swap /tests/* symlinks  →  /tests/<id>/*    # legacy shim (line 152-170)
    AOS_TASK_ID=<id>
    AOS_REWARD_DIR=/logs/verifier/<id>
    AOS_TESTS_DIR=/tests/<id>
    bash /tests/<id>/test.sh                    # writes reward.txt
aggregate per-task reward.txt → reward.json
```

Implication: **the agent has already exited during the verifier phase**, so the
agent cannot see `tests/` at that point. The leak lies entirely on the image-baking side.

### 4.3 Stream-level oracle phase (aligned with harbor single-task)

This directly reuses the contract of harbor's single-task `OracleAgent`
(`harbor/src/harbor/agents/oracle.py:79-82`): upload `solution/` → `/solution/`,
then exec `/solution/solve.sh`. **`/tests/` does not exist during the oracle phase**,
exactly as in single-task mode. The verifier uploads tests/ and grades only after the oracle finishes.

```
oracle agent phase:
    rm -rf /solution/* && upload <stream>/solution/  →  /solution/
    bash /solution/solve.sh                              # single run, orchestrates all tasks

verifier phase (invoked by the trial after OracleAgent exits):
    upload <stream>/tasks/<id>/tests/  →  /tests/<id>/   # tests/ appears only now
    for each arrival (parallel): bash /tests/<id>/test.sh → reward.txt
```

For solvers that need ground truth (wildclaw rule-based), **gt is uploaded
together with the solution**: `<stream>/solution/tasks/<id>/.gt/` and
`<stream>/tasks/<id>/tests/.gt/` have identical contents and serve the solver and
the grader respectively. The converter ships both copies directly, and assemble.py
phase 4d copies the whole `solution/` directory (including `.gt/`), not just `solve.sh`.

The solver locates gt through two mechanisms:

1. The orchestrator passes `AOS_GT_DIR=/solution/tasks/<id>/.gt` as an env variable
2. Fallback: the solver looks in `$(dirname "$0")/.gt`, the directory next to the script

This way the solver does not depend on `/tests/` at all, and the single-task and stream flows have the same semantics.

### 4.4 What to do with the stream-level top-level solve.sh

Currently `<stream>/solve.sh` is the stream-wide orchestrator (it iterates over every task's
`/oracle/tasks/<id>/solution/solve.sh`). Under the new scheme it does not need to be
baked into the image; a new host-side `StreamOracleAgent` can orchestrate inside the
harbor process (§10.4). `<stream>/solve.sh` stays on the host as a reference script
for running the oracle without harbor, but the main stream path does not depend on it.

---

## 5. Leak surface of the current stream image

### 5.1 Leak common to all streams: `_oracle/` baked into the image

`aos_bench/task_construction/assemble/dockerfile_merge.py:511-512` always appends at the end:

```dockerfile
# Oracle solver bundle (used by `bash /oracle/solve.sh`).
COPY _oracle/solve.sh /oracle/solve.sh
COPY _oracle/tasks/   /oracle/tasks/
RUN chmod +x /oracle/solve.sh; \
    find /oracle/tasks -name 'solve.sh' -exec chmod +x {} + 2>/dev/null || true
```

`_oracle/tasks/` is a full mirror of `<stream>/tasks/` made by
`shutil.copytree(tasks_dir, oracle_tasks_dir)` at `assemble.py:277`, so `/oracle/tasks/<id>/`
in the image **contains every task's instruction.md / solution/solve.sh / tests/test.sh / tests/test_outputs.py**.

Inside the container the agent can run `ls /oracle/tasks/04_Search_Retrieval_task_3_constraint_search/tests/`
and see `test_outputs.py`, whose judge prompt hard-codes "the reference answer includes at least two of
Xiaomi 14 Ultra / Vivo X100 Ultra / Magic 6 Ultimate / OPPO Find X7 Ultra". The agent can then
write a results.md that hits the rubric and is guaranteed full marks.

### 5.2 WildClawBench-specific leak: `/workspace/.gt/`

Current template in `aos_bench/task_construction/scripts/convert_wildclawbench.py`:

```dockerfile
COPY _workspace_exec/ /workspace/
COPY _workspace_gt/   /workspace/.gt/
RUN ln -sf /workspace /workspace/tmp && ln -sf /workspace/.gt /workspace/gt
```

`.gt` is only cosmetically hidden (`ls` hides dot directories by default); `ls -a / find / cat` all reveal it.
For rule-based graders (series 01) this is equivalent to publishing the answers.

### 5.3 Five tb2 source tasks with built-in build-time leaks

The source task Dockerfile directly runs `COPY tests/X`:

| Task | Leak |
|---|---|
| `overfull-hbox` | `tests/main.tex`, `tests/input.tex`, `tests/synonyms.txt` |
| `schemelike-metacircular-eval` | `tests/interp.py`, `tests/test` |
| `break-filter-js-from-html` | `tests/test_outputs.py` |
| `circuit-fibsqrt` | `tests/sim.c` |
| `regex-chess` | `check.py` (at the task root, nominally for the verifier) |

This is a build-time design choice by the upstream tb2 task authors (in some cases
the test assets must be built into the image for the oracle to run), and it carries
over into the stream image with the source task. **5 of 91 tasks leak.**

### 5.4 SkillsBench

A sample of 3 tasks showed no build-time `COPY` of tests in the source tasks. A full scan of all 77 tasks is covered in §9.3.

---

## 6. Cross-task conflict surface within a stream

### 6.1 The shared /workspace design choice

The `stream_verifier.py:6` docstring states explicitly:

> every sub-task observes the shared workspace the agent left behind

Tasks moved from `/workspace/<id>/...` to a shared `/workspace/`. This is
intentional (possible path conflicts between tasks are part of the ρ experiment).
It does, however, create three cross-task issues from the verifier's perspective:

| Conflict type | Trigger | Current handling |
|---|---|---|
| **Build-time COPY conflict** | Dockerfiles of several source tasks COPY to the same `/workspace/X` | `collision.py` renames automatically during assembly: the first task keeps the path, later ones become `/workspace/X__<task_id>` |
| **Runtime write conflict** | Task A's agent writes `/workspace/output.json` and task B's grader also reads `/workspace/output.json` | **No isolation at all**; the verifier does not clean the workspace between tasks |
| **`/workspace/.gt/` subdirectory conflict** | Several wildclaw tasks COPY to `/workspace/.gt/X` | Same as build time; the collision detector renames automatically |

In one 30-task mixed stream, the build stage had about 30 path collisions, all renamed
automatically. No runtime conflicts have been observed yet, because each oracle solver
writes to its own deliverable directory (`/workspace/tmp/results/<task>/`).

### 6.2 Reward namespace

`/logs/verifier/<task_id>/reward.txt` is per task, so tasks do not overwrite each other
(`stream_verifier.py:136, 179`). The legacy single file `/logs/verifier/reward.txt`
has an automatic shim: after the test it is `mv`ed into `<task_id>/` (`stream_verifier.py:191-193`).

### 6.3 Failure propagation

The `|| true` in `stream_verifier.py:186-194` ensures that a crash or timeout in one
task's test.sh does not block later tasks. A missing `reward.txt` counts as 0 by default. **Failures do not propagate.**

---

## 7. Target state: terminal-bench-style temporal isolation

### 7.1 Image stage (used for the agent run)

Keep only:

- Base dependencies (apt / pip / uv install)
- The initial workspace (per-task `_workspace_exec/` → `/workspace/<files>`)
- Markdown that the agent treats as "optional reference", such as skills (if any)

Do **not** bake in:

- `tests/` (under any subpath)
- `solution/`
- `_workspace_gt/` / `.gt/`
- the entire `_oracle/` directory
- `COPY tests/X` lines in source task Dockerfiles must be moved into a source-task fix or
  an assembler patch (see §9.2)

### 7.2 Verify stage (test time)

Reuse harbor's existing `StreamVerifier._upload_tasks_tree`:

```
for each arrival:
  upload <stream>/tasks/<id>/tests/   →  /tests/<id>/
```

WildClawBench gt travels with `tests/`: place it in
`<stream>/tasks/<id>/tests/.gt/` and have the grader read `${AOS_TESTS_DIR}/.gt/...`.
Harbor already supports this upload, so no protocol change is needed, only a reorganization of file locations.

### 7.3 Oracle stage (oracle time)

See §4.3. It follows harbor's single-task OracleAgent convention of "upload solution → exec
/solution/solve.sh", with an added per-arrival loop.

---

## 8. WildClawBench testing strategy

### 8.1 Directory layout (converter output)

```
data/harbor/wildclawbench-converted/<task_id>/
├── task.toml
├── instruction.md
├── environment/
│   ├── Dockerfile              # no longer COPYs _workspace_gt/, does not create /workspace/.gt
│   └── _workspace_exec/        # still COPYed into /workspace/, visible to the agent (input)
├── tests/
│   ├── test.sh                 # cd /workspace, run test_outputs.py
│   ├── test_outputs.py         # grade(), reads ${AOS_TESTS_DIR}/.gt/...
│   └── .gt/                    # new location: ground truth travels with tests
│       └── (gt_manifest.json / *.bib / ground_truth.json / ...)
└── solution/
    └── solve.sh                # oracle solution, reads ${AOS_TESTS_DIR}/.gt/... (same gt as at verify time)
```

### 8.2 Dockerfile template change

```dockerfile
WORKDIR /workspace

# Only COPY exec (the agent's initial input), not gt.
COPY _workspace_exec/ /workspace/

# /workspace/tmp remains a compatibility symlink for /tmp_workspace (the grader
# hard-codes this path internally; the path-rewrite stage changes it to /workspace/tmp):
RUN ln -sf /workspace /workspace/tmp
# Note: the /workspace/gt → /workspace/.gt symlink is removed, because
# /workspace/.gt no longer exists in the image. The grader reads ${AOS_TESTS_DIR}/.gt/ instead.
```

### 8.3 Grader path rewriting

Add one rule to `convert_wildclawbench._rewrite_paths`:

```python
_PATH_REWRITES = (
    ("/tmp_workspace/gt", "${AOS_TESTS_DIR}/.gt"),   # must come before the next rule
    ("/tmp_workspace", "/workspace/tmp"),
    ("/root/skills", "/workspace/skills"),
)
```

Note that in Python `${AOS_TESTS_DIR}` must be read via `os.environ.get("AOS_TESTS_DIR", "/tests")`.
The rewrite therefore cannot be a plain string substitution. It has to inject this at the top of the grader Python file:

```python
import os
_GT_DIR = os.environ.get("AOS_TESTS_DIR", "/tests") + "/.gt"
```

and then replace every `/tmp_workspace/gt` (or `/workspace/.gt`) with string interpolation such as
`f"{_GT_DIR}/foo.json"`. A cleaner implementation rewrites these into `os.path.join` form.

### 8.4 Solvers change the same way

Every `/workspace/.gt/...` in `wildclaw_solvers/*.sh` becomes
`"${AOS_TESTS_DIR}/.gt/..."` (`StreamVerifier` already sets `AOS_TESTS_DIR` to
`/tests/<task_id>`, `stream_verifier.py:183`; the oracle phase follows the same env convention).

The ordering follows §4.3: for each arrival the oracle agent first uploads tests, then
uploads the solution, and only after the solve.sh of every arrival has run does it hand
control to the stream verifier for grading:

```
oracle agent phase:
  for arrival in arrivals:
    upload tests/<id>/       →  /tests/<id>/       # includes .gt/
    rm -rf /solution/*
    upload solution/<id>/    →  /solution/         # same path as harbor single-task
    AOS_TESTS_DIR=/tests/<id> bash /solution/solve.sh

stream verifier phase (already exists, unchanged):
  upload tests/<id>/ → /tests/<id>/                 # idempotent overwrite
  for arrival in arrivals:
    swap /tests/* shim → /tests/<id>/*
    AOS_TESTS_DIR=/tests/<id> AOS_REWARD_DIR=/logs/verifier/<id> bash /tests/<id>/test.sh
```

### 8.5 Affected tasks

Of the current 52 tasks, 22 ship content in `_workspace_gt/` (the other 30 have an empty gt).
All 22 must be migrated to `tests/.gt/`. The converter changes in one place and every task
is re-converted (provided the LFS fixtures are available locally); otherwise
`migrate_wildclaw_in_place.py` migrates them in place.

---

## 9. tb2 / skillsbench changes

### 9.1 Default case (86 / 91 tb2 tasks and most skb tasks)

The source task layout is already separated (`tests/` and `solution/` are siblings of
environment/), and the source Dockerfile does not COPY them. **As soon as the stream
stops baking in `_oracle/`, these tasks satisfy temporal isolation** with zero changes to the source tasks.

### 9.2 Options for the 5 out-of-bounds tb2 tasks

| Task | Current COPY | Option A: move the asset into environment/ | Option B: assembler drops these COPY lines |
|---|---|---|---|
| `overfull-hbox` | `tests/main.tex` etc. | mv into `environment/` and update the verify path in the source task.toml | merger phase 1 detects `COPY tests/...` and replaces it with a NO-OP; the verifier phase uses `/tests/<id>/main.tex` |
| `schemelike-metacircular-eval` | `tests/interp.py` etc. | Same as above | Same as above |
| `break-filter-js-from-html` | `tests/test_outputs.py` | Hard to move (test_outputs.py must live under tests/) | Must use B |
| `circuit-fibsqrt` | `tests/sim.c` | Same as A | Or B |
| `regex-chess` | `check.py` | Not under tests/ but at the task root, a special case | Handled separately |

Recommended: B. Add an assembler safeguard that "drops any `COPY` instructions that reference paths
above `environment/`". The source tasks stay unchanged, but the stream image no longer leaks. The
cost is that verification of these 5 tasks now depends on `tests/<asset>` having been uploaded by the
verifier, which is fine: `stream_verifier._upload_tasks_tree` uploads the whole `tests/` by default, so the assets come along.

### 9.3 Full SkillsBench scan

Run the same `COPY tests/|COPY solution/|COPY \.\./` pattern scan over all 77 skb tasks.
If many tasks match, write an audit report that marks each one; if few match, apply the assembler patch as in §9.2.

---

## 10. Core components

### 10.1 EventsWatcher

Responsibility: replay and continuously watch `events.jsonl`, and hand `complete` events to the coordinator.

Input:

- `/sandbox/state/events.jsonl`

Output:

- complete events of the form `{"task_id": ..., "step": ..., "ts": ...}`

Implementation notes:

- Replay existing content on startup so no events are lost
- Use a polling tail at runtime to avoid extra platform dependencies
- Ignore `added` / `pending` / `in_progress` / `failed`
- Ignore malformed lines and log them

### 10.2 VerificationRunner

Responsibility: run a single task's `tests/test.sh` and produce a `VerifyResult`.

Input:

- `task_id`
- `kind = "online" | "final"`
- `tasks/<task_id>/tests/test.sh`
- `workspace_dir`

Output:

- `VerifyResult`

Execution rules:

1. Create a dedicated directory for the current job, `/tmp/aos_verify/<job_id>/`
2. Set environment variables:
   - `AOS_VERIFY_OUT`
   - `AOS_TASK_ID`
   - `AOS_VERIFY_KIND`
   - `AOS_TESTS_DIR` (points to `/tests/<task_id>/`, where `upload_dir` placed the tests)
3. Run `bash tasks/<task_id>/tests/test.sh` under `workspace_dir`
4. Read the reward with the following priority:
   - `$AOS_VERIFY_OUT/reward.txt`
   - `/logs/verifier/<task_id>/reward.txt`
   - `/logs/verifier/reward.txt`
5. Write the result to `verify_results/<task_id>/...`

### 10.3 VerificationCoordinator

Responsibility: manage online triggering, result persistence, and the session-end final batch.

State machine:

```text
arrived
-> online_verified (0 or 1 times in v1)
-> final_verified (exactly once)
```

v1 control policy:

- online verify runs serially
- final verify runs serially
- after the session ends, stop online ingestion, then run the final batch

### 10.4 StreamOracleAgent (replacing `_oracle/` baking)

Change the `_run_stream` method of `harbor/src/harbor/agents/oracle.py`
(currently line 122-159 directly execs `/oracle/solve.sh` and depends on it being baked into the image).
The new implementation is the loop described in §4.3. In essence it reruns the single-task OracleAgent
section (line 79-107) once per arrival, and **all paths use the existing `/tests/` and `/solution/`
from `EnvironmentPaths`**, with no new conventions:

```python
async def _run_stream(self, environment, container_oracle_path, host_oracle_path):
    # self._stream_dir is <stream>/, which contains tasks/<id>/{tests,solution}/
    stream_toml = tomllib.loads((self._stream_dir / "stream.toml").read_text())
    arrivals = [a["task_id"] for a in stream_toml.get("arrivals", [])]

    for task_id in arrivals:
        task_dir = self._stream_dir / "tasks" / task_id
        tests_src = task_dir / "tests"
        sol_src = task_dir / "solution"
        if not (sol_src / "solve.sh").exists():
            continue
        await environment.upload_dir(
            source_dir=tests_src,
            target_dir=f"{EnvironmentPaths.tests_dir}/{task_id}",
        )
        await environment.exec(
            f"rm -rf {EnvironmentPaths.solution_dir}/*", user="root",
        )
        await environment.upload_dir(
            source_dir=sol_src,
            target_dir=str(EnvironmentPaths.solution_dir),
        )
        solve_path = f"{EnvironmentPaths.solution_dir}/solve.sh"
        await environment.exec(f"chmod +x {solve_path}", user="root")
        await environment.exec(
            command=f"{solve_path} >> {container_oracle_log_path} 2>&1",
            env={
                "AOS_TASK_ID": task_id,
                "AOS_TESTS_DIR": f"{EnvironmentPaths.tests_dir}/{task_id}",
                **self._extra_env,
            },
            timeout_sec=...,
        )
```

The trial's `_run_verification` phase then calls `StreamVerifier.verify()` as usual, because:

- each task's tests were already uploaded to `/tests/<id>/` during the oracle phase (idempotent)
- each solve.sh has already written its deliverables to `/workspace/...`
- StreamVerifier's `_upload_tasks_tree` plus per-task `test.sh` scoring logic is unchanged

Implementation notes:

1. Remove the comment and implementation in `oracle.py:131-135` that "depends on the image baking `/oracle/`"
2. Change `_run_stream` to the loop above
3. Leave the single-task OracleAgent path (line 53-120) untouched

---

## 11. Data model

```python
@dataclass
class VerifyResult:
    kind: Literal["online", "final"]
    task_id: str
    attempt: int
    trigger: Literal["complete", "session_end"]
    trigger_step: int | None
    trigger_ts: float | None
    started_ts: float
    finished_ts: float
    duration_sec: float
    reward: float | None
    passed: bool
    exit_code: int | None
    error: str | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    reward_path: str | None = None
```

```python
@dataclass
class TaskVerificationRecord:
    task_id: str
    online_results: list[VerifyResult]
    final_result: VerifyResult | None
```

Storage paths:

- `/sandbox/state/verify_results/<task_id>/online_001.json`
- `/sandbox/state/verify_results/<task_id>/final.json`
- `/sandbox/state/verify_results/index.json`

---

## 12. Session lifecycle

Suggested flow:

```python
async def run_session_with_verification(...):
    coordinator = VerificationCoordinator(...)

    await coordinator.start()
    session_result = await session.run()

    await coordinator.stop_online_ingest()
    await coordinator.drain_online(timeout_sec=60)
    await coordinator.run_final_batch(
        arrived_task_ids=[tr.task_id for tr in session_result.task_results]
    )
    await coordinator.shutdown()

    return merge(session_result, coordinator.summary())
```

These phases must stay separate:

- `start()`: begin replaying and watching online complete events
- `stop_online_ingest()`: stop accepting new online verifies after the session ends
- `drain_online()`: wait for queued online jobs to finish
- `run_final_batch()`: run final verify over all injected tasks
- `shutdown()`: close the watcher and clean up background tasks

Do not design the whole verifier as a `pool.run()` that never exits; otherwise the wrap-up after the session ends becomes complicated.

---

## 13. Relationship to existing components

### 13.1 TodoStore

TodoStore is still responsible only for:

- writing `todos.json`
- appending to `events.jsonl`

The verifier does not write back to TodoStore.

### 13.2 AOSBenchSession

The session is responsible for:

- injecting tasks
- counting steps
- providing the source of truth for "which tasks were actually injected"

The full set of tasks for final verify is defined by `SessionResult.task_results`, not by the online event stream.

### 13.3 Existing Harbor-style verifier

To stay compatible with existing task scripts, the runner accepts the following reward output locations:

1. `$AOS_VERIFY_OUT/reward.txt`
2. `/logs/verifier/<task_id>/reward.txt`
3. `/logs/verifier/reward.txt`

The long-term recommendation is to migrate everything to `AOS_VERIFY_OUT`, but v1.1 keeps compatibility for now.

---

## 14. Migration plan

Each step can land independently, in order of increasing risk:

### 14.1 Step A: stop putting `_oracle/` into the image

In `dockerfile_merge.py:498-518`, remove the `COPY` lines for `_oracle/solve.sh` and `_oracle/tasks/`.
Keep `assemble.py:267-277` writing `<stream>/environment/_oracle/` as a host-side oracle reference,
but it no longer goes into the image.

**Impact**: the existing flow that runs the oracle baseline via `bash /oracle/solve.sh` breaks. This requires
Step D at the same time: harbor's `_run_stream` switches to the upload loop.

### 14.2 Step B: assembler safeguard that rejects COPYs from outside environment/

When `dockerfile_merge.py` merges per-task RUN/COPY lines, it filters out any COPY whose source is
`../tests/`, `../solution/`, `../check.py` and similar. This affects the 5 tb2 tasks in §9.2, but their
verify-time test.sh can already find these assets under `/tests/<id>/`.

### 14.3 Step C: reorganize WildClawBench gt (§8)

Changes to `convert_wildclawbench.py`:

1. Copy `ws_gt` to `<task_id>/tests/.gt/` instead of `environment/_workspace_gt/`
2. Remove the two `COPY _workspace_gt/` lines from the Dockerfile template
3. Update the grader path rewrite rules
4. Update the solver path rewrites

Migration: a one-off script moves the existing
`data/harbor/wildclawbench-converted/<id>/environment/_workspace_gt/`
to `<id>/tests/.gt/` and fixes the Dockerfile and grader accordingly.

### 14.4 Step D: harbor-side stream oracle path

Rewrite `oracle.py:_run_stream` as in §10.4. The `<stream>/solve.sh` path and the image-baked
`/oracle/...` path are no longer used; the stream oracle runs entirely as an in-process `OracleAgent`
loop of upload_dir + exec.

### 14.5 Step E: cross-task /workspace cleanup (optional)

If tasks ever need to be fully free of side effects (fairness), add a `StreamVerifier` option:
snapshot /workspace after verifying one task and restore it before the next. However, this
contradicts the purpose of the ρ experiment, which is to create conflicts through a shared sandbox, so **it is off by default**.

---

## 15. Verification checks

After each step lands, every one of the following should pass:

```bash
# 1. the agent image must not contain tests / solution / gt
docker run --rm <stream>:image bash -c '
  for p in /tests /solution /oracle /workspace/.gt; do
    [ -e "$p" ] && echo "LEAK: $p exists" || echo "OK: $p absent"
  done
'

# 2. stream oracle end to end; rewards should match the historical baseline
harbor run --task-type stream -p <stream_dir> \
    --env docker --agent oracle

# 3. run the stream verifier right after a normal agent run and confirm tests/ is injected by upload_dir
# (look for the "upload_dir → /tests/<id>/" event in the harbor log)
```

---

## 16. Edge cases

### 16.1 Agent never reports complete

- the online result is missing
- final verify still runs

### 16.2 Agent reports complete but later breaks the result

- online may pass
- final may fail

Both are kept and neither overwrites the other.

### 16.3 Verify script exits non-zero

- if the reward file exists, judge by the reward
- if the reward file is missing, record `error = "no_reward_file"`

### 16.4 Verify timeout

- record `error = "timeout"`
- `passed = False`

### 16.5 Partial reward

v1.1 keeps `reward` as is

- `passed` defaults to `reward >= 1.0`
- an arbitrary positive score is not automatically treated as a pass

---

## 17. Suggested metrics

We recommend two groups:

### 17.1 Final-truth metrics

- `final_pass_rate`
- `final_pass_rate_given_complete`
- `false_complete_rate`
- `silent_success_rate`

### 17.2 Online-behavior metrics

- `online_pass_rate_at_first_complete`
- `premature_complete_rate`
- `regression_after_complete_rate`

Definitions:

- `premature_complete_rate`: online fail, but final pass
- `regression_after_complete_rate`: online pass, but final fail

Together these show both the quality of the agent's online claims and the quality of its final deliverables.

---

## 18. Risks and known trade-offs

| Risk | Assessment |
|---|---|
| With `_oracle/` no longer baked into the image, oracle sweeps get slower | About 1 KB of upload per task, 30 KB total for a 30-task stream; negligible |
| Moving WildClawBench gt from `/workspace/.gt/` to `/tests/<id>/.gt/` breaks grader path rewriting | Covered by unit tests; after `_rewrite_paths` runs in the converter, grep all task files to confirm no `/workspace/.gt` remains |
| The assembler blocks the build-time COPY of the 5 tb2 tasks, and the original authors' oracle solve.sh may depend on the paths that COPY created | Compare with an oracle sweep; if a task fails because of this, handle it case by case |
| Changes to the upstream harbor oracle agent must be synced into our fork | Via a PR or a monkeypatch; the `oracle.py` change in harbor is small |
| Cross-task state contamination in /workspace still exists | A design trade-off, left for later analysis in the ρ experiment |

---

## 19. Module structure

```text
aos_bench/verifier/
├── __init__.py
├── result.py       # VerifyResult / TaskVerificationRecord
├── watcher.py      # events.jsonl replay + tail
├── runner.py       # single-task verifier execution core
└── pool.py         # online coordination + final batch orchestration
```

The stream-path code lives in:

- `aos_bench/task_construction/assemble/dockerfile_merge.py`: changed by §14.1/14.2
- `aos_bench/task_construction/scripts/convert_wildclawbench.py`: changed by §14.3
- `harbor/src/harbor/agents/oracle.py`: changed by §14.4
- `harbor/src/harbor/verifier/stream_verifier.py`: unchanged (the design is already correct)

---

## 20. v1.1 scope

v1.1 explicitly includes:

- online verify
- final verify
- per-task result persistence
- compatibility with the reward paths of existing Harbor `test.sh` scripts
- the temporal isolation changes (§14 Step A–D)
- the WildClawBench gt reorganization (§14.3)

v1.1 explicitly excludes:

- fine-grained leaderboard interpretation of partial credit
- feeding verify results back to the agent
- scheduling policies for frequent repeated online verifies
- isolation via independent container snapshots
- cross-task /workspace cleanup (§14.5)

---

## 21. References

- `harbor/src/harbor/verifier/stream_verifier.py`: implementation of single-container multi-task
  verify for streams
- `harbor/src/harbor/verifier/verifier.py:85-88`: single-task `upload_dir` injection of tests
- `harbor/src/harbor/agents/oracle.py:79-82`: the upload + exec contract of the single-task oracle
- `harbor/src/harbor/models/trial/paths.py:34-35`: definitions of the environment path constants for
  `/tests/` and `/solution/`
- `aos_bench/task_construction/assemble/dockerfile_merge.py:498-518`: where `_oracle/` is currently
  baked into the image
- `aos_bench/task_construction/scripts/convert_wildclawbench.py`: the WildClawBench
  converter, including the path rewrite and Dockerfile template
