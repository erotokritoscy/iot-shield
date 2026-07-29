# Parity checklist — React dashboard rewrite

One row per parity item in `brief.md` §6, plus the rows the audit of the dashboard source
added (suffix `(added)` — the inventory was a floor, not a ceiling).

Statuses: `todo` · `done` (verified, evidence cited) · `blocked` · `contested`.
**Every row is `done`.** Nothing was dropped, so `DEVIATIONS.md` carries choices, not losses.

Evidence is a test name or a screenshot. Test files are under `web/src/`; screenshots under
`docs/react-rewrite/screenshots/`. The suite is 87 tests (`npm run check`); the screenshots
come from a Playwright walkthrough of the real `docker compose` stack against demo data.

Shorthand for the test files:

| key | file |
|---|---|
| `monitor` | `pages/Monitor.test.tsx` |
| `errors` | `pages/errors.test.tsx` |
| `compose` | `pages/ComposePage.test.tsx` |
| `eval` | `pages/evaluate/EvaluatePage.test.tsx` |
| `charts` | `charts/specs.test.ts` |
| `params` | `lib/params.test.ts` |
| `format` | `lib/format.test.ts` |
| `evallib` | `pages/evaluate/lib.test.ts` |

## Monitor → Triage

| ID | Capability | Status | Evidence |
|----|-----------|--------|----------|
| T1 | KPI row (events / detections / alerts / malformed) from `/summary` | done | `monitor`: "shows the KPI row, the ranked queue and the drill-down" asserts 3/1/1/1; `01-triage-light.png` |
| T2 | Empty state with README pointer | done | `monitor`: "shows guidance rather than an empty table when nothing has run"; `00a-triage-empty.png` |
| T3 | Severity bar chart (fixed order, single hue, hidden when no alerts) | done | `charts`: "fixes the x order so the axis cannot re-scale", "uses one hue", "is hidden entirely when there are no alerts"; `monitor`: "draws the severity chart with its caption"; `01-triage-light.png` |
| T4 | Ranked queue table (severity, priority 1dp, asset, window start UTC, threat class, status) | done | `monitor`: same test asserts the badge, `62.0`, `pump-02`, `2026-07-07 12:00:30`; `01-triage-light.png` |
| T5 | Alert inspector (label format, rationale, conditional recommended action, score components) | done | `monitor`: asserts the option label `HIGH 62.0 — pump-02 @ 2026-07-07 12:00`, the rationale, the action and `asset_criticality`; `01-triage-light.png` |
| T6 | Evidence table via `/alerts/{id}/detections` | done | `monitor`: same test; `01-triage-light.png` (method / lane / score / threshold / window start / end) |
| T7 | Per-detection evidence collapsibles (non-empty `evidence` only) | done | `monitor`: same test clicks "stub_threshold — why it fired" and asserts the payload; `03-triage-evidence.png` |
| T8 **(added)** | A count key absent from `/summary` renders 0, not blank | done | `monitor`: "renders a KPI the summary does not carry as 0, not as blank (T8)" |

## Monitor → Assets & telemetry

| ID | Capability | Status | Evidence |
|----|-----------|--------|----------|
| A1 | Asset picker (auto-discovered suffix) + type/criticality/location caption + empty state | done | `monitor`: "renders the picker caption, the timeline and the all-lane detections table" and "shows the discovery empty state"; `04-assets.png` shows `type: camera · criticality: high · location: line-1`; `00b-assets-empty.png` |
| A2 | Telemetry timeline (telemetry-only overlays; bands + zero-length rules; null values excluded; empty state) | done | `charts`: "draws a zero-length detection window as a rule and a span as a band", "never plots an event whose value is undefined"; `04-assets.png` (two red rules over the value line) |
| A3 | All-lane detections table + empty state | done | `04-assets.png` lists flow, telemetry and log rows for one asset; `monitor` asserts the Detections heading |
| A4 | UTC everywhere on the page, chart axis included | done | `format`: "formats an instant in UTC whatever the viewer's zone", "feeds Vega the UTC wall-clock with no zone"; `charts`: "feeds the timeline UTC wall-clock, not the viewer's zone"; `monitor` asserts `2026-07-07 12:00:30` |

