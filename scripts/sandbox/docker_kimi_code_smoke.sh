#!/usr/bin/env bash
# scripts/sandbox/docker_kimi_code_smoke.sh — End-to-end smoke for the
# `kimi-code` arm (@moonshot-ai/kimi-code, npm-pinned) on harbor's docker
# backend, with the v0.3 aos_pool dispatcher wired in.
#
# It is the gate for the whole arm: if the user-layer mcp.json does not reach
# the model, the trial produces no scheduling data at all. The post-run block
# below prints (and, when `jq` is available, evaluates) two checks: R6, the
# main agent called the pool tools; R7, sub-agents can reach the pool.
#
# Modelled on scripts/sandbox/docker_stream_smoke.sh; the differences are all
# forced by the arm:
#   * credentials come from the KIMI_MODEL_* family — kimi-code reads normal
#     provider keys from config.toml only, so KIMI_API_KEY & co. do NOT work
#     in print mode.
#   * an explicit agent timeout is mandatory: `kimi -p` stays alive while a
#     goal / cron / background task is pending, so harbor's timeout — not the
#     CLI — is the termination authority.
#   * the CLI version is pinned via `--ak version=`; the adapter also carries a
#     pinned default, so an unpinned run is still reproducible.
#
# Usage:
#   export KIMI_MODEL_NAME=<model-id-as-the-endpoint-names-it>
#   export KIMI_MODEL_API_KEY=<key>
#   export KIMI_MODEL_BASE_URL=https://<endpoint>/v1
#   ./scripts/sandbox/docker_kimi_code_smoke.sh
#
# Required (no defaults, never hardcoded — the script refuses to run without them):
#   KIMI_MODEL_NAME       Model id. Passed through as-is; note that once this is
#                         set, `-m <real name>` hard-fails inside the CLI, so the
#                         adapter deliberately never emits `-m`.
#   KIMI_MODEL_API_KEY    API key for that endpoint.
#   KIMI_MODEL_BASE_URL   OpenAI/Anthropic-protocol base URL (…/v1). Required
#                         here on purpose: the shipped bundle only has default
#                         base URLs for provider types `kimi` and `openai`, and
#                         silently falling back to api.openai.com is a footgun.
#
# Optional overrides (env vars):
#   KIMI_MODEL_PROVIDER_TYPE  kimi | anthropic | openai. Default openai (the
#                         aos-120 GLM endpoint is OpenAI-protocol only).
#   KIMI_CODE_VERSION     npm version passed as `--ak version=`. Default 0.36.1.
#   LEGACY_ENGINE         "true" → `--ak legacy_engine=true`, which sets
#                         KIMI_CODE_LEGACY_FLAG=1 in the container. Default off.
#                         Escape hatch only. The DEFAULT (v2) engine registers
#                         a user-layer mcp.json's tools correctly against the
#                         real aos_pool dispatcher (verified end to end on
#                         2026-08-18); the earlier "v2 never registers" finding
#                         came from a hand-rolled offline stub. Reach for this
#                         only if a v2 run actually fails the R6 assertion.
#   STREAM_PATH           AOS-Bench stream dir. Default
#                         <repo>/data/stream/V0-2/light/mixed-5-1 (builds locally;
#                         V0-1 streams need the GLM-internal image hub).
#   ARRIVAL_MODE          stream (default, staggered) | burst | continual.
#   JOBS_DIR              Where harbor writes the job/trial tree.
#                         Default <repo>/jobs/kimi-code-smoke (gitignored).
#   JOB_NAME              Job name = JOBS_DIR subdir. Default timestamped.
#   AGENT_MODEL           harbor's --model label. Default
#                         "${KIMI_MODEL_PROVIDER_TYPE}/${KIMI_MODEL_NAME}".
#   AGENT_TIMEOUT_SEC     --agent-timeout-sec. Default 3600.
#   N_ATTEMPTS / OVERRIDE_CPUS / OVERRIDE_MEMORY / OVERRIDE_STORAGE
#   AGENT_SETUP_MULT      --agent-setup-timeout-multiplier. Default 8.0 — the
#                         install has to guarantee node >= 22 (nodesource or nvm)
#                         before `npm i -g`, which is slower than claude-code's.
#   ENV_BUILD_MULT        --environment-build-timeout-multiplier. Default 6.0.
#   HARBOR_JOB_QUEUE      Docker-proxy queue label. Empty = skip the overlay.
#   HARBOR_CPUSET         Physical core pinning, e.g. "0-3". Empty = skip.
#
# Prereqs (one-time): docker + `docker compose` v2, `cd harbor && uv sync`,
# and the dispatcher image (`docker build -t aos-bench/dispatcher:v1
# -f aos_bench/pool/Dockerfile.dispatcher .`).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HARBOR_DIR="${PROJECT_ROOT}/harbor"

