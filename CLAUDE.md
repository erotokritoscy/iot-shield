# iotshield MVP — developer guide

Docker Compose MVP that turns raw IT/OT input (telemetry, logs, flows) into prioritized,
explained alerts: ingest → normalize → detect (per-modality lanes) → correlate/prioritize →
Postgres → FastAPI → dashboard. Built by wrapping existing student modules as adapters.
CYENS internship project, July 2026 (intern: Andreas Tziakouris).

## Golden rules

1. **Contracts are law.** `src/iotshield/contracts/` defines the shared objects (NormalizedEvent,
   MalformedRecord, DetectionResult, PrioritizedAlert, Asset). Any change to a contract requires
   a new ADR in `docs/adr/` explaining what breaks and why it's worth it.
2. **Ports and adapters.** Modules never talk to each other directly — only through contracts.
   A new detector/normalizer/prioritizer = a new adapter that passes the *same* port contract
   test suite as every other adapter of that port. If an adapter needs a contract change, that's
   a design smell — stop and discuss.
3. **Reference code is read-only.** Student modules live in `../reference/` (see
   `../reference/INDEX.md` — read it before touching any of them; their READMEs are often stale,
   the code is the truth). Never import from `../reference/` at runtime; copy/wrap what we need
   and note provenance in the adapter's docstring.
4. **Timestamps are sacred.** Everything is correlated by (asset, time). All timestamps are
   timezone-aware UTC. Adapters must validate/normalize timestamps at the boundary and reject
   naive datetimes.
5. **Always demoable.** Prefer a stub adapter that keeps the end-to-end path green over a
   half-integrated real module.
6. **Plan first, implement after approval.** Propose and discuss before building; implement
   only once Andreas approves — unless he explicitly says "implement in one shot, without
   approval". (Full working conventions: workspace-root CLAUDE.md, one level up.)
7. **Test thoroughly.** Everything shipped comes with meaningful tests — unit tests for the
   logic, integration tests for the joints, port contract tests for every adapter. Not
   coverage-number worship; the goal is catching errors early. Untested code is not "done".
8. **Docs move with code.** A change that alters behavior, structure, or contracts updates the
   affected docs (CLAUDE.md, README, ADRs, docstrings) in the same change. Stale docs are bugs.

## Commands

```
uv sync                 # create .venv and install everything (incl. dev tools)
uv run pytest           # tests
uv run ruff check .     # lint
uv run ruff format .    # format
uv run pyright          # type check
scripts/check.sh        # all of the above (CI runs the same)
```

Run all four before considering any task done.

```
docker compose up -d            # postgres -> schema init -> API (:8000) -> dashboard (:8501)
docker compose run --rm runner  # batch job (profile "jobs"): populate the DB
docker compose down -v          # stop and drop the Postgres volume
```

The DB location is injected via `IOTSHIELD_DATABASE_URL` (compose → Postgres),
which overrides the config file's `database_url` (ADR-0010); the config file
keeps its SQLite default for host runs. Schema creation is owned by the one-shot
`python -m iotshield.storage.init` step (which the API, dashboard, and runner
depend on in compose); the read-only API never creates the schema, and the
runner still creates it for standalone host runs. The image is intentionally
unhardened until the first real detector (team3) settles the dependency surface.

Two demo runs exercise both normalizer adapters: `config/demo.yaml` +
`data/demo/demo.csv` (telemetry → the `stub` normalizer → detections/alerts), and
`config/demo_syslog.yaml` + `data/demo/demo_syslog.log` (syslog → the `syslog`
normalizer, re-implemented from team4 → LOG-lane events; no detections by
design — ADR-0011).

## Layout

```
src/iotshield/
  contracts/    frozen data contracts (pydantic v2) — the product's core
  normalize/    Normalizer port + adapters (stub, syslog, ...)
  detect/       Detector port + adapters (stub threshold, telemetry-ad, ...)
  alert/        scoring/correlation port + adapters (stub, team2 formula)
  storage/      SQLAlchemy models + repositories (Postgres; SQLite for tests)
  pipeline/     batch runner wiring ports together (run_pipeline entry point)
  api/          FastAPI service (read-side: events/detections/alerts/assets)
  dashboard/    Streamlit app (consumes the API only)
tests/          mirrors src; port contract tests are parametrized over all adapters
docs/adr/       architecture decision records (numbered, append-only)
config/         pipeline configs (which adapters run)
data/demo/      small demo inputs (keep tiny; big datasets stay in ../reference/)
```

## Conventions

- Python 3.11+, fully type-hinted; pydantic v2 for contracts; `typing.Protocol` for ports.
- Small files, one concern each. Match existing naming.
- Tests accompany every adapter; the port test suite is the swap-safety net — never skip it.
- Demo datasets: SKAB telemetry (in `../reference/team3-telemetry-ad/Datasets/`) + synthesized
  same-asset logs. Big/raw datasets are never committed to this repo.
