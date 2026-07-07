# Run the full local quality gate (same as CI).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

uv run ruff format --check .
if ($LASTEXITCODE -ne 0) { exit 1 }
uv run ruff check .
if ($LASTEXITCODE -ne 0) { exit 1 }
uv run pyright
if ($LASTEXITCODE -ne 0) { exit 1 }
uv run pytest
if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Host "All checks passed."
