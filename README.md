# iotshield MVP

Containerized integration of the iotshield.ai modular detection pipeline:
IT/OT input → normalization → per-modality anomaly detection → correlation &
alert prioritization → PostgreSQL → FastAPI → dashboard.

CYENS Centre of Excellence internship project (July 2026), integrating existing
graduate-student modules as swappable adapters behind frozen data contracts.

## Quick start (dev)

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
