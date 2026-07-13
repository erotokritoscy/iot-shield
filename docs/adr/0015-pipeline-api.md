# ADR 0015: Write-side Pipeline API (the control plane)

Date: 2026-07-13 · Status: accepted (approved by Andreas)

## Context
The productization pivot (`docs/design/week-2.md`) turns the pipeline into a service an
operator *triggers*: submit a composed configuration + input, get results. Two forces make
a dedicated write-side API the right shape, not a convenience:

- **The dashboard cannot run the pipeline itself.** ADR-0008 makes the dashboard a pure API
  client that never touches storage; the runner writes to storage. So the *only* way the
  orchestration UI can trigger a run is by calling an API. The write API is required.
- **The read API must stay write-free.** ADR-0007's read side is safe and horizontally
  scalable precisely because it only reads. A pipeline run is slow, effectful, and can OOM
  (a model detector) — folding it into the read app would forfeit those properties.

## Decision
Add a **separate write-side app and container role**, `create_control_app(engine)`, mirroring
the read API's construction but purpose-built as the writer. It reuses all the shared
plumbing (contracts, engine/session factory, repository, runner, the guard) and is launched
as its own role: `python -m iotshield.api.control` (default port 8100).

- **Operator/deployment split — the tenancy boundary.** `PipelineConfig` is factored into a
  `PipelineSpec` (operator-chosen: `source_format`, `normalizer`, `detectors`, `alert_engine`,
  `assets`) plus the deployment-owned `database_url`. The API accepts a `PipelineSpec` off the
  wire and stitches in `database_url` *server-side* (`config_from_spec`), so a caller can never
  point a run at another database. `PipelineSpec`'s `extra="forbid"` means a spec that smuggles
  `database_url` is rejected. This split is also where a future output-sink selection will live.

- **Input-acquisition seam.** The runner is split in two: `run_pipeline(config, path)` (CLI /
  standalone — reads the file, owns a per-run engine, creates the schema) and
  `execute_run(config, text, origin, session_factory)` (the guarded compute+persist core against
  an injected session factory). The API calls `execute_run` with its own long-lived engine, so it
  never creates a server-side file or churns engines per request. This is only the *entrance*
  seam; the *exit* (compute-vs-persist / ResponseSink) stays deferred behind the SaaS-vs-on-prem
  decision — runs still persist, exactly as before.

- **Endpoints.** `GET /health` (proves DB connectivity), `GET /modules` (the adapter catalogue —
  each adapter's declared capability plus a param schema introspected from its factory signature,
  which drives the UI menus), `POST /runs/validate` (dry-run the guard, for live UI feedback),
  `POST /runs` (run → summary). The guard runs **once**, inside the run: `execute_run` raises a
  typed `IncompatiblePipelineError` on an ERROR-level issue, which the endpoint maps to **422 with
  the issues** — so the runner owns the run-or-refuse policy and the API never pre-runs the guard.
  WARNINGs ride along in the response. The run response is a *summary* (counts + warnings + top
  alerts); full results are read back from the read API.

- **Sync, persist-and-summarize.** A run executes inline and persists (the interim/on-prem
  behavior). Returning full insights to the caller (`ResponseSink`) and async job semantics
  (submit → poll) are the week-2 §9-B-gated extensions, explicitly not built now.

- **Evolves ADR-0007.** There is now a second entry point to writing — though the write logic is
  still the runner's, invoked over HTTP. The read API is untouched.

- **Zero contract changes.** `PipelineSpec`, the request/response models, and the discovery DTOs
  are new, non-contract types.

## Consequences
- The read plane stays write-free and independently scalable; a slow or crashing run cannot take
  down the dashboard's backend. The two APIs are separate deploy/scale/failure domains.
- `GET /modules` makes the catalogue self-describing: a researcher who adds one registry entry
  (factory + capability, ADR-0014) sees the adapter appear in discovery — and thus the UI — with
  no API change.
- The API is the product surface, dogfooded by our own orchestration UI (ADR-0016) from day one.
- Deferred (unchanged): the output-side result sink + `ResponseSink`, async job semantics,
  per-stage services, and streaming ingest — each with its own trigger in the week-2 design.
