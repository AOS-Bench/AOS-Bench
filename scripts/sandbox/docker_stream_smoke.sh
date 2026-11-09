#!/usr/bin/env bash
# scripts/docker_stream_smoke.sh — End-to-end smoke for the v1 main-line
# Docker sandbox backend.
#
# Mirrors scripts/vm_stream_smoke.sh but uses harbor's docker backend with
# cgroup-based isolation (--cpus + --cpuset-cpus + --memory). On hosts where
# Docker only accepts jobs via a docker-proxy queue, set HARBOR_JOB_QUEUE
# so harbor labels the trial container for that queue
# (label key HARBOR_JOB_QUEUE_LABEL, default harbor.job-queue; see
# harbor/src/harbor/environments/docker/docker-compose-job-queue.yaml).
#
# Usage:
#   OPENROUTER_API_KEY=sk-or-v1-... ./scripts/docker_stream_smoke.sh
#
# Optional overrides (env vars):
#   STREAM_PATH       Path to the AOS-Bench stream directory.
#                     Default: <repo>/streams/aos-bench-sample-v1
#   JOBS_DIR          Where harbor writes the job/trial tree.
#                     Default: $HOME/aos-bench-logs/docker-smoke
#   JOB_NAME          Job-name (becomes the JOBS_DIR subdir). Defaults to a
#                     short timestamped name.
#   AGENT_MODEL       opencode model spec (provider/model). Default
#                     openrouter/z-ai/glm-4.7.
#   N_ATTEMPTS        --n-attempts. Default 1.
#   OVERRIDE_CPUS     CFS quota cap (cores). Default 4.
#   OVERRIDE_MEMORY   Memory cap in MB. Default 4096.
#   OVERRIDE_STORAGE  Workspace volume cap in MB. Default 20480.
#   AGENT_SETUP_MULT  --agent-setup-timeout-multiplier. Default 5.0.
#   ENV_BUILD_MULT    --environment-build-timeout-multiplier. Default 6.0.
#   HARBOR_JOB_QUEUE  Docker-proxy queue name. Default empty = no queue label.
#   HARBOR_CPUSET     Physical core pinning (e.g. "0-3"). Default empty =
#                     no pinning.
#
# Prereqs (one-time):
#   * Docker engine reachable
#   * docker compose v2 available as `docker compose` subcommand
#     (symlink /usr/sbin/docker-compose into ~/.docker/cli-plugins/ if
#      only the standalone binary is installed)
#   * harbor venv: `cd harbor && uv sync`
#   * Stream directory exists and contains stream.toml

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HARBOR_DIR="${PROJECT_ROOT}/harbor"

: "${STREAM_PATH:=${PROJECT_ROOT}/streams/aos-bench-sample-v1}"
: "${JOBS_DIR:=$HOME/aos-bench-logs/docker-smoke}"
: "${JOB_NAME:=docker-sample-$(date +%H%M%S)}"
: "${AGENT_MODEL:=openrouter/z-ai/glm-4.7}"
: "${N_ATTEMPTS:=1}"
: "${OVERRIDE_CPUS:=4}"
: "${OVERRIDE_MEMORY:=4096}"
: "${OVERRIDE_STORAGE:=20480}"
: "${AGENT_SETUP_MULT:=5.0}"
: "${ENV_BUILD_MULT:=6.0}"

# Only needed behind a docker proxy that routes containers by a queue label.
: "${HARBOR_JOB_QUEUE:=}"

# Optionally pin the container to specific physical cores so neighbouring
# processes don't bleed into trial timing, e.g. HARBOR_CPUSET=0-3.
: "${HARBOR_CPUSET:=}"

if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "error: OPENROUTER_API_KEY is not set." >&2
  echo "       export OPENROUTER_API_KEY=sk-or-v1-... and rerun." >&2
  exit 2
fi

if [[ ! -d "${STREAM_PATH}" || ! -f "${STREAM_PATH}/stream.toml" ]]; then
  echo "error: STREAM_PATH=${STREAM_PATH} is not a stream directory" >&2
  echo "       (expected stream.toml inside it)." >&2
  exit 2
fi

if ! command -v docker >/dev/null; then
  echo "error: docker not on PATH." >&2
  exit 2
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "error: 'docker compose' v2 subcommand not available." >&2
  echo "       Install the cli-plugin or symlink the standalone binary:" >&2
  echo "         mkdir -p ~/.docker/cli-plugins && \\" >&2
  echo "         ln -sf \$(command -v docker-compose) ~/.docker/cli-plugins/docker-compose" >&2
  exit 2
fi

mkdir -p "${JOBS_DIR}"

CMD=(
  uv run harbor run
  --path "${STREAM_PATH}"
  --task-type stream
  --env docker
  --aos-pool-v03
  --agent opencode
  --model "${AGENT_MODEL}"
  --ae "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"
  --jobs-dir "${JOBS_DIR}"
  --job-name "${JOB_NAME}"
  --n-attempts "${N_ATTEMPTS}"
  --n-concurrent 1
  --override-cpus "${OVERRIDE_CPUS}"
  --override-memory-mb "${OVERRIDE_MEMORY}"
  --override-storage-mb "${OVERRIDE_STORAGE}"
  --agent-setup-timeout-multiplier "${AGENT_SETUP_MULT}"
  --environment-build-timeout-multiplier "${ENV_BUILD_MULT}"
  --debug
  --yes
)

# harbor's docker backend reads HARBOR_JOB_QUEUE / HARBOR_CPUSET from os.environ
# at construction time and conditionally appends the matching compose overlay.
# Empty string = skip the overlay (don't propagate it).
ENV_VARS=()
[[ -n "${HARBOR_JOB_QUEUE}" ]] && ENV_VARS+=("HARBOR_JOB_QUEUE=${HARBOR_JOB_QUEUE}")
[[ -n "${HARBOR_CPUSET}" ]]    && ENV_VARS+=("HARBOR_CPUSET=${HARBOR_CPUSET}")
[[ -n "${http_proxy:-}" ]]     && ENV_VARS+=("http_proxy=${http_proxy}")
[[ -n "${https_proxy:-}" ]]    && ENV_VARS+=("https_proxy=${https_proxy}")

printf '== job:    %s\n== stream: %s\n== model:  %s\n== logs:   %s/%s\n' \
  "${JOB_NAME}" "${STREAM_PATH}" "${AGENT_MODEL}" "${JOBS_DIR}" "${JOB_NAME}"
printf '== queue:  %s\n== cpuset: %s\n== cpus/mem/storage: %s / %sM / %sM\n' \
  "${HARBOR_JOB_QUEUE:-<none>}" "${HARBOR_CPUSET:-<none>}" \
  "${OVERRIDE_CPUS}" "${OVERRIDE_MEMORY}" "${OVERRIDE_STORAGE}"

cd "${HARBOR_DIR}"
exec env "${ENV_VARS[@]}" "${CMD[@]}"
