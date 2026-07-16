# Project-owned PyCastle Agent image.
# Built from the repository root and pinned for one Run.
FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code @openai/codex \
    && npm cache clean --force

RUN useradd --create-home --shell /bin/sh pycastle \
    && install -d -o pycastle -g pycastle /pycastle/auth

# --- PROJECT TOOLCHAIN -----------------------------------------------------
# Install project-owned interpreters, compilers, package managers and libraries.
# ---------------------------------------------------------------------------
USER pycastle
ENV HOME=/home/pycastle
WORKDIR /home/pycastle
