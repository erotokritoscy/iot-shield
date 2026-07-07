# ADR 0005: Storage layer — projection tables, asset discovery, SQLite-tested Postgres

Date: 2026-07-08 · Status: accepted (discussed and approved by Andreas)

## Context
Storage has three jobs: the correlation join point (detections addressable by
asset + time), the read-side foundation for API/dashboard, and the audit trail
(lineage + never-drop). Postgres is the product database per the internship plan;
tests must not require Docker.

## Decision
- **Tables are a projection of the contracts, not a merger.** Contracts stay
  persistence-ignorant; each ORM class carries `from_contract`/`to_contract`
  converters, pinned lossless by round-trip tests. Rejected: SQLModel-style
  fusion — it gives the frozen contracts a second master.
- **SQLAlchemy 2.0 as the backend abstraction**: Postgres in compose, SQLite
  in-memory in tests, same code via the engine URL. No import-time engine and
  no default URL — config must say where the database is (avoids Team1's
  hanging-localhost-default failure). Repository functions take an explicit
  Session, flush but never commit: transaction boundaries belong to the caller.
- **`UtcDateTime` column type**: requires aware datetimes on write, re-attaches
  UTC on read — SQLite returns naive datetimes, which would otherwise fail our
  own contracts on rehydration and make the test suite dishonest.
- **Enums stored as wire-format strings** (contracts police vocabulary; native
  PG enums are operationally painful and don't exist on SQLite). **JSON columns
  get a JSONB variant on Postgres.**
- **Lineage as a junction table** (`alert_detections`), not a JSON array:
  queryable in both directions, FK-enforced (dangling lineage is impossible).
- **Asset discovery with real foreign keys.** `events/detections/alerts.asset_id`
  all FK to `assets`. Before any insert, `_ensure_assets` stubs unknown ids:
  name=id, type="unknown", criticality=MEDIUM, metadata `{"auto_discovered": true}`.
  Ids are never generated — they arrive in the data (natural keys, ADR-0003).
  Precedence: discovery only fills absence; registration (`upsert_assets`)
  inserts or updates and always wins. This resolves the trilemma: FK-without-
  discovery rejects data (violates never-drop); no-FK leaves the inventory
  incomplete and every device query NULL-handling. Discovered-but-unregistered
  devices become a queryable product feature.
- **`create_all`, not Alembic** — no deployed data to migrate; documented
  productization gap for the final report.

## Consequences
- The runner (next step) persists at every hop through the repository and owns
  transactions; API/dashboard get their read queries by extending repository.
- Known limits, accepted: `_ensure_assets` select-then-insert races under
  concurrent writers (MVP runner is single-process; hardening is ON CONFLICT DO
  NOTHING); garbage asset ids in parseable data become visible stub rows (by
  design — surfacing beats hiding); junction does not preserve `detection_ids`
  list order (lineage is set-like).
