# Project-owned PyCastle Agent image.
# PyCastle builds this file from the repository root and pins the resulting
# image for one Run. Once initialized, this entire file belongs to the project.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.210
ARG CODEX_VERSION=0.144.5
ARG GIT_USER_NAME=krishnakartik1
ARG GIT_USER_EMAIL=krishnakartik1@gmail.com
ARG PYCASTLE_HOST_UID=1000
ARG PYCASTLE_HOST_GID=1000

COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        procps \
        python3 \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system user.name "${GIT_USER_NAME}" \
    && git config --system user.email "${GIT_USER_EMAIL}" \
    && npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

RUN groupmod --gid "${PYCASTLE_HOST_GID}" node \
    && usermod \
        --uid "${PYCASTLE_HOST_UID}" \
        --gid "${PYCASTLE_HOST_GID}" \
        node \
    && install -d -o node -g node /pycastle/auth

# --- PROJECT TOOLCHAIN -----------------------------------------------------
# Install the interpreters, compilers, package managers, and OS libraries that
# this project's Setup, Runtime nodes, and Gate require. PyCastle never fills
# this section by inspecting repository manifests.
# ---------------------------------------------------------------------------

USER node
ENV HOME=/home/node
WORKDIR /home/node
