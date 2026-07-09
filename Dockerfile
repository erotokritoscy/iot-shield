# One image, four roles: the init step, the API, the dashboard, and the batch
# runner are the same codebase with different commands (set in compose). This is
# the MVP image — correct and reproducible, deliberately NOT yet hardened (slim
# base / non-root / distroless): that pass waits until the first real detector
# (team3) settles the dependency surface, so we build it once against reality.

FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Bytecode-compile on install (faster cold start); copy rather than hardlink into
# the venv since the build layer and cache live on different mounts.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Dependency layer first, keyed only on the lockfile so it caches across source
# edits. --frozen fails if uv.lock is stale, keeping the image reproducible.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Project source (README is load-bearing: pyproject reads it), then install the
# package itself into the venv.
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev

# Demo inputs last: they aren't needed to install, and compose bind-mounts over
# them, so editing demo data must not bust the dependency/project layers.
COPY config ./config
COPY data ./data

# Put the venv on PATH so `python`, `streamlit`, etc. resolve to it. Each service
# supplies its own command in docker-compose.yml.
ENV PATH="/app/.venv/bin:$PATH"
