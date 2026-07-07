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

See `CLAUDE.md` for the developer guide and `docs/adr/` for architecture decisions.
Reference student modules live outside this repo in `../reference/` (read-only).