: "${STREAM_PATH:=${PROJECT_ROOT}/data/stream/V0-2/light/mixed-5-1}"
: "${JOBS_DIR:=${PROJECT_ROOT}/jobs/kimi-code-smoke}"
: "${JOB_NAME:=kimi-code-$(date +%H%M%S)}"
: "${KIMI_CODE_VERSION:=0.36.1}"
: "${KIMI_MODEL_PROVIDER_TYPE:=openai}"
: "${LEGACY_ENGINE:=}"
: "${ARRIVAL_MODE:=stream}"
: "${AGENT_TIMEOUT_SEC:=3600}"
: "${N_ATTEMPTS:=1}"
: "${OVERRIDE_CPUS:=4}"
: "${OVERRIDE_MEMORY:=4096}"
: "${OVERRIDE_STORAGE:=20480}"
: "${AGENT_SETUP_MULT:=8.0}"
: "${ENV_BUILD_MULT:=6.0}"
: "${HARBOR_JOB_QUEUE:=}"
: "${HARBOR_CPUSET:=}"

# ── Credentials: fail fast, never default ────────────────────────────────
missing=()
for var in KIMI_MODEL_NAME KIMI_MODEL_API_KEY KIMI_MODEL_BASE_URL; do
  [[ -n "${!var:-}" ]] || missing+=("${var}")
done
if (( ${#missing[@]} > 0 )); then
  echo "error: missing required env var(s): ${missing[*]}" >&2
  echo "" >&2
  echo "  kimi-code takes credentials ONLY through the KIMI_MODEL_* family;" >&2
  echo "  KIMI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY are read from" >&2
  echo "  config.toml and are ignored in print mode. Export, then rerun:" >&2
  echo "" >&2
  echo "    export KIMI_MODEL_NAME=<model-id>" >&2
  echo "    export KIMI_MODEL_API_KEY=<key>" >&2
  echo "    export KIMI_MODEL_BASE_URL=https://<endpoint>/v1" >&2
  echo "    # optional: export KIMI_MODEL_PROVIDER_TYPE=openai|anthropic|kimi" >&2
  exit 2
fi

case "${KIMI_MODEL_PROVIDER_TYPE}" in
  kimi | anthropic | openai) ;;
  *)
    echo "error: KIMI_MODEL_PROVIDER_TYPE=${KIMI_MODEL_PROVIDER_TYPE} is not one of" >&2
    echo "       kimi|anthropic|openai (the only types the bundle allows)." >&2
    exit 2
    ;;
esac

: "${AGENT_MODEL:=${KIMI_MODEL_PROVIDER_TYPE}/${KIMI_MODEL_NAME}}"

case "${ARRIVAL_MODE}" in
  stream) ARRIVAL_FLAGS=() ;;
  burst) ARRIVAL_FLAGS=(--aos-all-ready) ;;
  continual) ARRIVAL_FLAGS=(--aos-serial) ;;
  *)
    echo "error: ARRIVAL_MODE=${ARRIVAL_MODE} must be stream|burst|continual." >&2
    exit 2
    ;;
esac

