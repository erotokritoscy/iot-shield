# React dashboard rewrite — specification

You are rewriting the iotshield Streamlit dashboard (`src/iotshield/dashboard/`) as a React
single-page application. This document is the specification. It runs **unattended**: Andreas
is asleep while you work, reviews everything the next morning, and cannot answer questions
mid-run — §2 tells you what to do instead of asking.

## 0 · How to use this document

1. **Read order, before writing any code:** this document in full → `CLAUDE.md` (the golden
   rules apply to you) → all five files in `src/iotshield/dashboard/` → `src/iotshield/api/`
   (`app.py`, `control.py`, `evaluation.py`) → `tests/dashboard/`. While reading the
   dashboard source, **audit the parity inventory (§6) against the code — it is a floor,
   not a ceiling.** It was compiled by another agent in a single survey and may have missed
   capabilities; add anything it missed to `docs/react-rewrite/PARITY.md` as a new ID
   (next number in the page's prefix, marked `(added)`).
2. **Precedence:** `CLAUDE.md` golden rules > the hard constraints (§1) > this brief's
   defaults (§3) and parity semantics (§6) > your judgment. §3 gives you a deviation path;
   §1 does not.
3. **The old dashboard's code comments are part of the spec.** `app.py` and `charts.py`
   document *why* behind nearly every non-obvious behavior. When a parity item references a
   behavior, the comment at that site is the authoritative rationale — read it before
   translating the behavior.
4. Maintain `docs/react-rewrite/PARITY.md` (evidence protocol: §10) and
   `docs/react-rewrite/DEVIATIONS.md` (when to log: §2) as you go. They are how your work
   is judged — by the goal evaluator tonight and by Andreas tomorrow.

## 1 · Mission and hard constraints

**Mission.** A React SPA that reproduces every capability of the Streamlit dashboard —
Monitor (Triage / Assets & telemetry / Data health), Operate (Compose & run), and the
gated Evaluate mode — as a pure client of the two existing APIs, with a calm,
information-dense product look (§7), integrated into the Docker Compose stack.

**Hard constraints.** These are not this brief's opinions — they are the repo's laws
(CLAUDE.md, the ADRs, the task definition) restated. No deviation, no matter how well
argued:

- **H1** `src/iotshield/**` and `tests/**` are read-only. Sole exception: adding CORS
  middleware to the two FastAPI apps *if and only if* a recorded D1 deviation demands it.
  `contracts/` is read-only absolutely, no exceptions.
- **H2** The Streamlit dashboard stays intact and runnable. `uv run pytest` and
  `scripts/check.sh` pass at every commit you make.
- **H3** Pure API client. The browser renders what the APIs return; it never computes
  detections, scores, rankings, or lane logic. (Standing exception, ported as-is: parsing
  sweep labels like `team3[window_size=20]` to choose curve-vs-bars rendering — V4.)
- **H4** Every rendered timestamp — table cells, chart axes, tooltips, captions — displays
  **UTC** (golden rule 4). This must be engineered, not hoped for: JS `Date` formats in
  the viewer's local zone and Vega-Lite shifts tz-aware datetimes the same way. Port the
  `_utc_wallclock` technique for charts; use one shared `formatUtc()` for text.
- **H5** No new backend endpoints. No auth, websockets, or polling. No feature the
  Streamlit app lacks, beyond the recorded divergences in §4.2.
- **H6** Never parse `lane:`-prefixed capability tokens or re-derive server vocabulary
  client-side. Where the old app asked the API for decoded facts (`CorpusInfo.lanes`,
  `source_formats`, `artifact_param`), the new app asks the same way.
- **H7** **A parity item is never dropped by argument — only reported.** If you conclude
  an item is impossible or wrong, mark it `blocked`/`contested` in PARITY.md with a
  DEVIATIONS.md entry, preserve the old behavior where you can, and move on. Rationale for
  the asymmetry: a wrong architecture deviation fails *loudly* (a build breaks, a proxy
  won't route) so your own justification is testable; a wrongly dropped parity semantic
  fails *silently* (runs start refusing, a sweep quietly measures an unswept spec) and
  nobody is awake to catch a plausible-sounding-but-wrong rationale.
- **H8** Nothing irreversible: no deletions outside `web/` and `docs/react-rewrite/`, no
  git history rewriting, no force-push. All work stays on the rewrite branch.

## 2 · Autonomy protocol

Nobody is watching. Never wait for approval; this brief, the constraints, and the goal are
your full authority. Three situations, three moves:

1. **A judgment call the brief doesn't settle** (state shape, component structure, a UX
   detail, naming): decide and proceed. Log it in DEVIATIONS.md only if a reviewer would
   be surprised — routine engineering choices don't need entries.
2. **A default decision (§3, D1–D8) fails on concrete evidence** — a library that won't
   build, an approach that demonstrably can't satisfy a parity item: deviate. Record in
   DEVIATIONS.md what you hit (with the evidence), what you chose instead, and the
   consequences; reflect the final state in the ADR (§10). Plausibility is not evidence —
   deviate on what you *observed*, not what you suspect.
3. **A hard constraint or parity semantic is in your way:** do not deviate (H7). Mark
   `blocked`/`contested`, log it, work on something else.

**Morning-review contract** — Andreas's oversight happens tomorrow, so make it cheap:
all work happens on the **`react-dashboard`** branch, which already exists and should be
checked out when you arrive — verify with `git branch --show-current` before your first
commit, check it out if it is not current, and never create or switch to any other
branch. Commit in reviewable slices, each message naming the parity IDs it lands; save
screenshots under `docs/react-rewrite/screenshots/` *and* surface them in the
conversation; keep DEVIATIONS.md the single index of everything that needs his eyes.

## 3 · Default architecture decisions (D1–D8)

Defaults, not laws: another agent chose them from a survey, and you will know things it
did not. Deviate per §2.2 — on evidence, recorded. Absent evidence against them, follow
them; do not relitigate from taste.

- **D1 — Same-origin via reverse proxy; no CORS.** The Streamlit app called the APIs
  server-to-server, so no browser cross-origin request ever occurred and both FastAPI apps
  deliberately ship no CORS (`api/app.py` docstring). A SPA moves the calls into the
  browser. Serve the SPA from **nginx**, which also proxies `/api/*` → read API and
  `/pipeline/*` → Pipeline API. Consequences: one origin (no CORS, the documented stance
  preserved); the SPA calls only relative paths, so per-environment API URLs vanish from
  the frontend — proxy targets are nginx config fed by env vars, restoring the 12-factor
  property `IOTSHIELD_API_URL`/`IOTSHIELD_PIPELINE_API_URL` provided. The proxy must
  accept large request bodies (run inputs travel inline as JSON) and a **read timeout ≥
  120 s** on `/pipeline/*` (the old ControlClient's timeout — model detectors and
  benchmarks take real time). Dev mode: Vite `server.proxy` with the same two routes, so
  dev and prod agree about paths. Fallback if the proxy path fails: CORS middleware on
  both apps (the one H1 exception), recorded.
- **D2 — Stack:** Vite + React 18 + TypeScript (strict) + Tailwind CSS + shadcn/ui, in a
  new top-level `web/` directory. TanStack Query for server state. No Redux, no
  CSS-in-JS runtime, no second UI kit.
- **D3 — Charts: Vega-Lite via `react-vega`.** `charts.py` is not styling — it encodes
  validated dataviz decisions (single-hue magnitude, reserved status red, truth as neutral
  backdrop, UTC wall-clock, zero-span floors, undefined-never-plotted-as-zero). Altair
  compiles to Vega-Lite, so the six builders port nearly one-to-one as TS functions
  returning specs, preserving those decisions by construction. A native-library restyle is
  a separate future project.
- **D4 — Types from OpenAPI, validation at the boundary.** The old client validated every
  response against the pydantic contracts at runtime, so drift failed loudly at one
  boundary instead of rendering wrong (ADR-0008). TS types are erased at runtime and
  cannot preserve that alone. So: generate TS types from both apps' `/openapi.json`
  (`openapi-typescript`, via a checked-in `web/scripts/` script; commit the output), and
  wrap every fetch in one client module that surfaces non-2xx and parse failures as typed
  errors, with zod runtime checks on the load-bearing shapes (alerts, detections, module
  catalogue, evaluation report). Every page renders API failure as a friendly error state
  — never a blank screen or unhandled rejection (M1–M3).
- **D5 — Evaluate gating: probe, don't flag.** A static SPA can't read server env, so
  `IOTSHIELD_EVAL_UI` can't gate it. The real gate was always `IOTSHIELD_EVAL_API`: unset,
  the `/eval` router is *absent, not guarded* (ADR-0021 D1). Probe
  `/pipeline/eval/corpora` at startup; 404/unreachable hides the mode. Deliberate
  divergence — one flag instead of two; record in the ADR, update compose and any docs
  naming `IOTSHIELD_EVAL_UI`.
- **D6 — Saved-report fallback becomes an upload.** The old Evaluate page fell back to
  reading `reports/evaluation.json` from the *server's* disk; a browser can't. Replacement:
  a file-picker ("load a saved report JSON") whenever the eval API is unreachable — keeps
  the CLI → file → dashboard path with no new endpoint. Deliberate divergence; record it.
- **D7 — Coexistence:** new compose service `dashboard-web` on port **8080**; the
  Streamlit `dashboard` service stays on 8501 untouched. Retiring Streamlit is a separate
  later change that only Andreas triggers.
- **D8 — Testing:** Vitest + React Testing Library + MSW (mock both APIs at the network
  boundary — the moral equivalent of the old suite injecting `TestClient`).
  `tests/dashboard/test_app.py` is the behavioral spec to translate. `web/package.json`
  gets a `check` script: typecheck + eslint + test + build.

## 4 · Translations and divergences

### 4.1 Streamlit mechanisms → React translations

Much of `app.py`'s complexity fights Streamlit's execution model: the whole script re-runs
on every interaction, and widget state outlives the options that produced it. React has
neither problem. **Port the invariant, not the mechanism:**

| Streamlit mechanism | Invariant it protected | React translation |
|---|---|---|
| `st.cache_resource` on clients | don't rebuild an HTTP client per interaction | nothing — a module-scope fetch wrapper is built once |
| `st.cache_data` on `_run_benchmark`/`_parse_report` | a benchmark runs on the button press only; re-exploring a finished report is instant | run on click; keep the last report in state keyed by the exact request JSON |
| latched run + "showing the previous run" warning (`_EVAL_RUN_KEY`) | on-screen results never silently mismatch on-screen controls; a benchmark never re-runs on a stray interaction | keep report + its request in state; when form state ≠ that request, show the warning naming the previous corpus/specs/protocol; re-run only on the button |
| session-state pruning on corpus switch (`_entry_widgets`) | a detector selection must not survive into a corpus where it's ineligible | derive eligibility from corpus; filter (don't reset) selections when corpus changes |
| stale-sweep reset **with a message** (`_sweep_widgets`) | a configured sweep never vanishes silently to quietly measure an unswept spec | when the sweep key is no longer offered, clear it AND render the warning naming what was cleared |
| widget `index=` honored only on first render | preselection never overrides an explicit operator choice | preselect via initial state only; never reset a touched field |
| validation on every rerun (compose page) | operator sees guard verdicts live, before running | call `/pipeline/runs/validate` on spec change, debounced, plus once on mount |

