# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE} AS builder

ARG CODEX_VERSION=0.145.0
ARG PACK_ROOT=/opt/aos-agents/codex

# node:22-bookworm-slim sets NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
# but does not ship the ca-certificates package, so node starts with an empty
# trust store and every npm request fails UNABLE_TO_VERIFY_LEAF_SIGNATURE. That
# reads like a proxy MITM but is a missing package. Only bites on a cold build
# where no layer cache hides the npm step.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install --prefix "${PACK_ROOT}" --omit=dev --no-audit --no-fund \
      "@openai/codex@${CODEX_VERSION}" \
    && mkdir -p "${PACK_ROOT}/node/bin" \
    && cp "$(command -v node)" "${PACK_ROOT}/node/bin/node"

COPY bin/codex /opt/aos-agents/codex/bin/codex
RUN chmod 0755 /opt/aos-agents/codex/bin/codex

FROM ubuntu:24.04
ARG CODEX_VERSION=0.145.0
COPY --from=builder /opt/aos-agents/codex /opt/aos-agents/codex
RUN /opt/aos-agents/codex/bin/codex --version \
    | grep -F "${CODEX_VERSION}"

LABEL org.aos.agent.name="codex" \
      org.aos.agent.version="${CODEX_VERSION}" \
      org.aos.agent.install-path="/opt/aos-agents/codex"

ENTRYPOINT ["/opt/aos-agents/codex/bin/codex"]
CMD ["--version"]
