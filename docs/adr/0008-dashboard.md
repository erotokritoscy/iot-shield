# ADR 0008: Dashboard — pure API client with a typed boundary

Date: 2026-07-09 · Status: accepted (discussed and approved by Andreas)

## Context
The dashboard is the product to everyone who isn't building it, and it is the
API's first real customer — building it against the API only (never storage)
surfaces API gaps before the week-3 review and keeps the frontend swappable.

## Decision
- **Streamlit, three pages** (Triage / Assets & telemetry / Data health),
  single-app sidebar navigation. Entry point is a two-line script
  (`streamlit_app.py`) importing `main()` from the module — all logic stays
  importable, which is what makes AppTest + monkeypatch injection work.
- **Typed ApiClient**: every response parses through the contract models, so
  UI code holds validated objects and API drift fails loudly at the parse
  boundary. The client accepts any `httpx.Client`; tests inject FastAPI's
  `TestClient` for in-process, real-API coverage with no server.
- **Charts per the dataviz method** (form → color-by-job → validate → marks):
  single-series line + reserved status color for detection overlays
  (pair CVD-validated, ΔE 75.7); severity counts as a one-hue bar (axis labels
  carry identity); stat tiles for KPIs (not charts). Altair — bundled with
  Streamlit, declarative, supports the detection-window overlay. Every chart
  ships with tooltips and an accompanying table/caption, so meaning never
  rides on color alone. Light theme only for the MVP.
- **Testing strategy**: AppTest per page against seeded AND empty databases
  (the empty state is what every fresh compose-up shows), plus the
  unreachable-API path (friendly error, not a traceback). Client tests cover
  typed parsing, filter forwarding, lineage resolution, and error surfacing.
- **No caching, no auto-refresh** in v1: Streamlit reruns fetch fresh data
  per interaction; correctness first, optimize when a real dataset hurts.
- Client-side aggregation (severity counts) is honest at MVP data sizes;
  server-side aggregates are the documented upgrade path if data grows.

## Consequences
- The full read path is now demoable: pipeline -> API -> dashboard.
- Alert acknowledge/close stays deferred with the PATCH endpoint (ADR-0007).
- Compose (next step) packages runner + API + dashboard + Postgres.