if [[ ! -d "${STREAM_PATH}" || ! -f "${STREAM_PATH}/stream.toml" ]]; then
  echo "error: STREAM_PATH=${STREAM_PATH} is not a stream directory" >&2
  echo "       (expected stream.toml inside it)." >&2
  # The default is the tiered V0-2 path, which only exists on `data/` pins that
  # carry the tier layout. An older pin has flat `data/stream/<name>/` dirs, so
  # show what is actually checked out rather than silently substituting one —
  # the stream choice changes what the run measures.
  if [[ -d "${PROJECT_ROOT}/data/stream" ]]; then
    echo "       streams available on the current data/ pin (first 10):" >&2
    # `|| true`: head closing the pipe SIGPIPEs find, and pipefail would turn
    # that into the script's exit code instead of the intended 2.
    find "${PROJECT_ROOT}/data/stream" -maxdepth 3 -name stream.toml -printf '         %h\n' \
      2>/dev/null | sort | head -10 >&2 || true
    echo "       re-run with STREAM_PATH=<one of those>." >&2
  else
    echo "       data/ submodule looks uninitialised: git submodule update --init data" >&2
  fi
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

if ! docker image inspect aos-bench/dispatcher:v1 >/dev/null 2>&1; then
  echo "warning: aos-bench/dispatcher:v1 image not found locally; the trial will" >&2
  echo "         hang in setup with no aos-disp-* container. Build it with:" >&2
  echo "           docker build -t aos-bench/dispatcher:v1 \\" >&2
  echo "             -f aos_bench/pool/Dockerfile.dispatcher ." >&2
fi

mkdir -p "${JOBS_DIR}"
JOB_DIR="${JOBS_DIR}/${JOB_NAME}"

CMD=(
  uv run harbor run
  --path "${STREAM_PATH}"
  --task-type stream
  --env docker
  --aos-pool-v03
  --agent kimi-code
  --ak "version=${KIMI_CODE_VERSION}"
  --model "${AGENT_MODEL}"
  # The KIMI_MODEL_* family must reach the *container*, so pass it as agent env
  # rather than relying on the harbor process env. The adapter treats anything
  # set explicitly as authoritative and derives nothing over the top of it.
  --ae "KIMI_MODEL_NAME=${KIMI_MODEL_NAME}"
  --ae "KIMI_MODEL_API_KEY=${KIMI_MODEL_API_KEY}"
  --ae "KIMI_MODEL_BASE_URL=${KIMI_MODEL_BASE_URL}"
  --ae "KIMI_MODEL_PROVIDER_TYPE=${KIMI_MODEL_PROVIDER_TYPE}"
  --jobs-dir "${JOBS_DIR}"
  --job-name "${JOB_NAME}"
  --n-attempts "${N_ATTEMPTS}"
  --n-concurrent 1
  --override-cpus "${OVERRIDE_CPUS}"
  --override-memory-mb "${OVERRIDE_MEMORY}"
  --override-storage-mb "${OVERRIDE_STORAGE}"
  # `kimi -p` does not reliably self-terminate, so the agent
  # timeout is the stop condition. Multiplier pinned to 1.0 so the number above
  # is the literal wall budget.
  --agent-timeout-sec "${AGENT_TIMEOUT_SEC}"
  --agent-timeout-multiplier 1.0
  --agent-setup-timeout-multiplier "${AGENT_SETUP_MULT}"
  --environment-build-timeout-multiplier "${ENV_BUILD_MULT}"
  --debug
  --yes
)

if [[ -n "${LEGACY_ENGINE}" && "${LEGACY_ENGINE}" != "false" && "${LEGACY_ENGINE}" != "0" ]]; then
  CMD+=(--ak "legacy_engine=true")
fi