### 4.2 Deliberate divergences — the complete list

D5 (probe replaces `IOTSHIELD_EVAL_UI`), D6 (upload replaces server-file fallback), D7
(new port during coexistence). Anything else that cannot be reproduced exactly is handled
per H7 — reported, never silently diverged.

## 5 · The two APIs (all of the surface the SPA consumes)

Read API (proxied at `/api`): `GET /health`, `/summary` (counts + top_alert), `/assets`,
`/events?asset_id&lane&limit`, `/detections?asset_id&lane&limit`, `/alerts?limit` (already
priority-descending), `/alerts/{id}/detections`, `/malformed?limit`.

Pipeline API (proxied at `/pipeline`): `GET /health`; `GET /modules` — catalogue of
normalizers/detectors/alert_engines/sinks, each `ModuleInfo` with `params: ParamInfo[]`
(name/type/default/choices), `consumes`, `produces`, `formats`, `artifact_param`;
`POST /runs/validate` (`{spec}` → issues with level+stage+message); `POST /runs`
(`{spec, inputs: [{content, origin}]}` → counts, warnings, destinations). Eval router
(present only when `IOTSHIELD_EVAL_API=1`): `GET /eval/corpora` (per-corpus `lanes`,
`source_formats`, `error`, licence/provenance, plus `fetchable` hints and `root`),
`GET /eval/detectors` (`DetectorSummary`: params, lanes, fittable, artifact_param),
`GET /eval/specs` (committed bench specs + the corpora they fit), `POST /eval/run`
(`{corpus, specs: [{label, spec, sweep}], fit_protocol, include_timeline}` →
`EvaluationReport`). Shapes: generate from OpenAPI (D4); pydantic sources are
`api/control.py`, `api/evaluation.py`, `eval/report.py`, `contracts/models.py`.