## Monitor → Data health

| ID | Capability | Status | Evidence |
|----|-----------|--------|----------|
| DH1 | Summary counts table | done | `monitor`: "shows the counts and the malformed audit"; `05-data-health.png` |
| DH2 | Malformed table (raw / error / source format / origin) | done | `monitor`: same test asserts `garbage-line`, `unparseable`, `test://seed`; `05-data-health.png` |
| DH3 | Success state when no malformed records | done | `monitor`: "shows a success state when nothing was rejected"; `00c-data-health-empty.png` |
| DH4 **(added)** | The counts table's columns are whatever `/summary.counts` carries | done | `monitor`: "takes the counts table's columns from whatever /summary carries (DH4)" |

## Monitor cross-cutting

| ID | Capability | Status | Evidence |
|----|-----------|--------|----------|
| M1 | Read-API health gate with start command | done | `errors`: "gates Monitor on health and names the start command (M1)" |
| M2 | Endpoint failure mid-page → friendly error, no crash | done | `errors`: "renders an in-page error when one endpoint fails after health passed (M2)" — and asserts the rest of the page still renders |
| M3 | Contract drift → friendly error, no crash | done | `errors`: "renders an in-page error when a response fails contract validation (M3)" and "treats an out-of-vocabulary enum as contract drift, not as a blank badge" |
| M4 **(added)** | The health gate is Monitor's only: Operate and Evaluate work while the read plane is down | done | `errors`: "leaves Operate usable while the read plane is down (M4)" |

## Operate → Compose & run

| ID | Capability | Status | Evidence |
|----|-----------|--------|----------|
| C1 | Catalogue fetch; unreachable / empty-catalogue errors | done | `errors`: "names the Pipeline API's start command when the catalogue is unreachable (C1)", "refuses to render a blank form when the catalogue is empty (C1)" |
| C2 | 1–10 sources (normalizer, conditional format picker, params, upload/paste, origin) | done | `compose`: "shows a format picker only when the normalizer declares more than one dialect (C2)", "takes content from an uploaded file and its origin from the filename (C2)"; paste origin `source-1` asserted in the run-receipt test; `06-compose.png` |
| C3 | Typed param widgets; optional numerics render empty, never coerced to 0 | done | `params`: "opens a numeric with no default EMPTY, and omits it", "preselects the reported default of a closed vocabulary" |
| C4 | Untouched params omitted from the submitted spec | done | `compose`: "omits every untouched parameter from the submitted spec (C4)" asserts the POST body is `{name: team3_zscore, params: {}}`; "sends a parameter the operator actually moved"; `params`: "omits every parameter of an untouched learning detector", "compares like the Python original, not like JavaScript" |
| C5 | 1–10 detector rows; same adapter duplicable (state keyed by index) | done | `compose`: "lets the same adapter appear twice with different params (C5)" asserts two `log_burst` entries with different presets in one request body |
| C6 | Preselection rules (feeds ∧ ready → feeds → first; format-based normalizer preselect) | done | `compose`: "preselects a detector the sources feed AND that is ready to run (C6)" — `stub_threshold` over the alphabetically-first `meter_iforest`, and `stub` over `flow` |
| C7 | Alert engine picker + params | done | `06-compose.png` (section 3, with its collapsible params) |
| C8 | Sink multiselect (postgres default) + params; ≥1 detector and ≥1 sink gates | done | `compose`: "defaults the sink to postgres and captions a run that skips it (C8, C10)" |
| C9 | Live debounced validation; `[stage] message` issues; run-button gating | done | `compose`: "reports a compatible chain, then runs it and renders the receipt (C9, C10)" asserts "Chain is compatible.", the button disabled until content exists, then enabled; `06-compose.png` |
| C10 | Run receipt (4 counts, warnings, destinations, Triage handoff vs non-postgres caption) | done | `compose`: same test asserts 1/0/1/1, "Written to: postgres" and the hand-off notice; `07-compose-run-receipt.png` (real run against the Pipeline API) |

