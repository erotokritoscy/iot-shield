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
   test suite as every other adapter of that port, and declares a **capability** (what it
   consumes/produces, co-located with the adapter) so the compatibility guard and orchestration
   UI can read its shape without constructing it (ADR-0014). Result **sinks** (`sink/`, ADR-0018)
   are the fourth port — the pluggable output — but being *terminal* consumers of the whole run
   they declare no capability and sit outside the guard; they are still registered and swapped by
   name like the others. If an adapter needs a contract change, that's a design smell — stop and
   discuss.
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
docker compose up -d            # postgres -> init -> read API (:8000) + Pipeline API (:8100) -> dashboard (:8501)
docker compose run --rm runner  # batch job (profile "jobs"): populate the DB
docker compose down -v          # stop and drop the Postgres volume
```

The DB location is injected via `IOTSHIELD_DATABASE_URL` (compose → Postgres),
which overrides the config file's `database_url` (ADR-0010); the config file
keeps its SQLite default for host runs. Schema creation is owned by the one-shot
`python -m iotshield.storage.init` step (which the API, dashboard, and runner
depend on in compose); the read-only API never creates the schema, and the
runner still creates it for standalone host runs. The first real detector (team3,
ADR-0012) settled the runtime dependency surface as **numpy-only** (sklearn/torch
deferred to future per-model ADRs); image hardening is now unblocked but not yet done.

A run ingests **one or more sources** (ADR-0019): `PipelineSpec.sources` is a list of
`{normalizer, source_format, path?}`, each routed to its normalizer by its declared format
(explicit tagging, never sniffing) and pooled into the detector lanes — so one run can mix
modalities. The legacy top-level `source_format`/`normalizer` desugars to a one-element
`sources` list, so old configs still work. Detectors are handed their lane's events sorted
by `(asset_id, timestamp)` — a port guarantee windowed models rely on.

Seven demo runs exercise the adapters: `config/demo.yaml` + `data/demo/demo.csv`
(telemetry → the `stub` normalizer → `stub_threshold` detector → detections/alerts);
`config/demo_syslog.yaml` + `data/demo/demo_syslog.log` (syslog → the `syslog`
normalizer, re-implemented from team4 → LOG-lane events across three hosts, both
RFC dialects + quarantined malformed lines; no detections by design — ADR-0011);
`config/demo_team3.yaml` + `data/demo/demo_team3.csv` (telemetry → the `team3_zscore`
detector, vendored from team3, on a real SKAB valve fault → one collective
detection → one alert — ADR-0012); `config/demo_triage.yaml` +
`data/demo/demo_triage.csv` (both detectors → the `weighted` engine over four
assets → a *ranked* queue where severity, asset criticality, and detection volume
each visibly reorder the alerts — ADR-0013); `config/demo_file.yaml` +
`data/demo/demo.csv` (the SAME pipeline as `demo.yaml`, but a **file/JSONL sink**
instead of Postgres — no database, no dashboard — demonstrating the pluggable
result store, ADR-0018); `config/demo_log.yaml` + `data/demo/demo_log.log` (an sshd brute
force → the `log_burst` detector's `auth_failure` preset → twelve failures in ~60s merged
into ONE collective detection → one HIGH alert; every line is severity INFO, so the
`severity_spike` preset would see nothing — ADR-0020); and `config/demo_multimodal.yaml`
(two sources in ONE run — `data/demo/demo_multimodal.csv` telemetry +
`data/demo/demo_multimodal.log` syslog on the same asset — routed, pooled into two lanes,
and **both judged**: the telemetry spike plus a log burst correlate into one alert whose
`corroboration` factor is 1.5, the first time cross-lane agreement has ever fired — the
same spike scores ~27/LOW on telemetry alone vs ~56/HIGH corroborated — ADR-0019 + ADR-0020).

The team3 artifact is committed (`src/iotshield/detect/team3/artifacts/skab_zscore.json`);
regenerate it only when the model/data change: `uv run python -m iotshield.detect.team3.train`.

## Layout

```
src/iotshield/
  contracts/       frozen data contracts (pydantic v2) — the product's core
  capabilities.py  adapter capability vocabulary (what each stage consumes/produces)
  normalize/    Normalizer port + adapters (stub, syslog, ...); a run routes each of its sources here by format (ADR-0019)
  detect/       Detector port + adapters (stub threshold, telemetry-ad, log burst, ...)
  alert/        scoring/correlation port + adapters (stub, weighted, ...)
  sink/         ResultSink port + adapters (postgres default, file/JSONL, ...) — pluggable output
  storage/      SQLAlchemy models + repositories (Postgres; SQLite for tests)
  pipeline/     compute (store-agnostic) + registry + compatibility guard (run_pipeline entry point)
  api/          FastAPI services: read side (app.py) + write-side Pipeline API (control.py)
  dashboard/    Streamlit app (consumes the APIs only: Monitor = Triage/Assets/Health; Operate = Compose & run)
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