## 6 · Parity inventory — the completion checklist

Every item must be implemented and *individually verified* (§10 defines "verified"). IDs
are stable — use them in PARITY.md, commit messages, and progress reports. Floor, not
ceiling: extend per §0.1.

### Monitor → Triage

- **T1** KPI row from `/summary.counts`: events, detections, alerts, malformed records —
  four labeled stat tiles.
- **T2** Empty state: no alerts → info panel "No alerts yet — run the pipeline first…"
  (keep the pointer to the README demo), not an empty table.
- **T3** Severity bar chart: alert count per severity, x-order fixed
  `low, medium, high, critical, emergency`, single hue (series blue), tooltip, caption
  "Alert count per severity." Hidden entirely when no alerts.
- **T4** Ranked queue table: severity, priority (1 decimal), asset, window start (UTC),
  threat class, status — in API order (already ranked).
- **T5** Alert inspector: pick an alert (option label format:
  `HIGH 62.0 — pump-02 @ 2026-07-07 12:00`, UTC); shows rationale, recommended action
  (only when present), and the **score components table** (the explainability receipt).
- **T6** Evidence table via `/alerts/{id}/detections`: method, lane, score, threshold,
  window start, window end.
- **T7** Per-detection evidence: for each detection with a non-empty `evidence` payload, a
  collapsible "`{method}` — why it fired" showing the JSON (pretty-printed). This is what
  makes a finding actionable ("cam-07 contacted 41 destinations", not a bare score).

