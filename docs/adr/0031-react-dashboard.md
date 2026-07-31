# 31. The dashboard as a React single-page application

Date: 2026-07-29

## Status

Accepted. Adds a second dashboard (`web/`, served on :8080) beside the Streamlit one
(:8501), which stays intact and runnable. Amends ADR-0008's *implementation* while keeping
its decision — the dashboard is a pure API client that never touches storage — and refines
ADR-0021 D1's UI gate. Changes no contract, adds no endpoint, and touches no adapter.

## Context

The Streamlit dashboard did its job: it made the pipeline visible from the first week and it
grew, without redesign, into three planes (Monitor, Operate, Evaluate) over a year's worth of
decisions. What it could not grow into is a product surface. Two costs had become structural.

**The execution model was the design.** Streamlit re-runs the whole script on every
interaction, so a third of `dashboard/app.py` was machinery defending invariants *against the
framework*: `st.cache_resource` so an HTTP client is not rebuilt per click (a measured 155 ms
of CA-bundle loading each time), `st.cache_data` so a benchmark does not re-run when the user
touches a selectbox, a latched run key so a finished report is not wiped by the first
interaction with its own timeline picker, session-state pruning so a detector chosen on one
corpus cannot survive into a corpus where it is ineligible. Every one of those protects a
real invariant; none of them is *about* the product.

**The visual language was not ours.** Streamlit's defaults are a demo aesthetic — wide
padding, framework-branded controls, no design tokens, no dark theme worth the name. An
operations console is read for hours at a stretch by an analyst who cares about density,
alignment and legibility, and none of those were reachable without fighting the framework.

The rewrite was specified in `docs/react-rewrite/brief.md` and implemented unattended in one
session. Its governing constraint was **parity, not redesign**: every capability of the old
app, item by item, with its microcopy carried across near-verbatim, because those captions
are where the product's explanations live (what `event_f1` means, why a cell is empty, what a
sweep costs). The parity ledger is `docs/react-rewrite/PARITY.md`; deviations and blockers
are `docs/react-rewrite/DEVIATIONS.md`.

## Decision

### D1 — Same origin via a reverse proxy; no CORS anywhere

Both FastAPI apps deliberately ship **no CORS**: `api/app.py`'s docstring records why, and it
was true — the Streamlit app called them server-to-server, so no browser cross-origin request
ever occurred in this architecture. A SPA moves those calls into the browser, which is
normally the moment a team adds `CORSMiddleware` and starts maintaining an origin allowlist.

We did not. The SPA is served by **nginx**, which also reverse-proxies `/api/*` to the read
API and `/pipeline/*` to the Pipeline API. One origin, so the documented stance survives
untouched, and the browser's same-origin policy keeps doing the work an allowlist would have
had to imitate. The bundle only ever requests **relative paths**, which has a second payoff:
per-environment API URLs vanish from the frontend entirely. `IOTSHIELD_API_URL` and
`IOTSHIELD_PIPELINE_API_URL` — the same two variables the old dashboard read — are now
consumed by the nginx config template, so the 12-factor property is preserved rather than
replaced by a build-time constant baked into a bundle.

Two proxy settings are requirements rather than defaults: a **64 MB body limit** (run inputs
travel inline as JSON, and a Zeek `conn.log` is an ordinary upload) and a **600 s read
timeout on `/pipeline/*`**. The old `ControlClient` allowed 120 s because model detectors and
benchmarks take real time; a proxy that gives up sooner than the work takes turns a slow
answer into a wrong one. Vite's dev server mirrors both routes, so dev and production agree
about paths and neither has an environment-specific base URL.

### D2 — Vite + React 18 + TypeScript (strict) + Tailwind, in a new top-level `web/`

TanStack Query for server state; no Redux, no CSS-in-JS runtime, no second UI kit. The UI
primitives live in `web/src/components/ui/` and we own the source, shadcn-style — but built on
**native** `<select>`, `<input>` and `<textarea>` rather than a headless listbox library. This
is a dense data-entry console, and the platform controls bring keyboard, mobile and assistive
behaviour for free. The dependency saved is real but secondary; the reason is that a form
which is the product surface should not reimplement a combobox.

### D3 — Charts as Vega-Lite specs, ported one-to-one from `charts.py`

`dashboard/charts.py` is not styling. It encodes validated dataviz decisions: single hue for a
magnitude comparison (identity rides on the axis, so a second hue adds noise, not
information), a **reserved** status red that means "our detector said something" and is never
a data series, ground truth as a deliberate neutral backdrop, UTC wall-clock feeding, a
visible floor for zero-length spans so point anomalies are not silently omitted, and
undefined scores dropped rather than drawn as zero.

