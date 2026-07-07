# ADR 0002: Codebase foundations

Date: 2026-07-07 · Status: accepted

## Context
The MVP integrates five student modules (all Python) into one product. We need one
codebase that a solo intern + AI agents can develop fast without breaking things silently.

## Decision
- **Single language: Python 3.11+** for everything (pipeline, API, dashboard). All candidate
  modules are Python; the plan's stack (FastAPI, Streamlit, SQLAlchemy/Postgres) is Python-native.
  One language = one toolchain = one test runner.
- **One package, src layout** (`src/iotshield/`), organized by pipeline stage (ports), not by
  technical layer. Modules-as-adapters inside one process; no per-algorithm microservices
  (matches the plan's service boundaries: api, ai-engine/pipeline, db, dashboard).
- **Tooling: uv** (env + deps + lock), **ruff** (lint + format), **pyright** standard mode
  (type check), **pytest**. All four run in CI and via `scripts/check.sh`.
- **Student code stays outside the repo** in `../reference/` (read-only, with `INDEX.md`).
  We wrap or re-implement against our contracts; we never import reference code at runtime.
  Rationale: reference trees contain committed venvs/node_modules/datasets (repo bloat,
  agent-search noise, licensing ambiguity) and their quality is uneven.
- **Contracts as pydantic v2 models; ports as `typing.Protocol`** (structural typing keeps
  adapters dependency-free of each other).

## Consequences
- Adding a module = new adapter + passing the port's contract test suite; swapping is cheap.
- Anything reused from reference code must be copied in with provenance noted, then held to
  this repo's type/lint/test standards.
- Python-version floor 3.11 (system interpreter); can raise later via uv without pain.