### Monitor → Assets & telemetry

- **A1** Asset picker labeled `name [asset_id]` with an `(auto-discovered)` suffix when
  `metadata.auto_discovered`; caption line: type · criticality · location (location only
  when present). Empty state when no assets.
- **A2** Telemetry timeline: value line over time for the asset's TELEMETRY events, with
  **telemetry-lane detections only** overlaid (spans as translucent red bands, zero-length
  windows as 2 px red rules — deliberately lane-restricted: overlaying flow/log windows on
  a value axis they don't belong to would mislead; the comment in `assets_page` is the
  spec). Caption explains line vs bands. "No telemetry with numeric values" empty state
  (events with `value=None` are not plotted).
- **A3** Detections table for the asset across **all** lanes (method, lane, score,
  threshold, window start) — so a flow- or log-only asset is not invisible on this page;
  "No detections for this asset" otherwise.
- **A4** All timestamps UTC, chart axis included (H4).

### Monitor → Data health

- **DH1** Summary counts table from `/summary`.
- **DH2** Malformed table: raw, error, source format, origin — "captured, never dropped"
  framing kept in the heading.
- **DH3** Success state ("No malformed inputs recorded.") when the list is empty.

### Monitor cross-cutting

- **M1** Health gate: read API unreachable → full-page friendly error including the start
  command (`IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api`).
- **M2** A single endpoint failing mid-page (API died after health passed) → in-page
  friendly error, never a crash. (Old test: `test_endpoint_failure_mid_page…`.)
- **M3** A response failing validation (contract drift) → same friendly error path. (Old
  test: `test_contract_drift_mid_page…`.)

### Operate → Compose & run

- **C1** Catalogue from `/pipeline/modules`; Pipeline API unreachable → friendly error with
  its start command; an empty catalogue → explicit error, not a blank form.
- **C2** 1–10 input sources; each: normalizer picker, format picker (shown only when the
  normalizer declares >1 format), collapsible params, and content via **file upload**
  (decode UTF-8 with replacement) **or** pasted text; origin = uploaded filename, else
  `source-N`.
- **C3** Param widgets typed from `ParamInfo`: `choices` → select (reported default
  preselected); `int`/`float` → numeric input, where a `default: None` numeric renders
  **empty** and stays omitted (never coerce to 0 — a fabricated 0 is refused at adapter
  construction and turns "pick a detector and press run" into a failed run); `bool` →
  checkbox; else text input.