## Evaluate

| ID | Capability | Status | Evidence |
|----|-----------|--------|----------|
| E1 | Mode gated by eval probe; upload fallback when unreachable | done | `eval`: "hides the mode when the eval router was never registered", "shows the mode when the plane answers", "offers the saved-report upload when the Pipeline API is unreachable (D6)" |
| E2 | No-corpora hints; broken corpora listed with reason, excluded from picker | done | `eval`: "lists a broken bundle with its reason and keeps it out of the picker", "names the root, the generate command and the fetchable hints when there are none (E2)" |
| E3 | Corpus picker + captions; fit protocol radio with help texts; no holdout | done | `eval`: "captions the corpus, offers both fit protocols and no holdout", "shows provenance and licence only for a licenced corpus"; `09-evaluate-report.png` |
| E4 | Committed-spec mode (corpus-filtered, run verbatim, no sweep) | done | `eval`: "offers only the specs whose source shape fits the corpus", "explains itself when no committed spec fits", "sends the committed spec VERBATIM and shows what it sent" (asserts `assets` survives in the request body); `08-evaluate-committed-spec.png` |
| E5 | Compose mode (lane eligibility, engine, 1–6 entries, run-together semantics, pruning on corpus switch) | done | `eval`: "offers only detectors the corpus's lanes can feed, and prunes on a corpus switch"; `evallib`: "carries every detector of one entry into ONE spec", "builds one source per declared format"; `11-evaluate-compose-entries.png` |
| E6 | Detector plan (trained/scratch modes, artifact prefill, both blockers) | done | `eval`: "blocks the run when a trained detector names no artifact", "blocks fitting from scratch under protocol `none`, and says why", "says why trained mode offers no knobs, and re-offers them in scratch mode" (asserts `window_size` prefills 60, not 0); `11-evaluate-compose-entries.png` |
| E7 | Sweep (numeric only, qualified keys iff >1 detector, trained excluded, count caption, stale-clear with message) | done | `eval`: "keeps bare sweep keys in a single-detector entry", "qualifies every knob by detector as soon as an entry holds more than one", "clears an invalidated sweep WITH a message rather than silently", "reports what a sweep will cost and sends the values with the request"; `11-evaluate-compose-entries.png` |
| E8 | Empty entries dropped; duplicate labels made unique | done | `evallib`: "drops entries with no detectors", "makes duplicate labels unique so two rows are tellable apart" |
| E9 | Spec echo collapsible + total benchmark-count caption | done | `eval`: "sends the committed spec VERBATIM and shows what it sent"; `08-evaluate-committed-spec.png` (the echo) and `11-evaluate-compose-entries.png` ("This will run **4** benchmark(s)") |
| E10 | Button-gated latched run; stale-result warning; spinner; friendly failure | done | `eval`: "latches the run and warns when the controls drift from it" — the warning names corpus, specs and protocol, and the report stays on screen; `11-evaluate-compose-entries.png` |
| E11 | Report header caption (corpus, description, cases, incidents, protocol, generated-at UTC) | done | `eval`: "renders the header caption, both charts, the timeline and the per-case table"; `09-evaluate-report.png` |
| E12 | Head-to-head table + curve/bars chart; undefined dropped never zero; explanatory captions | done | `charts`: the nine-case curve/bars decision table, "drops undefined scores rather than drawing them as zero", "keeps only the defined scores when a report mixes them"; `eval` asserts the em-dash for undefined cells; `09-evaluate-report.png` |
| E13 | Consistency strip when cases > 1 | done | `eval`: same test asserts the "Consistency across cases" heading; `09-evaluate-report.png` (one dot per recording) |
| E14 | Case timeline (pickers, truncation warning, zero-span floor, latency caption) | done | `charts`: "gives a zero-length span a visible floor rather than omitting it", "draws nothing when there are no faults, detections or alerts", "keeps truth a neutral backdrop and our output the status red"; `09-evaluate-report.png` (truth band, detection comb, alert bar) |
| E15 | Per-case table across all specs; dedup notes as warnings | done | `eval`: same test asserts the "Per case" heading and the deduplicated note; `09-evaluate-report.png` |
| E16 **(added)** | The composed spec's sources come from the corpus's declared `source_formats`, with each normalizer resolved from the registry **by declared format** | done | `evallib`: "builds one source per declared format, in the corpus's order", "resolves each normalizer from the registry, by declared capability" |
| E17 **(added)** | "None of the selected detectors can be trained" notice under a fit protocol | done | `eval`: "says so when a fit protocol is asked of detectors that cannot train (E17)" |
| E18 **(added)** | An unreadable or wrong-shaped saved report is an empty state, not a traceback | done | `eval`: "treats an unreadable saved report as an empty state, not a traceback (E18)" (both cases) |
| E19 **(added)** | Each spec mode fetches only its own endpoints | done | `eval`: "fetches only the endpoints the active mode uses (E19)" |

