# ADR 0009: Review hardening — value-domain validation and boundary fixes

Date: 2026-07-09 · Status: accepted (xhigh code review, fixes approved by Andreas)

## Context
An xhigh multi-agent review of the whole skeleton found that the contracts
police *shape* (aware timestamps, required fields, ordered windows) but not
*value domain*. That one gap produced the review's worst bugs, and several
boundary/robustness issues rode alongside. This ADR records the contract
changes (golden rule 1) and the batch they shipped with.

## Decision — contract changes (the part that needs an ADR)
- **Non-finite floats rejected.** `NormalizedEvent.value`, `DetectionResult.score`
  / `threshold`, and `PrioritizedAlert.priority_score` set `allow_inf_nan=False`.
  `nan`/`inf` are shape-valid floats but crash JSON serialization (starlette
  forbids them → 500) and bind as NULL on SQLite. Rejecting at the contract means
  the normalizer turns bad input into a `MalformedRecord` and a NaN-producing
  detector fails loudly instead of corrupting storage.
- **Epoch-number timestamps rejected.** A `BeforeValidator` on `UtcDatetime`
  rejects bare numbers and numeric strings, which pydantic's lax mode would read
  as epoch seconds — so a column-shifted CSV line becomes malformed data, not a
  silent 1970-dated event. Detection: a real ISO-8601 string never parses as a
  `float()`.
- **`detection_ids` de-duplicated, order-preserving.** Lineage is a set of
  distinct detections; a repeated id would crash the alert↔detection junction's
  composite primary key. The contract normalizes it.

These are tightening-only: every previously valid object stays valid. No stored
data changes shape, so no migration is implied.

## Decision — same-batch fixes (no contract change)
- **Storage drops VARCHAR length caps** on free-form strings (asset_id, name,
  metric, source_format, origin, method, threat_class). The caps existed only in
  the ORM, so contract-valid rows crashed on Postgres while SQLite (which ignores
  VARCHAR limits) stayed green — restoring the "lossless projection" invariant.
- **Reads return the newest rows within a limit**, oldest-first (`get_events`,
  `get_detections`, `list_malformed`): an ASC + LIMIT hid the most recent data.
- **Error paths widened**: `load_config` surfaces `yaml.YAMLError` as a
  `ValueError` (CLI exits 2, not a traceback); the registry catches bad param
  *values* and coerces at construction so they fail fast and name the adapter;
  the dashboard boundary catches `ValidationError`/`JSONDecodeError`, not just
  `httpx.HTTPError`.
- **Dashboard fidelity**: telemetry-lane filter on the detections overlay (no
  cross-lane red bands); chart timestamps fed as UTC wall-clock so the axis
  matches the UTC tables beside it (golden rule 4 at the last hop).
- **Input/config robustness**: runner reads `utf-8-sig` (BOM-safe); the resolved,
  credential-safe database location is printed in the run report and API startup
  log, so a cwd-relative SQLite path pointing two processes at different files is
  visible instead of silently empty.

## Consequences
- The trust boundary now holds on value as well as shape; malformed floats and
  epoch timestamps land in the audit view as intended.
- The SQLite-only test suite still cannot see Postgres-specific truncation — a
  Postgres-backed CI run remains the real long-term guard (follow-up).
- Deferred by agreement: a metric selector / faceted timeline for multivariate
  assets (arrives with SKAB); recalibrating the stub severity floor so `LOW` is
  reachable (superseded by the Team2 scoring adapter); typed per-adapter param
  models and deriving the port-suite cases from the registry (own discussion).
