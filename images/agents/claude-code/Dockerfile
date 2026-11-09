# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE} AS builder

ARG CLAUDE_CODE_VERSION=2.1.198
ARG PACK_ROOT=/opt/aos-agents/claude-code

RUN npm install --prefix "${PACK_ROOT}" --omit=dev --no-audit --no-fund \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && mkdir -p "${PACK_ROOT}/node/bin" \
    && cp "$(command -v node)" "${PACK_ROOT}/node/bin/node"

COPY bin/claude /opt/aos-agents/claude-code/bin/claude
RUN chmod 0755 /opt/aos-agents/claude-code/bin/claude

FROM ubuntu:24.04
ARG CLAUDE_CODE_VERSION=2.1.198
COPY --from=builder /opt/aos-agents/claude-code /opt/aos-agents/claude-code
RUN /opt/aos-agents/claude-code/bin/claude --version \
    | grep -F "${CLAUDE_CODE_VERSION}"

LABEL org.aos.agent.name="claude-code" \
      org.aos.agent.version="${CLAUDE_CODE_VERSION}" \
      org.aos.agent.install-path="/opt/aos-agents/claude-code"

ENTRYPOINT ["/opt/aos-agents/claude-code/bin/claude"]
CMD ["--version"]
