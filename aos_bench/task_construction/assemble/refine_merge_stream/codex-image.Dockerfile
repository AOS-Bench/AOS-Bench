FROM node:20-bookworm

RUN sed -i \
      -e 's|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
      -e 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git bash \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global @openai/codex@0.144.1 \
    && useradd --create-home --shell /bin/bash agent

USER agent
WORKDIR /refine
