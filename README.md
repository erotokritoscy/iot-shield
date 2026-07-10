# iotshield MVP

Containerized integration of the iotshield.ai modular detection pipeline:
IT/OT input → normalization → per-modality anomaly detection → correlation &
alert prioritization → PostgreSQL → FastAPI → dashboard.

CYENS Centre of Excellence internship project (July 2026), integrating existing
graduate-student modules as swappable adapters behind frozen data contracts.

## Quick start (compose)

The whole stack on real Postgres, reproducibly, in two commands:

```
docker compose up -d           # postgres -> schema init -> API -> dashboard
docker compose run --rm runner # run the demo pipeline to populate the database
```

Then open the dashboard at http://localhost:8501 and the API at
http://localhost:8000/docs. Services find each other by name on the compose
network (the API reaches Postgres at `postgres`, the dashboard reaches the API
at `api`); credentials come from `.env` (copy `.env.example`), with defaults that
run out of the box. `docker compose down -v` tears it all down, volume included.

The `runner` is a job, not a long-lived service — it runs once and exits, so it
sits behind the `jobs` profile and is invoked with `docker compose run`, not
`up`. Point it at other data with `docker compose run --rm runner --input
data/demo/demo.csv --config config/demo.yaml`.

## Quick start (dev, no Docker)

```
uv sync
uv run pytest
```

## Run the demo pipeline

```
uv run python -m iotshield.pipeline --input data/demo/demo.csv --config config/demo.yaml
```

One atomic run: normalize → detect → correlate/prioritize → store (SQLite here;
Postgres via the same config's `database_url` in compose). The end-of-run report
prints counts and the top alerts with their rationales.

A second demo swaps in the `syslog` normalizer (re-implemented from team4,
ADR-0011) — logs instead of telemetry:

```
uv run python -m iotshield.pipeline --input data/demo/demo_syslog.log --config config/demo_syslog.yaml
```

Syslog records land in the LOG lane, so this run's visible results are events and
captured malformed lines, not detections (the telemetry detector ignores them).

A third demo swaps in the `team3_zscore` detector (a robust z-score model vendored
from team3, ADR-0012) over a real SKAB valve fault — the detector lane's payoff:

```
uv run python -m iotshield.pipeline --input data/demo/demo_team3.csv --config config/demo_team3.yaml
```

The input is telemetry that ramps from normal into a sustained valve anomaly, so
this run produces a real detection (one collective anomaly episode) and a
prioritized alert on the `skab-testbed` asset.

## Serve the API

```
$env:IOTSHIELD_DATABASE_URL = "sqlite:///demo.db"   # or the Postgres URL
uv run python -m iotshield.api
```

Read-side endpoints over whatever the pipeline stored: `/health`, `/assets`,
`/events`, `/detections`, `/alerts`, `/alerts/{id}/detections`, `/malformed`,
`/summary` — interactive docs at `/docs`. Responses are the data contracts,
serialized as-is.

## Open the dashboard

```
$env:IOTSHIELD_API_URL = "http://127.0.0.1:8000"   # optional, this is the default
uv run streamlit run src/iotshield/dashboard/streamlit_app.py
```

Three pages against the API: Triage (ranked alert queue + drill-down to
evidence), Assets & telemetry (per-asset timeline with detection overlays),
Data health (counts + the malformed-input audit).

See `CLAUDE.md` for the developer guide and `docs/adr/` for architecture decisions.
Reference student modules live outside this repo in `../reference/` (read-only).
