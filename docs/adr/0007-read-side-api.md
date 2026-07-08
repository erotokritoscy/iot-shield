# ADR 0007: Read-side API — contracts as wire format

Date: 2026-07-09 · Status: accepted (discussed and approved by Andreas)

## Context
The skeleton had a write path (runner -> DB) and no read path. Everything
downstream (dashboard, external integrations, the week-3 review) consumes the
database through an API.

## Decision
- **Contract objects ARE the response schema.** Endpoints return pydantic
  contract models directly; FastAPI serializes them. Zero translation code;
  a wire-format round-trip test pins the promise.
- **App factory with injected engine** (`create_app(engine)`); sessions
  per-request via a dependency reading `app.state.session_factory`. Tests
  drive the app with TestClient over SQLite — no server, no Docker.
- **Services configure via environment** (`IOTSHIELD_DATABASE_URL`, host/port)
  — 12-factor; the runner keeps its per-run config file. Different lifecycles,
  different config mechanisms. Host defaults to 127.0.0.1 (compose overrides).
- **Read-only v1.** The runner is the only writer. `PATCH /alerts/{id}/status`
  (triage workflow) deferred until the dashboard proves it wants it — same
  build-when-consumed rule that resurrected `count_summary` in this step.
- **Dual schema ownership accepted**: both runner and API call the idempotent
  `create_schema` at startup so compose start order never matters. Real fix
  (init step/migrations) documented as the productization gap (ADR-0005).
- **No CORS configuration** — the dashboard calls the API server-to-server;
  no browser cross-origin request exists in this architecture. Deliberately
  avoids the reference modules' `allow_origins=["*"]` pattern.
- Validation at the boundary: naive datetime query params are a 422 (golden
  rule 4), lanes/statuses validate against the enums, limits are bounded.

## Consequences
- The dashboard (next step) is a pure API client.
- Repository gained `get_alert_by_id`, `get_detections_by_ids`, `count_summary`.
- Auth is out of scope per the internship plan; the API must not be exposed
  beyond the compose network in this form.