if (( ${#ARRIVAL_FLAGS[@]} > 0 )); then
  CMD+=("${ARRIVAL_FLAGS[@]}")
fi

# harbor's docker backend reads HARBOR_JOB_QUEUE / HARBOR_CPUSET from os.environ
# at construction time and conditionally appends the matching compose overlay.
# Empty string = skip the overlay (don't propagate it).
ENV_VARS=()
[[ -n "${HARBOR_JOB_QUEUE}" ]] && ENV_VARS+=("HARBOR_JOB_QUEUE=${HARBOR_JOB_QUEUE}")
[[ -n "${HARBOR_CPUSET}" ]] && ENV_VARS+=("HARBOR_CPUSET=${HARBOR_CPUSET}")
[[ -n "${http_proxy:-}" ]] && ENV_VARS+=("http_proxy=${http_proxy}")
[[ -n "${https_proxy:-}" ]] && ENV_VARS+=("https_proxy=${https_proxy}")

# Never print a key: first 5 chars only, same convention as the other launchers.
printf '== job:      %s\n== stream:   %s (%s)\n== agent:    kimi-code @%s%s\n' \
  "${JOB_NAME}" "${STREAM_PATH}" "${ARRIVAL_MODE}" "${KIMI_CODE_VERSION}" \
  "$([[ -n "${LEGACY_ENGINE}" ]] && echo ' (legacy engine)' || true)"
printf '== model:    %s via %s (type %s)\n== key:      %.5s… (%d chars)\n' \
  "${KIMI_MODEL_NAME}" "${KIMI_MODEL_BASE_URL}" "${KIMI_MODEL_PROVIDER_TYPE}" \
  "${KIMI_MODEL_API_KEY}" "${#KIMI_MODEL_API_KEY}"
printf '== logs:     %s\n== timeouts: agent %ss · setup ×%s · build ×%s\n' \
  "${JOB_DIR}" "${AGENT_TIMEOUT_SEC}" "${AGENT_SETUP_MULT}" "${ENV_BUILD_MULT}"
printf '== queue:    %s\n== cpuset:   %s\n== cpus/mem/storage: %s / %sM / %sM\n' \
  "${HARBOR_JOB_QUEUE:-<none>}" "${HARBOR_CPUSET:-<none>}" \
  "${OVERRIDE_CPUS}" "${OVERRIDE_MEMORY}" "${OVERRIDE_STORAGE}"

cd "${HARBOR_DIR}"
RC=0
if (( ${#ENV_VARS[@]} > 0 )); then
  env "${ENV_VARS[@]}" "${CMD[@]}" || RC=$?
else
  "${CMD[@]}" || RC=$?
fi

# ── Post-run assertions (R6 / R7) ──────────────────────────────
# Not `exec`'d above precisely so these can run. R6 is the gate: without a
# registered aos_pool the trial has no scheduling data at all.
# Plain globs (no `**`) so the printed commands work in a shell without
# globstar. Harbor writes trials directly under the job dir
# (`<job>/<trial>/{agent,verifier,dispatcher,result.json}`), so that is what
# these assume; the `find` fallback printed below is depth-proof if a launcher
# ever adds an attempt level.
WIRE_GLOB="${JOB_DIR}/*/agent/kimi-code-home/sessions/*/*/agents"

cat <<EOF

── post-run assertions ──────────────────────────────────────────────────
(the globs below assume harbor's layout <job>/<trial>/agent/…; if one expands
 to nothing, resolve the journals depth-proof with
   find ${JOB_DIR} -path '*/agents/main/wire.jsonl'
 and substitute.)
R6a  aos_pool MCP tools registered (the gate):
       jq -r 'select(.type=="mcp.tools_discovered")' ${WIRE_GLOB}/main/wire.jsonl
     expect a record naming server "aos_pool" with the todo_* tools in
     .enabledNames. On the published 0.36.1 DEFAULT (v2) engine this record was
     never observed against local MCP stubs even though the handshake
     completed — if it is empty here too, rerun with LEGACY_ENGINE=true, which
     did register the same server's tools (note: legacy writes protocol 1.4
     with a different record vocabulary).

R6b  the dispatcher actually handed tasks out (tasks.json is an OBJECT —
     {"tasks":[…],"stream_done":…} — so the filter has to go through .tasks):
       jq '[.tasks[] | select(.claimed_at)] | length' ${JOB_DIR}/*/dispatcher/tasks.json
     expect > 0. Zero claims with a green R6a means the model never called the
     tools; zero claims with an empty R6a is the registration gap. Harbor also
     dumps the same shape per trial at ${JOB_DIR}/*/dispatcher_state.json,
     which is inside the job dir even when PERSIST_ROOT is not.

R7   subagents can reach the pool (only the \`agent\` and \`coder\` profiles
     carry mcp__* — \`explore\` and \`plan\` cannot see MCP tools at all):
       jq -r 'select(.type=="context.append_loop_event") | .event.name' \\
         ${WIRE_GLOB}/agent-*/wire.jsonl | grep -c '^mcp__aos_pool__'
     0 (or no agent-* dirs) means the preamble must not tell the lead to
     delegate pool polling.

also worth a look:
     tail -5 ${JOB_DIR}/*/agent/kimi-code.stderr.txt   # the ONLY place run failures appear
     jq -r .type ${WIRE_GLOB}/main/wire.jsonl | sort | uniq -c
     ls ${JOB_DIR}/*/agent/trajectory*.json       # root + trajectory.<agentId>.json
     cat ${JOB_DIR}/*/verifier/reward.json        # verifier truth, not the agent's self-report
── monitor (visual check of the lanes) ──────────────────────────────────
     harbor/.venv/bin/python -m aos_bench.monitor --jobs-dir ${JOB_DIR} --port 4000
EOF

# Evaluate what we can right here, so a green/red is visible without a second
# shell. Purely informational — the run's own exit code is what we return.
if command -v jq >/dev/null 2>&1; then
  echo ""
  echo "── evaluated now ────────────────────────────────────────────────────────"
  n_main=0
  n_mcp=0
  n_sub_pool=0
  while IFS= read -r wire; do
    n_main=$((n_main + 1))
    n_mcp=$((n_mcp + $(jq -r 'select(.type=="mcp.tools_discovered") | .serverName' \
      "${wire}" 2>/dev/null | grep -c . || true)))
  done < <(find "${JOB_DIR}" -path '*/kimi-code-home/sessions/*/agents/main/wire.jsonl' 2>/dev/null)
  while IFS= read -r wire; do
    n_sub_pool=$((n_sub_pool + $(jq -r 'select(.type=="context.append_loop_event") | .event.name' \
      "${wire}" 2>/dev/null | grep -c '^mcp__aos_pool__' || true)))
  done < <(find "${JOB_DIR}" -path '*/kimi-code-home/sessions/*/agents/agent-*/wire.jsonl' 2>/dev/null)
  n_claimed=0
  # Prefer the dispatcher's own tasks.json; fall back to harbor's per-trial dump
  # (identical shape, and inside the job dir even when the dispatcher's
  # PERSIST_ROOT is not). Never both — they describe the same stream and would
  # double-count.
  task_dumps="$(find "${JOB_DIR}" -path '*/dispatcher/tasks.json' 2>/dev/null || true)"
  if [[ -z "${task_dumps}" ]]; then
    task_dumps="$(find "${JOB_DIR}" -name dispatcher_state.json 2>/dev/null || true)"
  fi
  while IFS= read -r tasks; do
    [[ -n "${tasks}" ]] || continue
    # tasks.json is {"tasks":[…],"stream_done":…} (aos_bench/pool/pool.py:544),
    # so `.[]` would iterate the object's VALUES and die on the boolean.
    n_claimed=$((n_claimed + $(jq '[.tasks[] | select(.claimed_at)] | length' "${tasks}" 2>/dev/null || echo 0)))
  done <<< "${task_dumps}"
  printf 'root journals found : %d\n' "${n_main}"
  printf 'R6a mcp.tools_discovered records : %d  %s\n' "${n_mcp}" \
    "$( ((n_mcp > 0)) && echo PASS || echo 'FAIL — see LEGACY_ENGINE=true above')"
  printf 'R6b claimed tasks   : %d  %s\n' "${n_claimed}" \
    "$( ((n_claimed > 0)) && echo PASS || echo FAIL)"
  printf 'R7  subagent mcp__aos_pool__ calls : %d  %s\n' "${n_sub_pool}" \
    "$( ((n_sub_pool > 0)) && echo PASS || echo 'n/a or FAIL (no subagent pool calls)')"
fi

exit "${RC}"
