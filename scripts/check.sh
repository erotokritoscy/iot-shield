#!/usr/bin/env bash
# Run the full local quality gate (same as CI).
set -euo pipefail
cd "$(dirname "$0")/.."

uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
echo "All checks passed."