- **C4** **Only touched params are submitted.** A param equal to the catalogue-reported
  default (or left empty with no default) is omitted from the spec. Why this is
  load-bearing: "not given" carries meaning three ways — a preset owns the real default
  (ADR-0024 D3), a learning adapter reads it from its artifact, or absence means "learn it
  from the data" (ADR-0022). Submitting a full prefilled block beside an artifact id is
  exactly the half-configured request ADR-0022 D4 refuses. Port `_untouched`'s comparison
  semantics (value == default, with string-compare fallback).
- **C5** 1–10 detector rows; the **same adapter may appear in several rows with different
  params** (state keyed by row index, not by name) — that is how two presets run at once.
- **C6** Detector preselection for the first row on first render only: prefer a detector
  that (a) consumes a lane the current sources produce AND (b) is ready to run
  (`artifact_param === null`); fall back to (a) alone; fall back to first. Never plain
  alphabetical (that once opened the page on a flow chain fed telemetry CSV, and later on
  an untrained `meter_iforest` that a run refuses). Normalizer preselection: the one whose
  declared `formats` include the stub CSV format — by declared format, not by name.
- **C7** Alert engine picker + collapsible params (same C3/C4 rules).
- **C8** Sink multiselect (default `postgres` when present), per-sink collapsible params;
  ≥1 detector and ≥1 sink required to proceed (info notices, not errors).
- **C9** Live validation: the assembled spec goes to `/pipeline/runs/validate` on change
  (debounced); issues render as `[stage] message` — errors red, warnings amber; "Chain is
  compatible." on a clean pass. Run button enabled only when validation passes AND every
  source has non-blank content.
- **C10** Run: POST with inputs; render the four counts (events, malformed, detections,
  alerts), every warning, destinations; when the postgres sink was selected and alerts > 0
  → hand-off notice to Monitor → Triage; when postgres was **not** selected → caption that
  results are in the chosen sinks, not Triage. (A receipt, not a results view — Triage owns
  browsing.)

### Evaluate (gated per D5)

- **E1** Mode hidden unless the eval probe succeeds (D5). When the eval API is unreachable:
  the upload fallback (D6) with the explanatory message and the CLI command to generate a
  report.
- **E2** No corpora → warning naming the root, the `generate` command, and the per-corpus
  `fetchable` hints. Broken corpora (`error` set) listed with their reason and **excluded**
  from the picker — "my corpus vanished" must never be the only feedback.
- **E3** Corpus picker + caption (cases · description) + provenance/licence caption when
  licenced. Fit protocol radio (`none`/`prefix`) with the two help texts ported verbatim;
  there is deliberately no `holdout`.
- **E4** Two spec modes, mirroring the page: **Run a committed bench spec** — only specs
  whose declared source shape fits the corpus (from `/eval/specs`), multiselect, per-spec
  caption naming its detectors, run **verbatim** (never recomposed), no sweep; none fit →
  explanatory message. **Compose my own** — E5–E9.
- **E5** Compose mode eligibility: detectors whose `lanes` intersect the corpus's `lanes`
  (both server-decoded); none → explanatory message. Engine picker (weighted/stub). 1–6
  entries; an entry is a multiselect of detectors that run **together** in one pipeline
  (that is what makes corroboration measurable); default = one detector per entry walking
  the eligible list; selections pruned (not reset) when the corpus changes (§4.1).
- **E6** Per-detector plan inside an entry: a detector **with** `artifact_param` gets the
  two-mode radio — *Use trained weights* (artifact id text input, prefilled from the
  catalogue default or the known-shipped table `{team3_zscore: skab_zscore}`; caption
  explaining artifact atomicity; **blocker** when empty) / *Fit from scratch* (hyperparam
  widgets; **blocker** when fit protocol is `none`, with the ported explanation — an
  untrained model under `none` scores a row of zeros that reads like a bad detector). A
  detector without `artifact_param` just gets its param widgets. Blockers render as errors
  and disable the run button.
- **E7** Sweep per entry: one **numeric** hyperparameter; the offered keys are
  detector-qualified (`flow_burst.threshold`) iff the entry holds >1 detector, bare
  otherwise (the key text is exactly what the server parses and the report labels quote);
  trained-mode detectors contribute nothing (artifact atomicity — with the caption saying
  so); comma-separated values; "→ N run(s)" caption; stale-sweep clearing with message
  (§4.1).