## Charts (shared invariants)

| ID | Capability | Status | Evidence |
|----|-----------|--------|----------|
| V1 | Palette ported exactly; red reserved for our output; caption/table companions | done | `charts`: "uses one hue: identity is on the axis labels", "keeps truth a neutral backdrop and our output the status red", "draws a zero-length detection window as a rule and a span as a band" (asserts `palette.critical` on both overlays and `palette.series` on the line); every chart in the screenshots carries its caption. Dark-theme re-instancing is logged in DEVIATIONS.md |
| V2 | UTC wall-clock feeding for every temporal axis and tooltip | done | `format`: "feeds Vega the UTC wall-clock with no zone"; `charts`: "feeds the timeline UTC wall-clock, not the viewer's zone" |
| V3 | Hover tooltips on every mark, matching old fields | done | `charts`: "carries the old fields on every mark" asserts the tooltip field lists of the timeline's line and band layers, the head-to-head bars, the consistency strip and the case timeline against the Python originals |
| V4 | Sweep-label parse decides curve vs bars, with all fallbacks | done | `charts`: nine tests covering the happy path, the qualified key, plain labels, a mixed set, two swept parameters, a non-numeric sweep, one point, two different sweeps, and all-undefined scores |
| V5 | Recessive chart frame | done | `recessive()` in `charts/specs.ts` (grid, muted labels, no view stroke, transparent background); visible in `01-triage-light.png`, `04-assets.png`, `09-evaluate-report.png` |

## Cross-cutting / packaging

| ID | Capability | Status | Evidence |
|----|-----------|--------|----------|
| X1 | `web/` scaffold; `npm run check` green | done | `npm run check` = typecheck + eslint + 87 tests + build, exits 0 (output in the session transcript) |
| X2 | nginx + Vite dev proxy (large bodies, ≥120 s on `/pipeline/*`) | done | `web/nginx.conf.template` (64 MB body, 600 s read timeout) and `vite.config.ts` `server.proxy` mirroring both routes; verified live: `GET :8080/api/health` → `{"status":"ok"}` and `GET :8080/pipeline/eval/corpora` → the corpus list |
| X3 | `web/Dockerfile` multi-stage + `dashboard-web` compose service on :8080 | done | `docker compose ps` shows `dashboard-web  Up  0.0.0.0:8080->80/tcp`; every screenshot was taken through it |
| X4 | React counterparts of all old AppTest scenario families | done | seeded Triage incl. KPI values, seeded Assets, seeded Health, three empty states, unreachable-API, endpoint-failure-mid-page, contract-drift-mid-page — `monitor` + `errors`; plus the Compose and Evaluate families in `compose` + `eval` |
| X5 | ADR + README/CLAUDE.md/compose updates; Python gates green | done | `docs/adr/0031-react-dashboard.md`; README (`web/` commands, :8080, the one-flag eval note); CLAUDE.md (commands + layout); compose comments; `scripts/check.sh` → "All checks passed." with 984 passed, 11 skipped |
| X6 | Light + dark themes via tokens; toggle; charts legible in both | done | tokens in `src/index.css`, consumed by `tailwind.config.js`; `01-triage-light.png` vs `02-triage-dark.png` and `09` vs `10-evaluate-report-dark.png` |
