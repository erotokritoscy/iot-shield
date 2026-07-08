# ADR 0006: Pipeline runner — config-driven registry, atomic runs

Date: 2026-07-08 · Status: accepted (discussed and approved by Andreas)

## Context
Contracts, ports+stubs, and storage exist in isolation; the runner is the spine
that composes them — the walking skeleton's first end-to-end walk, and the
internship plan's week-2 deliverable (CLI + module-selection config file).

## Decision
- **Config-driven adapter registries** (`pipeline/registry.py` + YAML config):
  each port has a name→factory map; the config names which adapters run and
  with which params. Integrating a future module = one registry entry + one
  YAML line; the runner never changes. Unknown names/params fail loudly with
  the known alternatives. Config parsed by pydantic, extra keys forbidden.
- **One transaction per run.** The repository's flush-not-commit design pays
  off here: everything flushes along the way, one commit at the end. A crash
  mid-run leaves zero rows — a run either happened or it didn't.
- **The alert engine sees this run's detections only** (in-memory list, no DB
  re-query). Cross-run correlation raises supersede/re-score questions we are
  not answering in the MVP; recorded as future work. Assets ARE read back from
  the DB, so scoring sees registrations and fresh discoveries alike.
- **The RunReport is the demo surface** until the API/dashboard exist: counts
  plus top alerts with rationales, printed and returned for tests.
- Adapters are built and input read before any DB is touched (fail fast on
  config errors). CLI: `python -m iotshield.pipeline --input ... --config ...`,
  argparse (stdlib), exit 2 with a message on config/input errors.

## Consequences
- Demo artifacts (`data/demo/demo.csv`, `config/demo.yaml`) are covered by an
  end-to-end test with exact expected counts — the demo cannot rot silently.
- Deferred: cross-run correlation; run-tracking table (`pipeline_runs`) — the
  report covers the need until the API step proves otherwise.
