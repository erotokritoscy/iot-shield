# ADR 0010: Full-compose topology — env-injected DB URL and schema ownership

Date: 2026-07-09 · Status: accepted (approved by Andreas)

## Context
The walking skeleton ran as loose processes against SQLite. To make it
reproducible (`docker compose up`), to exercise real Postgres (closing the
SQLite-only blind spot ADR-0009 flagged), and to give team3's heavy dependency
tree a container boundary to land behind, we package the four components —
Postgres, the read API, the dashboard, and the batch runner — as a Compose
stack. Two questions had to be decided; this ADR records them.

## Decision 1 — the database URL is injected via the environment
`load_config` now lets `IOTSHIELD_DATABASE_URL` override the config file's
`database_url` when set (and supply it when the file omits it). So one config
file (`config/demo.yaml`, SQLite) works unchanged on the host, while compose
points the same file at Postgres via env — the same 12-factor path the API and
dashboard already use. This lightly bends ADR-0006's "config is the file," but
only for the one value that is genuinely environment-dependent (where the
database lives); the module-selection surface stays entirely in the file.
Credentials therefore live only in `.env` (gitignored), never in committed YAML.

The suite is made hermetic against this: a root autouse fixture clears
`IOTSHIELD_*` so a developer's shell can't redirect a test's database.

## Decision 2 — schema ownership: a dedicated init step, but the runner stays self-contained
Previously both the API (`create_app`) and the runner created the schema
("dual ownership … so start order never matters"). With a shared Postgres that
invites two services racing `create_all`, so we add a one-shot init step,
`python -m iotshield.storage.init`, which creates the schema and exits. In
compose it is the single owner: the api, dashboard, and runner all
`depends_on` it via `service_completed_successfully`, and `postgres` gates *it*
via `service_healthy`.

- **The read-only API no longer creates the schema.** It assumes the tables
  exist — the honest posture for a read side, and the change that gives the init
  step a purpose.
- **The runner keeps creating the schema.** It is a self-contained batch *job*
  whose CLI contract is "run against the given database in one command"; that
  must keep working against a fresh database on the host (and it is what 8
  end-to-end tests and the primary demo command rely on). In compose the runner
  runs *after* init, so its `create_schema` is a harmless no-op and never races
  anything. Stripping it would convert a one-command tool into a two-step flow
  and buy nothing — so strict "init is the only creator" was rejected in favour
  of "init owns it for the long-lived services; the standalone job stays
  standalone."

The only path that changes: running the API alone against a never-initialised
database now shows empty/500 instead of auto-creating tables — a degenerate case
(no data to serve anyway), resolved by running the pipeline (or `init`) first.

## Consequences
- `docker compose up` brings up a readiness-ordered stack on real Postgres;
  `docker compose run --rm runner …` populates it. Parity with production, and
  the review's SQLite-only gap is now exercisable locally.
- The image is intentionally **unhardened** (uv on `bookworm-slim`, root, no
  multi-stage slimming). Hardening waits until team3 settles the dependency
  surface, so it is done once against reality (follow-up).
- A Postgres-backed CI run (same URL wiring) remains the real regression guard
  for dialect-specific bugs (follow-up, noted since ADR-0009).
