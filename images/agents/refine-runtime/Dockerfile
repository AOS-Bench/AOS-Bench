# AOS-Bench refine runtime — the long-lived container that REFINE drives Codex in.
#
# Replaces registry.example.com/swedev/synth-agent-runtime:py311-cc21207-codex01441-...
# (that registry is gone). Built from the
# same agent pack as images/agents/codex, so the Codex version stays governed by
# images/agents/agents.lock.toml rather than by an opaque upstream tag.
#
# The agent pack alone is NOT a drop-in. refine_merge_stream/orchestrator.py
# drives this image as a *proxy container*, not as an entrypoint:
#   docker run -d <image> sleep infinity          # so ENTRYPOINT must not be codex
#   docker exec -u 0    ... sh -c 'tar -xzf ...'  # needs sh, tar, mkdir, rm, chown
#   docker exec -u 1001 ... codex exec --json ... # needs codex ON PATH as uid 1001
# Hence: a plain shell CMD, coreutils/tar present, an `agent` user at 1001, and a
# /usr/local/bin/codex symlink. /refine is pre-created and owned by 1001 because
# the orchestrator's first exec writes there before any chown runs.
#
# Model access is Codex's own `model_providers.<name>.wire_api=responses` path
# (orchestrator.py:2389). The model gateway must answer
# POST /v1/responses with 200, so no shim is needed — only GLM_API_KEY, which the
# orchestrator forwards with `docker exec -e` (never in argv).
#
# Build + push on a host that can run containers:
#   docker build -f images/agents/refine-runtime/Dockerfile \
#       -t zhuerle/aosbench:refine-runtime-codex-0.145.0 images/agents/codex
#   docker push zhuerle/aosbench:refine-runtime-codex-0.145.0
#
# Then point REFINE at it:
#   export AOS_REFINE_CODEX_IMAGE=zhuerle/aosbench:refine-runtime-codex-0.145.0
ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE} AS builder

ARG CODEX_VERSION=0.145.0
ARG PACK_ROOT=/opt/aos-agents/codex
ARG NPM_REGISTRY=https://registry.npmjs.org

# node:22-bookworm-slim sets NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
# but does NOT ship the ca-certificates package, so that file does not exist and
# node starts with an empty trust store. Every npm request then dies with
# UNABLE_TO_VERIFY_LEAF_SIGNATURE, which reads like a proxy MITM and is really a
# missing package — installing ca-certificates alone fixes it.
# Do NOT "fix" this with --strict-ssl=false: that would silently accept any cert
# for the supply chain this whole image is built from.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install --prefix "${PACK_ROOT}" --omit=dev --no-audit --no-fund \
      --registry "${NPM_REGISTRY}" \
      "@openai/codex@${CODEX_VERSION}" \
    && mkdir -p "${PACK_ROOT}/node/bin" \
    && cp "$(command -v node)" "${PACK_ROOT}/node/bin/node"

COPY bin/codex /opt/aos-agents/codex/bin/codex
RUN chmod 0755 /opt/aos-agents/codex/bin/codex

FROM ubuntu:24.04
ARG CODEX_VERSION=0.145.0

# ca-certificates is required: Codex talks to the model over HTTPS. tar and
# coreutils carry the workspace staging path. No python — the orchestrator runs
# nothing Python-side inside this container.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates tar coreutils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/aos-agents/codex /opt/aos-agents/codex
# A symlink into the pack does NOT work: bin/codex derives its own root from
# `dirname "${BASH_SOURCE[0]}"/..`, which through /usr/local/bin/codex resolves
# to /usr/local and then fails on /usr/local/node/bin/node. Use a shim that execs
# the real path so the wrapper still sees its own directory.
RUN printf '%s\n' '#!/bin/sh' \
        'exec /opt/aos-agents/codex/bin/codex "$@"' \
        > /usr/local/bin/codex \
    && chmod 0755 /usr/local/bin/codex \
    && codex --version | grep -F "${CODEX_VERSION}"

# uid/gid 1001 is not negotiable — orchestrator.py chowns the workspace to
# 1001:1001 and then execs as -u 1001:1001. ubuntu:24.04 ships an `ubuntu` user
# at 1000, so 1001 is free.
RUN groupadd -g 1001 agent \
    && useradd -u 1001 -g 1001 -m -s /bin/bash agent \
    && install -d -o 1001 -g 1001 -m 0755 /refine /refine/workspace /refine/codex_home

LABEL org.aos.agent.name="codex" \
      org.aos.agent.version="${CODEX_VERSION}" \
      org.aos.agent.install-path="/opt/aos-agents/codex" \
      org.aos.role="refine-runtime"

# No ENTRYPOINT: the orchestrator supplies `sleep infinity` as the command.
CMD ["sleep", "infinity"]
