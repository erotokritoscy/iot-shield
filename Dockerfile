# One image, three roles: the init step, both APIs, and the batch runner are the
# same codebase with different commands (set in compose). The dashboard is not
# among them — it is a separate nginx image built from web/. This is the MVP
# image — correct and reproducible, deliberately NOT yet hardened (slim base /
# non-root / distroless). The runtime dependency surface was numpy-only until the
# meter detector brought scikit-learn in (ADR-0030), ~100 MB with scipy, which
# makes that hardening pass more valuable rather than less; it is tracked
# separately and intentionally not done in this change.

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

# Put the venv on PATH so `python`, `uvicorn`, etc. resolve to it. Each service
# supplies its own command in docker-compose.yml.
ENV PATH="/app/.venv/bin:$PATH"
