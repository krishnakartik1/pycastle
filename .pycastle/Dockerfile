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
# Add your project's own language toolchains and system packages here, e.g.:
#
#   RUN apt-get update && apt-get install -y --no-install-recommends \
#         python3 python3-pip \
#     && rm -rf /var/lib/apt/lists/*
#
# Leave this block empty if a plain Node toolchain is all your repo needs.
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