Altair compiles to Vega-Lite, so the six builders port to TypeScript functions returning
specs, preserving each of those by construction. `web/src/charts/specs.ts` is close enough to
`charts.py` to diff. A restyle onto a native React charting library is a separate project that
would have to re-earn every one of those decisions; doing it inside a parity rewrite would
have hidden the losses.

### D4 — Types from OpenAPI, validation at the boundary

The old client parsed every response through the pydantic contracts, so API/UI drift failed
loudly at one boundary instead of rendering wrong (ADR-0008). TypeScript is erased at runtime
and cannot carry that alone, so the property is rebuilt from two halves that are both needed:

- `web/src/lib/api/*.gen.ts` is generated from **each app's own `/openapi.json`** (dumped
  in-process by `web/scripts/dump_openapi.py`, so a clean checkout can regenerate with
  nothing running) and committed. A schema change the client has not absorbed is a *compile*
  failure.
- `web/src/lib/api/schemas.ts` validates every response with zod, and every non-2xx, non-JSON
  or non-conforming reply becomes one typed `ApiError` that pages render as a friendly state.
  A *deployment* drifting from the bundle it is serving fails loudly at the same single
  boundary the Python client used.

Compile-time assertions tie the halves together: each schema is checked against the generated
server type it validates, so adding a contract field without teaching the client about it is a
build error rather than a silent strip. The closed vocabularies (lane, severity, status,
criticality) are zod enums for the same reason the pydantic ones were enums — an
out-of-vocabulary value is drift, not a badge with no colour.

### D5 — The Evaluate mode is gated by a **probe**, not by a flag

Deliberate divergence, and a simplification. The evaluation plane had two flags:
`IOTSHIELD_EVAL_API` decides whether the Pipeline API registers the `/eval` router, and
`IOTSHIELD_EVAL_UI` showed the Streamlit mode. A static bundle cannot read server env, so the
second flag is unreachable — but it was always the weaker of the two. Under ADR-0021 D1 the
router is **absent, not guarded**: without the API flag there is no route to reach, so the UI
flag could only ever hide a mode that already could not work.

So the SPA probes `GET /pipeline/eval/corpora` at startup and distinguishes two answers:

- **404** — the router was never registered. The deployment has no research plane; the mode
  is hidden, exactly as an unset `IOTSHIELD_EVAL_UI` did.
- **transport failure** — the plane is enabled but the Pipeline API is down or still starting.
  The mode stays visible and the page offers the saved-report fallback (D6), which is how the
  old page degraded rather than disappeared.

Consequence: one flag decides the plane end to end. `IOTSHIELD_EVAL_UI` still exists for the
Streamlit service (which needs it, and which stays runnable), and compose's comment now says
which dashboard each flag serves.

### D6 — The saved-report fallback becomes an upload

The old Evaluate page fell back to reading `reports/evaluation.json` from the **server's**
disk. A browser cannot, and inventing an endpoint to serve it would violate the no-new-
endpoints constraint and put arbitrary file reads on the control plane. Replacement: a file
picker — "load a saved report JSON" — whenever the eval API is unreachable. The CLI → file →
dashboard path keeps working, with the CLI command shown beside the picker, and a corrupt file
is an empty state rather than a traceback. Deliberate divergence.

### D7 — Coexistence, not replacement

New compose service `dashboard-web` on **:8080**; the Streamlit `dashboard` stays on :8501,
untouched, along with the root Python image and ports 8000/8100. Both are pure clients of the
same two APIs, so they can disagree about nothing that matters. Retiring Streamlit is a
separate, deliberate change — this ADR does not make it, and the parity ledger is what would
justify it.

### D8 — Vitest + React Testing Library + MSW, mocking at the network boundary

MSW is the moral equivalent of the old suite injecting FastAPI's `TestClient` into the typed
client: the app under test makes real `fetch` calls through its real client module and only
the wire is faked, so the fetch wrapper, the zod validation and the error mapping are all
exercised rather than stubbed past. The fixtures mirror `tests/dashboard/conftest.py`'s seeded
world (pump-02, T0, one detection, one HIGH alert, one malformed record), so the two suites
assert the same story and a divergence shows up as a disagreement rather than as two
independent green runs.

