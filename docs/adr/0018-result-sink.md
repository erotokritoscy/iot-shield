# ADR 0018: The ResultSink — a pluggable result store

Date: 2026-07-15 · Status: accepted (approved by Andreas)

## Context
Until now the pipeline had three composable seams — input (source/normalizer), processing
(detector), scoring (alert engine) — but the **output was welded to Postgres**. The runner
computed and persisted in one interleaved transaction (`normalize -> insert -> detect ->
insert -> read-assets-back -> build_alerts -> insert`), so "where results live" could not be
chosen. That asymmetry is the biggest gap in the extensibility goal: a researcher who wants
iotshield to feed *their* environment had to scrape our database or our read API.

The week-2 design's third pillar (after the compatibility guard and the orchestration UI) is a
**deployment-agnostic ResultSink** (framing B: a *pluggable store*, not merely an additive
notification). Andreas chose framing B with Postgres as the default so nothing breaks, and
maximum extensibility.

## Decision
Split the run into two phases so the store becomes swappable, and add a fourth port.

- **Compute is store-agnostic.** `compute_run` produces a `RunResult` (assets, events,
  malformed, detections, alerts, warnings, provenance) entirely in memory — no database. The
  one coupling that blocked this, reading asset rows back mid-transaction to get criticalities,
  is broken by `resolve_assets`, which resolves the inventory in memory (registration wins,
  then auto-discover). The discovered-stub rule is shared with the storage layer via
  `contracts.discovered_asset`, so the two can never diverge.

- **The `ResultSink` port** (`sink/`): `write(result: RunResult) -> None`. Unlike the other
  ports it declares **no `Capability`** — a sink is a *terminal consumer of the whole result*,
  not a typed joint in the transformation chain, so the compatibility guard does not apply.
  Its extensibility comes from registration + discovery + swap-by-name, exactly like the others.

- **The transaction moves into the Postgres sink.** `PostgresSink` does what the runner did —
  one atomic transaction, the repository as the only SQL — now behind the port. `session.begin()`
  is a Postgres concern, so it lives with Postgres. The read API and dashboard are unchanged:
  they are consumers of the Postgres *store*, which is the default sink.

- **`FileSink` (JSONL)** is the first non-DB sink and the proof of framing B: a deployment can
  run with **no Postgres and no dashboard** and still capture every run losslessly to a file
  (one self-contained JSON object per run, serialized through the contracts' own pydantic dump).
  When a server sets `output_dir`, only the basename of a caller-supplied path is honored under
  it, so the UI/API cannot be tricked into writing arbitrary server paths.

- **Config & injection.** `sinks: list[SinkConfig]` defaults to `[postgres]` (so every existing
  config is unchanged); `database_url` becomes optional, *required only when a postgres sink is
  present*. `execute_run` takes a `SinkResources` bag (session factory, output dir) instead of a
  bare session factory — the API injects its long-lived engine, and a host run builds a DB engine
  only when a configured sink `requires_database` (a file-only run builds none). Sinks run in
  config order (system-of-record first); a sink that raises fails the run (no cross-store
  atomicity — a best-effort delivery sink must swallow its own errors).

## Consequences
- **Adding an output is now one file + one registry line** — a webhook/SIEM sink appears in
  `/modules` and the compose UI automatically, with the runner untouched. The input seam's
  extensibility story is finally mirrored on the output.
- A **file-only deployment** is a first-class mode (`config/demo_file.yaml`) with no database.
- **Deferred:** re-enriching alerts from a persistent store across runs (inject `known_assets`
  into `compute_run`); best-effort/retry delivery semantics and a webhook/SIEM sink.
