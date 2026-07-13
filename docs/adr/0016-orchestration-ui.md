# ADR 0016: Orchestration UI (compose & run)

Date: 2026-07-13 · Status: accepted (approved by Andreas)

## Context
The pivot (`docs/design/week-2.md` §5.1) asks for an operator-facing surface to *compose* a
pipeline — pick a normalizer, detectors, and an alert engine, be told immediately if the
choice is incoherent, and trigger a run — usable by a non-specialist. The Pipeline API
(ADR-0015) provides the mechanism; this is its front end.

## Decision
Add a **"Compose & run" page** to the existing Streamlit dashboard, backed by a second typed
client, `ControlClient`, that talks to the Pipeline API — mirroring the read-side `ApiClient`
and preserving ADR-0008 (the dashboard only ever talks to APIs; it never touches storage or
the runner).

- **Registry-driven, so it never goes stale.** The page renders entirely from `GET /modules`:
  the normalizer/detector/alert-engine menus are the catalogue's names, and each adapter's
  parameter form is generated from the introspected param schema. A researcher who adds one
  registry entry (ADR-0014) sees their module appear here with **no UI code changed** — the
  extensibility goal made visible.

- **Incoherence is caught two ways.** The ingest-ERROR is made *structurally impossible*: the
  input format is derived from the chosen normalizer's declared `consumes`, not entered
  independently, so it always matches. Everything else is caught by calling `POST /runs/validate`
  live on each rerun and rendering the issues — ERRORs in red, WARNINGs in amber. The run button
  is disabled until validation reports no errors and input is present.

- **Trigger + confirm; read the rest under Triage.** On run, the page calls `POST /runs` and shows
  the returned summary (counts, warnings, top alerts) with a pointer to the Triage page, which
  reads the full results from the read API exactly as before. The write path (compose→run) and the
  read path (browse results) stay cleanly separated.

- **The page is self-contained** on the read side: it uses only the Pipeline API and returns before
  the read-API health gate, so the two surfaces fail independently.

## Consequences
- The dashboard is now a **reference client of the product surface** — the exact API a customer
  would call is dogfooded by our own UI from day one.
- Typed drift protection extends to the write side: an API/UI schema mismatch fails loudly in
  `ControlClient` (parsing the Pipeline API's response models), never renders wrong.
- Deferred (unchanged): multi-tenant auth/session, richer input handling (large-file upload,
  streaming), and any SaaS-vs-on-prem-specific result presentation — all gated on the week-2 §9-B
  business decisions.