`npm run check` (typecheck + lint + test + build) is the gate, and `tests/dashboard/` stays
exactly as it was — the Streamlit app is still tested by the suite that always tested it.

## Consequences

**What is now true.** Every capability of the old dashboard exists in the new one, verified
item by item against `PARITY.md`, including the ones that are easy to lose because they fail
silently: only *touched* parameters reach a spec (a full prefilled block beside an artifact id
is the half-configured request ADR-0022 D4 refuses); detector preselection prefers one the
sources feed **and** that is ready to run, never whichever name sorts first; a committed bench
spec runs verbatim rather than as a recomposed lookalike; a sweep that stops being offered is
cleared **with a message**; a report on screen never silently mismatches the controls above
it. Every timestamp renders UTC by explicit construction, and the charts are fed UTC
wall-clock the way `_utc_wallclock` did.

**What the rewrite bought beyond parity.** Design tokens in one file, so changing the product's
look is changing a variable rather than hunting hex values; a dark theme that is the same ramp
inverted with the blue and red re-checked for contrast; and the removal of the framework
machinery described above — the React translations are one line each where Streamlit needed a
mechanism.

**What it cost.** A second toolchain (node) in the repository and a second image in compose,
plus ~1.2 MB of JavaScript, most of it Vega. Two constants are now duplicated across the
language boundary with a comment naming their source — `TIMELINE_SPAN_CAP` and the stub CSV
format — because neither is published on the wire and adding an endpoint to publish them was
out of scope. The sweep-value parser is a second implementation of `eval/sweep.py: values`,
pinned by tests, because sharing it across languages is not possible; that is the one place
where "the CLI and the dashboard cannot disagree" is now enforced by tests rather than by
construction.

**What full-stack verification caught that the mocked suite could not.** The SPA route
`/assets` collided with Vite's default build directory of the same name: nginx matched the
built directory first and issued a redirect to an address the browser could not reach, so the
page was unreachable in production and perfectly healthy in dev. The build directory is now
`static/`. It is a good argument for the last gate being a real browser against the real
stack, which is also where the screenshots in `docs/react-rewrite/screenshots/` come from.

**What is deferred.** Retiring the Streamlit dashboard (D7). A native-library chart restyle
(D3). The Playwright walkthrough is a screenshot tool, not an assertion suite; promoting it to
a smoke test in CI is the obvious next step and is not done here.

## Amendment (2026-07-30) — D7's deferral is taken, and the demo goes with it

Coexistence was always a staging decision, and it ended. `src/iotshield/dashboard/` and
`tests/dashboard/` are deleted, the `dashboard` compose service and :8501 are gone, and
`IOTSHIELD_EVAL_UI` went with them — it only ever gated that app's mode, which is what D5
already said made it the weaker of the two flags. `web/` is the only UI.

The parity ledger this ADR named as what would justify retirement (`docs/react-rewrite/`) was
deleted in the same change, along with `docs/design/` and `docs/integration/`. That is worth
stating plainly rather than quietly: the citations above no longer resolve, and this amendment
is now the record that the bar was cleared. The current-state description lives in
`docs/guide/`, which is where a reader should go; this ADR stays as written, per the
append-only rule.

**The thirteen `config/demo*.yaml` went at the same time, and for a reason that belongs here.**
They existed to exercise one adapter per ADR — a config per feature, which is an *index of the
codebase* rather than a demo suite, and by the end nobody could hold it in their head. What
made them removable is this ADR's own D1: the Compose page composes a `PipelineSpec` in the
browser and posts the input as uploaded content (`SourceInput.content`, never a server-side
path), so a run driven by clicking needs no YAML at all. `config/demo.yaml` survives as the
*walkthrough* rather than as an index — the same three inputs and the same chain the page is
clicked through — so the CLI and the compose `runner` stay exercised and print the queue the
dashboard prints. It registers no `assets:`, because the page cannot register any and two
demos of one scenario that disagree about the numbers are worse than one.

`data/demo/` is now generated by `scripts/make_demo_data.py` rather than hand-authored, so
"what exactly does this demo prove?" is answered by a module docstring instead of by a careful
reading of the bytes.

**What it cost.** D8's fixtures were shaped to mirror the Python suite so a divergence surfaced
as a disagreement rather than as two independent green runs; that second side is gone, so they
are now simply this suite's fixtures. And a chain can be perfectly compatible while naming an
input that was deleted — nothing checked that, which is how the compose `runner` shipped for
one commit pointing at two configs that no longer existed. `test_compat.py` now checks both
halves.
