# PyCastle agent image.
#
# This builds the `pycastle/agent:node22` image the Docker sandbox runs the
# agent inside. It is based on `node:22-slim` so the bundled Claude/Codex CLIs
# available, and it runs as the non-root `node` user the sandbox expects.
#
# Extend it with YOUR project's language dependencies at the marked point below,
# then build it once:
#
#     docker build -t pycastle/agent:node22 .pycastle
#
FROM node:22-slim

# Codex's Rust TLS stack verifies certificates against the system trust store,
# which node:22-slim ships empty. Install ca-certificates as root so codex can
# reach auth.openai.com; the Node-based Claude CLI bundles its own roots and is
# unaffected. git is installed here too: the implement/review prompts tell the
# agent to commit and to read "the diff produced so far", which need git
# regardless of language, so it belongs in the base image, not a stack-specific
# extension point.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Install the agent CLIs your runtime needs. Pin versions to taste.
RUN npm install -g @anthropic-ai/claude-code @openai/codex && npm cache clean --force

# --- PROJECT EXTENSION POINT ------------------------------------------------
# This is a Python project (a pyproject.toml was present at scaffold time), so
# the image carries python3 plus the gate toolchain (ruff/black/pytest). The
# Docker sandbox runs the quality gate INSIDE this image, and a toolchain-less
# image makes that gate fail loud, so these must be baked in.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# node:22-slim (Debian 12) marks the system interpreter externally-managed, so
# --break-system-packages is required to pip-install into it.
RUN pip install --break-system-packages --no-cache-dir ruff black pytest uv poetry

# Project dependencies are installed from the checked-out manifest by setup.
ENV PATH=".pycastle/venv/bin:${PATH}"
# ---------------------------------------------------------------------------

# Pre-create the auth-volume mount dirs owned by `node`. A fresh Docker named
# volume mounted at a path absent from the image initializes root-owned, which
# would block the non-root `node` user from writing its login. Creating them
# node-owned here means a brand-new auth volume inherits node ownership.
RUN mkdir -p /home/node/.claude /home/node/.codex \
    && chown -R node:node /home/node/.claude /home/node/.codex

# The agent runs as the non-root `node` user (the Claude CLI refuses root, and
# files it writes stay owned by a real user rather than root).
USER node
WORKDIR /home/node
