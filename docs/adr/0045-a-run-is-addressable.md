# ADR-0045: A run is addressable

- **Status:** accepted
- **Date:** 2026-11-01
- **Related:** ADR-0018 (result sinks), ADR-0039 (organizations — `org_id` as a storage fact),
  ADR-0033 (a capped list must say so), ADR-0031 (the dashboard)
- **Scope:**
  - the sink port: `RunResult.run_id`
  - storage: a nullable, indexed `run_id` column on `events`, `malformed_records`,
    `detections` and `alerts`; an optional `run_id` on every insert and list function
  - the read API: an optional `run_id` query parameter on `/events`, `/malformed`,
    `/detections`, `/alerts`
  - the Pipeline API: `RunResponse.run_id`
  - the dashboard: a Run page (`/runs/:runId`); the Compose page's result tiles link to it

  **No contract changes.** The four contracts are untouched: `run_id` is a storage fact,
  exactly as `org_id` is.

## Context

A run on the Compose page ended in four counts — *110 events, 2 malformed, 8 detections,
2 alerts* — and nothing to follow them to. Asked to make them clickable, there was no honest
target: the store recorded *whose* each row was (ADR-0039) but not *which run* wrote it, so a
link could only open the organization's whole store. On a long-lived deployment "2 alerts"
would lead to a queue of hundreds, and the number on the tile and the rows behind it would
disagree — a summary that cannot be checked against its own detail.

## Decisions

### D1 — The id is computed with the run and lives on `RunResult`

`RunResult.run_id` is a fresh `uuid4` per computed result. It sits on the result rather than
on `SinkResources` beside `org_id` because the two are different kinds of fact: the tenant is
decided by *who asked* (and must not be choosable through the spec), whereas a run's identity
is a fact about *what was computed* — one result, one id — and every sink should see the same
value. The Postgres sink stamps it on each row; the file sink writes it on the run's JSON line,
so the two stores of one run can be joined.

### D2 — A column on four tables, not a `runs` table

`run_id` is a nullable, indexed column on the four data tables, written through an optional
keyword on the repository's insert functions. A `runs` table (with run_at, sources, warnings)
would make runs listable, but nothing asks for that yet, and it would add a foreign key the
super admin's delete-an-organization path must order around. The column is the smallest thing
that makes a run *addressable*; a table can be added beside it later without changing its
meaning.

**Nullable on purpose.** Rows can arrive outside a run — a replay, a backfill, a test writing
through the repository — and the repository already says so (`_ensure_assets`). Inventing an
id for them would make "which run wrote this" answerable with a fiction. Assets carry no
`run_id`: a device outlives the runs that saw it.

### D3 — Filtering is inside the tenant, never instead of it

Every list function takes `run_id` as an optional filter *in addition to* the required
`org_id`. So another organization's run id finds nothing, indistinguishable from an id that
never existed — the same stance as an alert id. A run id is a name, not a capability, and
`tests/api/test_run_scope.py` holds that across both APIs.

### D4 — One Run page, opened in a new tab

The tiles open `/runs/<id>#<list>` in a new tab — new, so following a count never discards the
chain composed on the wizard. The page shows all four lists for that run, scrolled to the one
clicked, rather than four filtered copies of the Monitor pages: those pages are views of the
*store*, and threading a run filter through Triage, Assets and Data health would make each of
them answer two questions. Alerts link onward to Triage (`/?alert=<id>`), where the inspector
already lives. Events are listed on every lane, which no page did before — Assets shows
telemetry only.

A tile links only when the run was written to Postgres (a `postgres` destination): a
file-only run stored nothing the read API serves, and a link to an empty page would read as a
lost run rather than as a sink choice. A zero stays a plain number.

## Consequences

- **The schema changed with no migration**, as ADR-0039's did: an existing database needs
  `docker compose down -v`. Rows stored before this change would in any case carry no
  `run_id`, so the Run page would say nothing is stored under them.
- Lists on the Run page are still capped (ADR-0033) and say so; a very large run shows its
  most recent rows.
- **Open:** runs are addressable but not *listable*. A "recent runs" view needs the `runs`
  table D2 deferred.
