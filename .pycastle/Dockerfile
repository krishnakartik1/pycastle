# Project-owned PyCastle Agent image.
# PyCastle builds this file from the repository root and pins the resulting
# image for one Run. Once initialized, this entire file belongs to the project.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.210
ARG CODEX_VERSION=0.144.5

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

RUN useradd --create-home --shell /bin/sh pycastle \
    && install -d -o pycastle -g pycastle /pycastle/auth

# --- PROJECT TOOLCHAIN -----------------------------------------------------
# Install the interpreters, compilers, package managers, and OS libraries that
# this project's Setup, Runtime nodes, and Gate require. PyCastle never fills
# this section by inspecting repository manifests.
# ---------------------------------------------------------------------------

USER pycastle
ENV HOME=/home/pycastle
WORKDIR /home/pycastle