- **E8** Entries with no detectors are dropped; duplicate labels are made unique
  (`x`, `x (2)`).
- **E9** **Spec echo:** the exact request payload, shown in a collapsible on the page that
  makes it — the reconciliation tool for "dashboard number ≠ CLI number". Plus the total
  benchmark-count caption when sweeps expand.
- **E10** Run: button-gated (primary), disabled on blockers/empty; latched-result semantics
  per §4.1 including the "showing the previous run (corpus …, specs …, protocol …)"
  warning; spinner while running; failure → friendly error.
- **E11** Report header caption: corpus, description, cases, incidents, fit protocol,
  generated-at (UTC).
- **E12** Head-to-head: table (spec, detectors, the `summary()` columns as serialized in
  the report, fitted `k/n`) + chart — **sweep curve when every scored label parses as one
  single-parameter numeric sweep, else bar chart** (V4); undefined scores are **dropped,
  never drawn as zero** (a bar at zero claims a detector scored nothing when nothing was
  measurable); the three explanatory captions (event_f1 vs point_f1 vs compression; empty
  cell = undefined; flat-line-is-a-real-result under the curve) ported.
- **E13** Consistency strip (only when cases > 1): one dot per case per spec + the
  mean-hides-the-shape caption.
- **E14** Timeline: spec picker + case picker; truth/detections/alerts bars on one clock;
  "no timelines in this report" info; truncation warning naming `TIMELINE_SPAN_CAP` (the
  cap value must come from the report/API side or be duplicated with a comment naming its
  source); the latency caption. Zero-length spans drawn with the 1/300th-of-window visible
  floor (min 0.5 s).
- **E15** Per-case table across **all** specs (spec column + case fields, excluding
  `precision_at_k` and `timeline`); the deduplicated union of case notes rendered as
  warnings (`! …` are the guard/ingest explanations for rows of zeros).

### Charts (shared invariants)

- **V1** Palette ported exactly: series `#2a78d6`, status-critical `#d03b3b` (reserved —
  detections/alerts only, never a data series), truth `#b8b6ac` (neutral backdrop, not a
  series hue), grid/muted/baseline neutrals from `charts.py`. Identity never rides on
  color alone — every chart keeps its caption/table companion.
- **V2** UTC wall-clock feeding for every temporal axis and tooltip (H4).
- **V3** Hover tooltips on every mark, matching the old fields.
- **V4** Sweep-label parsing (`base[name=value]` regex) decides curve vs bars; a
  non-matching or non-numeric or multi-series set falls back to bars; <2 points is not a
  curve. Port the logic, not just the regex.
- **V5** The recessive frame: light grid, muted labels, no view stroke — charts sit *in*
  the page, they don't shout at it.

### Cross-cutting / packaging

- **X1** `web/` scaffold per D2; `npm run check` (typecheck + lint + test + build) green.
- **X2** nginx serving + proxy per D1 (large bodies, ≥120 s read timeout on `/pipeline/*`);
  Vite dev proxy mirroring the same paths.
- **X3** Multi-stage `web/Dockerfile` (node build → nginx serve) — not edits to the Python
  image — and the compose service `dashboard-web` on :8080, depending on both APIs'
  health, with proxy targets from env.
- **X4** Test suite per D8, including React counterparts of all scenario families in
  `tests/dashboard/test_app.py` (seeded Triage incl. KPI values, seeded Assets, seeded
  Health, empty states ×3, unreachable-API, endpoint-failure-mid-page,
  contract-drift-mid-page).
- **X5** ADR + docs per §10; `uv run pytest` and `scripts/check.sh` still green.
- **X6** Light and dark theme both styled via tokens (§7); theme toggle; charts legible in
  both.

## 7 · Design brief

**Character.** A calm, information-dense operations console — reference points are Grafana
and Linear, not a marketing site. Data is the interface; chrome recedes. Analyst workflow
first: the Triage queue is the home screen.

**Layout.** Fixed left sidebar: product name, then nav grouped exactly as the modes group
the planes — Monitor (Triage / Assets / Data health), Operate (Compose & run), Evaluate
(only when enabled). Dense tables (right-aligned numerics, tabular figures), page title +
one-line description atop every page. Beyond that, layout is yours.

