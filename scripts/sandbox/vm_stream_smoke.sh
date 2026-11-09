#!/usr/bin/env bash
# scripts/vm_stream_smoke.sh — End-to-end smoke for the VM environment backend.
#
# Boots a per-trial qemu+KVM VM with cloud-init, builds the task's docker
# image once on the host (cached as a tarball, shipped into the VM via
# `docker load`), runs the opencode CLI agent against an AOS-Bench stream,
# then verifies. Intended as a fast "is the whole pipeline alive?" check
# after touching harbor's VM backend or the opencode wiring.
#
# Usage:
#   OPENROUTER_API_KEY=sk-or-v1-... ./scripts/vm_stream_smoke.sh
#
# Optional overrides (env vars):
#   STREAM_PATH       Path to the AOS-Bench stream directory.
#                     Default: $HOME/vm-stream-smoke/fix-git-stream
#   JOBS_DIR          Where harbor writes the job/trial tree.
#                     Default: $HOME/aos-bench-logs/debug
#   JOB_NAME          Job-name (becomes the JOBS_DIR subdir). Defaults to a
#                     short timestamped name. Keep <=40 chars to stay clear
#                     of the QMP socket sun_path 108-byte limit, even though
#                     the socket has been moved to /tmp.
#   AGENT_MODEL       opencode model spec (provider/model). Default
#                     openrouter/z-ai/glm-4.7.
#   N_ATTEMPTS        --n-attempts. Default 1.
#   OVERRIDE_CPUS     VM vCPUs. Default 4.
#   OVERRIDE_MEMORY   VM RAM in MB. Default 4096.
#   OVERRIDE_STORAGE  VM disk in MB. Default 20480.
#   AGENT_SETUP_MULT  --agent-setup-timeout-multiplier. Default 5.0.
#   ENV_BUILD_MULT    --environment-build-timeout-multiplier. Default 6.0.
#
# Prereqs (one-time):
#   * qemu-kvm, qemu-utils, cloud-image-utils, genisoimage on PATH
#   * Membership in `kvm` and `docker` groups (we wrap the harbor invocation
#     in `sg kvm -c "sg docker -c '...'"` so re-login is not required)
#   * Base cloud image staged at $HARBOR_VM_BASE_IMAGE (or
#     $HOME/harbor-vm/base/debian-12-genericcloud-amd64.qcow2)
#
# Notes:
#   * The smoke uses harbor's stock opencode agent (npm-installed inside
#     the trial container).
#   * If you see "cloud-init reported an error" in trial.log, dump
#     <trial>/vm/serial.log and grep for "Err:" / "Failed to" / "WARNING".
#     The most common failure is apt-update timing out because the host
#     proxy isn't reachable from the guest; harbor's VM backend now reads
#     http_proxy/https_proxy from the host env and rewrites 127.0.0.1 →
#     10.0.2.2 (SLIRP gateway alias).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HARBOR_DIR="${PROJECT_ROOT}/harbor"

: "${STREAM_PATH:=$HOME/vm-stream-smoke/fix-git-stream}"
: "${JOBS_DIR:=$HOME/aos-bench-logs/debug}"
: "${JOB_NAME:=vm-fixgit-oc-$(date +%H%M%S)}"
: "${AGENT_MODEL:=openrouter/z-ai/glm-4.7}"
: "${N_ATTEMPTS:=1}"
: "${OVERRIDE_CPUS:=4}"
: "${OVERRIDE_MEMORY:=4096}"
: "${OVERRIDE_STORAGE:=20480}"
: "${AGENT_SETUP_MULT:=5.0}"
: "${ENV_BUILD_MULT:=6.0}"
# Pin the Debian 12 cloudimg as the default base — harbor's stock default is
# ubuntu-22.04 but the staged Ubuntu image on this host drops into grub rescue
# (likely a partial download). Debian boots cleanly through cloud-init.
: "${HARBOR_VM_BASE_IMAGE:=$HOME/harbor-vm/base/debian-12-genericcloud-amd64.qcow2}"
export HARBOR_VM_BASE_IMAGE

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

if ! command -v qemu-system-x86_64 >/dev/null; then
  echo "error: qemu-system-x86_64 not on PATH." >&2
  echo "       sudo apt-get install -y qemu-kvm qemu-utils cloud-image-utils genisoimage" >&2
  exit 2
fi

mkdir -p "${JOBS_DIR}"

# Harbor invocations that touch /dev/kvm and /var/run/docker.sock need both
# group memberships. `sg <group> -c` adds the group to the subprocess without
# requiring the user to log out and back in. We nest sg-shells so a single
# invocation has both groups.
INNER_CMD=(
  uv run harbor run
  --path "${STREAM_PATH}"
  --task-type stream
  --env vm
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

# Forward the host outbound proxy. harbor's VM backend reads these and rewrites
# loopback to the SLIRP gateway alias so apt inside the guest can reach it.
PROXY_ENV=("HARBOR_VM_BASE_IMAGE=${HARBOR_VM_BASE_IMAGE}")
[[ -n "${http_proxy:-}" ]]  && PROXY_ENV+=("http_proxy=${http_proxy}")
[[ -n "${https_proxy:-}" ]] && PROXY_ENV+=("https_proxy=${https_proxy}")
[[ -n "${all_proxy:-}" ]]   && PROXY_ENV+=("all_proxy=${all_proxy}")

printf '== job:    %s\n== stream: %s\n== model:  %s\n== logs:   %s/%s\n' \
  "${JOB_NAME}" "${STREAM_PATH}" "${AGENT_MODEL}" "${JOBS_DIR}" "${JOB_NAME}"

# Quote each argv item once so the inner shell sees them as separate words.
INNER_QUOTED="$(printf '%q ' "${INNER_CMD[@]}")"
ENV_QUOTED="$(printf '%q ' "${PROXY_ENV[@]}")"

cd "${HARBOR_DIR}"
exec sg kvm -c "sg docker -c \"env ${ENV_QUOTED}${INNER_QUOTED}\""
