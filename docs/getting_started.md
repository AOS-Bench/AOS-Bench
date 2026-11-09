# Getting started

This guide sets up AOS-Bench on a plain Linux host with Docker, runs one workload, and explains
the outputs. The [README](../README.md) has the short version. [中文版](./getting_started_zh.md)

## 1. Requirements

| | |
|---|---|
| OS | Linux with cgroup v2 |
| Docker | Engine 20.10 or later, with the `docker compose` v2 plugin |
| Resources | a stream trial uses 4 CPUs and 8 GiB; plan for at least 8 CPUs, 16 GiB and 100 GB of free disk for images |
| Tools | `git`, [Git LFS](https://git-lfs.com), [uv](https://github.com/astral-sh/uv), Python 3.12 or later |
| Model access | an API key for the agent's model; optionally an OpenRouter key for the LLM judge used by a few graders |

Check Docker:

```bash
docker compose version          # must print v2.x
docker run --rm hello-world
```

If `docker compose` is missing but `docker-compose` is installed, link it as a plugin:

```bash
mkdir -p ~/.docker/cli-plugins
ln -sf "$(command -v docker-compose)" ~/.docker/cli-plugins/docker-compose
```

## 2. Install

```bash
git clone --recurse-submodules https://github.com/AOS-Bench/AOS-Bench.git
cd AOS-Bench

# Harbor-AOS has its own environment; put aos_bench on its path.
(cd harbor && uv sync --all-extras)
echo "$(pwd)" > "$(ls -d harbor/.venv/lib/python3*/site-packages)/aos_bench.pth"

# The per-job dispatcher image, built once.
docker build -t aos-bench/dispatcher:v1 -f aos_bench/pool/Dockerfile.dispatcher .
```

If you cloned without `--recurse-submodules`, run `git submodule update --init`.

## 3. Get the workloads

Tasks and workloads are in [AOS-Bench-Task](https://github.com/AOS-Bench/AOS-Bench-Task). Install
[Git LFS](https://git-lfs.com) first; some environment files are stored with it:

```bash
git lfs install
git clone https://github.com/AOS-Bench/AOS-Bench-Task.git data
```

| Directory | Contents |
|---|---|
| `data/stream/V1-0/valid-0917/medium/` | 10 workloads of 10 tasks |
| `data/stream/V1-0/valid-0917/heavy/` | 5 workloads of 20 tasks |
| `data/stream/V1-0/valid-0917/heavy-plus/` | 1 workload of 30 tasks (case study) |
| `data/stream/V1-0/valid-0917-individual-dedup/<tier>/` | the same tasks, one directory per task, for individual runs |

A workload directory holds `stream.toml` (resources, prebuilt image, arrival schedule),
`environment/` (the merged Dockerfile) and `tasks/<task-id>/` (instruction, tests and reference
solution for each task). The format is specified in [task_stream.md](./tasks/task_stream.md).

`stream.toml` pins a prebuilt image on Docker Hub (`zhuerle/aosbench`), pulled on first use.
Run `docker login` beforehand to avoid anonymous pull limits. To build from the Dockerfile
instead, add `--force-build` to `harbor run`. Some images take an hour or more to build.

## 4. Choose an agent

| Agent | `--agent` | Example `--model` | Credentials (`--ae`) | Settings used in the paper (`--ak`) |
|---|---|---|---|---|
| Claude Code | `claude-code` | `claude-opus-5` | `ANTHROPIC_API_KEY` | `version=2.1.241`, `reasoning_effort=high`, `permission_mode=bypassPermissions`, `disallowed_tools=WebSearch,WebFetch` |
| Codex | `codex` | `gpt-5.6-sol` | `OPENAI_API_KEY`, `CODEX_FORCE_API_KEY=1` | `version=0.50.0`, `reasoning_effort=high`, `wire_api=responses` |
| Kimi Code | `kimi-code` | `kimi/kimi-k3` | `KIMI_API_KEY` | `version=0.36.1` |

Other models behind compatible endpoints:
- **Claude Code**: point it at an Anthropic-compatible endpoint with `ANTHROPIC_BASE_URL` and
  `ANTHROPIC_AUTH_TOKEN`. The paper ran GLM-5.3 and DeepSeek-V4-Pro this way.
- **Codex**: use `OPENAI_BASE_URL`.
- **Kimi Code**: set the endpoint explicitly with `KIMI_MODEL_BASE_URL`, `KIMI_MODEL_API_KEY`
  and `KIMI_MODEL_PROVIDER_TYPE` (`kimi`, `openai` or `anthropic`).

## 5. Run a workload

```bash
cd harbor
uv run harbor run \
  --path ../data/stream/V1-0/valid-0917/medium/v1-medium-10-cdf6049a \
  --task-type stream --aos-pool-v03 --env docker \
  --agent claude-code --model claude-opus-5 \
  --ak version=2.1.241 --ak reasoning_effort=high \
  --ak permission_mode=bypassPermissions --ak disallowed_tools=WebSearch,WebFetch \
  --ae ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --ve OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
  --agent-timeout-sec 9000 \
  --jobs-dir ../jobs --n-attempts 1 --yes
```

What happens:
1. Harbor starts the dispatcher container for the job, then the trial container (4 CPUs, 8 GiB).
2. The agent is installed and given the dispatcher's MCP tools. It claims tasks with
   `todo_next`, works on them, and reports each one with `todo_update`. Tasks are released
   according to the schedule in `stream.toml`.
3. When the agent stops or reaches `--agent-timeout-sec`, Harbor closes the dispatcher to further
   claims and updates, and stops every process the agent started.
4. Each task's verifier grades the final state of the shared workspace.

Use `--agent-timeout-sec 14400` for heavy workloads and `21600` for the thirty-task workload.
For an **individual** run, point `--path` at a tier directory under
`valid-0917-individual-dedup/`, drop `--task-type stream --aos-pool-v03`, and use
`--agent-timeout-sec 5400`.

**LLM judge.** A few WildClawBench graders call an LLM judge through `OPENROUTER_API_KEY`
(optionally `OPENROUTER_BASE_URL`, `JUDGE_MODEL` and `AOS_JUDGE_VISION_MODEL`). Pass them with
`--ve`. If a workload declares a judge variable in `[verifier.env]` and it is unset, the trial
fails at the start instead of after the agent has run.

## 6. Read the results

Trials are written to `jobs/<job-name>/<trial-name>/`:

| Path | Contents |
|---|---|
| `verifier/reward.json` | reward per task, in [0, 1] |
| `verifier/<task-id>/` | grader output for each task |
| `verifier/scheduling_metrics.json` | release, claim and completion time, and final state, per task |
| `dispatcher/events.jsonl` | every release, claim and status update, with timestamps |
| `agent/` | the agent's transcript and session logs |
| `agent/.harbor-cgroup-resources.tsv` | sampled CPU, memory and I/O counters of the trial container |
| `agent_stop.json` | whether the dispatcher was closed and how many agent processes were stopped |
| `result.json` | phase timings (environment setup, agent execution, verification) and the outcome |

To watch trials as they run:

```bash
python -m aos_bench.monitor --jobs-dir jobs --port 4000   # then open http://localhost:4000
```

## 7. Behind a proxy or a scheduling docker proxy

- **HTTP proxy.** Pass `http_proxy`, `https_proxy` and `no_proxy` through `--ae` (for the agent)
  and `--ve` (for graders). Keep `aos_dispatcher` in `no_proxy`, so MCP traffic to the dispatcher
  is not proxied.
- **Queue label.** If your Docker endpoint is a proxy that routes containers by a queue label,
  set `HARBOR_JOB_QUEUE` to the queue name. Set `HARBOR_JOB_QUEUE_LABEL` if the label key is not
  `harbor.job-queue`.
- **Core pinning.** `HARBOR_CPUSET=0-3` pins the trial container to specific cores.

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The trial hangs during setup and no `aos-disp-*` container appears | The dispatcher image is missing. Build it as in step 2. |
| `ModuleNotFoundError: aos_bench` | The `aos_bench.pth` line from step 2 is missing. |
| `pull access denied` or rate-limit errors for `zhuerle/aosbench` | Run `docker login`, or add `--force-build`. |
| A task is missing from `reward.json` | Its grader failed to run. See `verifier/<task-id>/stdout.txt`. |
| The trial fails at once with `Environment variable '…' not found` | A grader needs a variable that is not set. Pass it with `--ve`. |
| The agent's processes are OOM-killed | The workload exceeded 8 GiB. Docker enforces the limit with a cgroup. In the paper's sandbox it was not enforced, so the paper scores overruns at grading time instead. |