**Design tokens** — single source: CSS variables in `web/src/index.css`, consumed by
Tailwind config. Change design by changing tokens, never by scattering hex values.

- Accent/interactive: the series blue `#2a78d6` family. Status: critical `#d03b3b`,
  warning amber, success green, info = muted blue. Severity badges in the queue: low →
  muted gray-blue, medium → amber, high → orange, critical → the status red, emergency →
  deep red/near-black — always carrying the text label; color supplements, never replaces
  (CVD rule, same as the charts).
- Neutrals: warm gray ramp consistent with the chart neutrals (`#fcfcfb` paper, `#e1e0d9`
  grid, `#898781` muted, near-black ink); dark theme = the same ramp inverted, blue/red
  re-checked for WCAG AA contrast on both themes.
- Type: Inter (UI) + JetBrains Mono (ids, params, JSON, code); no decorative type.

**Avoid-list** (defaults that otherwise creep in): gradients, glass blur, emoji in UI
copy, rounded-pill mega-buttons, marketing hero sections, skeleton shimmer everywhere
(plain spinners/short fades suffice), color-only status.

**Microcopy:** port the Streamlit captions and help strings near-verbatim — they encode
the explanations (what event_f1 means, why a cell is empty, what a sweep costs). Losing
them loses the product's explainability voice.

**Self-review loop (replaces a human design gate — mandatory).** After the app shell and
the first populated page (Triage on MSW mock data): screenshot light and dark at desktop
width, open the images, and critique them against this section — tokens only? avoid-list
clean? dense enough? severity badges labeled? both themes legible? Iterate once on what
you find, then proceed. For each later page, screenshot and inspect before marking its
parity items done. Save all screenshots under `docs/react-rewrite/screenshots/` and show
them in the conversation — they are Andreas's morning design review.

## 8 · Testing requirements

Per D8/X4. MSW fixtures mirror `tests/dashboard/conftest.py`'s seeded world (pump-02, T0,
one detection, one HIGH alert, one malformed record) so the two suites assert the same
story. Component tests per page: seeded render, empty state, error state. Interaction
tests for the load-bearing form semantics: C4 (untouched params omitted — assert the
outgoing request body), C5 (two rows of one adapter keep independent params), E6 blockers,
E7 stale-sweep clearing message, E10 latched-result warning. Chart-spec unit tests: V4's
curve/bars decision table, the zero-span floor, UTC feeding (pass a tz-aware instant,
assert the spec datum is UTC wall-clock). A Playwright smoke pass against the real compose
stack is a stretch goal, not a completion requirement.

## 9 · Packaging & compose integration

Per D1/D7/X3: `web/Dockerfile` (stage 1: node `npm ci && npm run build`; stage 2:
nginx-alpine, `dist/` + templated nginx conf whose env-fed proxy targets are
`http://api:8000` / `http://pipeline-api:8100` in compose). Compose service
`dashboard-web`, 8080:80, `depends_on` both APIs healthy. The root Python `Dockerfile`,
the `dashboard` service, and ports 8000/8100/8501 are untouched.

## 10 · Order of work and process rules

**Suggested order** — reorder freely, no log entry needed: (1) scaffold + tokens + shell +
typed API layer + MSW harness; (2) Monitor pages + their charts; (3) Operate; (4)
Evaluate; (5) packaging/compose + full-stack verification in the real browser against demo
data; (6) ADR + docs + final audit. What is *not* a suggestion: work incrementally with
the gates green at every commit (H2 plus `npm run check` once `web/` exists), and finish
with a final audit that walks PARITY.md item by item.

**Evidence protocol.** PARITY.md lists every ID with status
(`todo`/`done`/`blocked`/`contested`) and one line of evidence — the test name that pins
it, or the screenshot/manual check that demonstrated it. When you claim an item done in
conversation, surface the evidence in the transcript (test output, screenshot, request
body): the goal evaluator can only see what you show it. Never mark blocked work done —
an honestly reported blocker counts in your favor; a faked item does not.

**Docs move with code** (golden rule 8): one new ADR (next free number) recording D1–D8 as
implemented, deviations included, with §3's rationale; README gains the `web/` dev
commands and the :8080 service; `CLAUDE.md`'s layout/commands sections gain `web/`;
compose comments updated (including D5's flag removal). Commit per slice, messages naming
the parity IDs landed.
