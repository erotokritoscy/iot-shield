# The dashboard

Read `docs/guide/architecture.md` first, and `docs/guide/read-api.md` and
`docs/guide/pipeline-api.md` alongside this one: every page here is a client of one of those two
services, and what a page can show is bounded by what they publish.

`web/` is the product's user interface: a React single-page app served by nginx on `:8080`. This
guide is the product view of it. What it shows, why it is arranged that way, what it refuses to do,
and what you have to know before you change it.

It replaced a Streamlit app that has since been deleted. That app is why several decisions here
are phrased as translations of something, and ADR-0031 is where the comparison is recorded, but
there is no second dashboard in the tree any more. `web/` is the only UI.

## The one fact everything else follows from

**The dashboard computes nothing.**

It has no database connection, no detector, no scoring, no pipeline import, and no access to a
corpus file. Every number on every page arrived from one of two HTTP APIs, and every action it can
take is a request to one of them. Ask "where did this number come from?" and the answer is always an
endpoint, never a calculation.

That is not modesty about the frontend. It is what makes the whole product substitutable: the read
API is the same door a SIEM would come through, and a UI that computed its own severity would be
publishing a number no other client could reproduce. The rule has one deliberate exception, and it
is worth knowing because it looks like a violation: the Compose page reasons about *whether a
detector will be able to run*, from declarations the API publishes. It reads a published fact, it
does not re-derive server logic. The section on Compose says why that exception had to exist.

## Three planes, which is also the menu

The sidebar has three groups, and they are the three planes of the product rather than a
navigation scheme somebody chose:

| Group | Plane | Talks to |
|---|---|---|
| **Monitor** (Triage, Assets & telemetry, Data health) | read | the read API, `/api/*` |
| **Operate** (Compose & run) | write | the Pipeline API, `/pipeline/*` |
| **Evaluate** (Benchmarks) | evaluation | the Pipeline API's `/eval/*` router, or a local file |

Monitor browses what the pipeline produced. Operate composes a chain and triggers a run. Evaluate
*judges* the pipeline instead of using it. The grouping is the same one `architecture.md` describes,
and the sidebar is the first place a new operator meets it.

The routes are flat, one per view: `/`, `/assets`, `/data-health`, `/compose`, `/evaluate`, with
anything else redirecting to Triage. When the deployment requires login, a fourth group,
**Organization**, holds `/account` (everyone) and `/users` (admins), and `/login` and `/register`
sit outside the shell. A **super admin** (ADR-0040) with no home organization gets a different sidebar altogether —
*Platform → Organizations, Account* — because they see no organization's data: `TenantOnly`
redirects every data page to `/admin/organizations`. One *with* a home (the seeded
`admin@admin.com`, at home in UCY) gets the normal pages for that organization plus a Platform
group. `SuperAdminOnly` sends anyone else away from `/admin/*`, and the organization page
disables suspend/delete on the viewer's own home and shows super admins without role, reset or
remove controls. The organization page holds status (suspend/reactivate), users (role, reset
password, remove, add) and a delete that stays disabled until the organization's id is typed.

## Same origin, so no CORS anywhere

Both FastAPI apps deliberately ship no CORS middleware. That was easy to hold when the Streamlit
dashboard called them server to server, because no browser cross-origin request ever happened. A SPA
moves those calls into the browser, which is normally the moment a team adds `CORSMiddleware` and
starts maintaining an origin allowlist.

This one does not. nginx serves the bundle *and* reverse-proxies `/api/*` and `/pipeline/*` to the
two services, so there is one origin and the browser's same-origin policy keeps doing the work an
allowlist would have had to imitate.

The second payoff is the one you feel while working: **the bundle never names a host.** Every
request is a relative path. There is no `VITE_API_URL`, no build-time base URL, and no
per-environment bundle. `IOTSHIELD_API_URL` and `IOTSHIELD_PIPELINE_API_URL` are read by the nginx
config template at container start, so one image runs in every environment. In development the Vite
dev server proxies the same two prefixes to `127.0.0.1:8000` and `127.0.0.1:8100`, so dev and
production agree about paths.

Two proxy settings are requirements rather than defaults, and both exist because of what this
product does:

- `client_max_body_size 8m`, because run inputs travel inline as JSON and a Zeek `conn.log` is an
  ordinary upload. nginx's default of 1 MB would refuse it as a 413 with no useful message.
- `proxy_read_timeout 600s` on `/pipeline/*`, because model detectors and benchmarks take real time.
  A proxy that gives up sooner than the work takes turns a slow answer into a wrong one.

One more, less obvious: the built assets go to `static/`, not Vite's default `assets/`. `/assets` is
a client route (Assets & telemetry), and nginx matched the built directory first and redirected the
route away. The collision is invisible in development and fatal in production, so the build
directory was renamed to keep the route namespace the app's own.

## The one door into both APIs

Every request in the app goes through `lib/api/`, and it is worth understanding why that module is
as heavy as it is.

The old Python client parsed every response through the pydantic contracts, so a drift between API
and UI failed loudly at one boundary instead of rendering wrong. TypeScript types are erased at
runtime and cannot carry that property alone, so it is rebuilt from two halves that are both
necessary:

- **`*.gen.ts` is generated from each app's own `/openapi.json`** and committed. A contract change
  the client has not absorbed is a *compile* failure. Regenerate with `npm run gen:types`;
  `web/scripts/dump_openapi.py` dumps the schemas in-process, so a clean checkout can regenerate
  with nothing running.
- **`schemas.ts` validates every response with zod at runtime.** Compile-time assertions tie the
  halves together: each zod schema is checked against the generated server type it validates, so
  adding a contract field without teaching the client about it is a build error rather than a silent
  strip.

The generated types cover the *deployment you built against*. The zod pass covers the *deployment
you are actually talking to*, which is the case that bites in production: a dashboard image serving
an older bundle against a newer API.

Everything that can go wrong becomes one typed `ApiError` with four kinds:

| Kind | What happened |
|---|---|
| `transport` | nothing answered: the service is down, or DNS/proxy failed |
| `status` | it answered, with a non-2xx |
| `parse` | it answered 2xx with something that is not JSON |
| `contract` | it answered with JSON that no longer matches what this client was built against |

The pages render all four as a friendly state with a retry, never as a blank screen and never as a
stack trace. A FastAPI `detail` is unwrapped on the way through, in all three shapes it can take:
a plain string, a list of pydantic validation errors, and the structured `{message, issues}` the
Pipeline API raises for an incompatible chain. Surfacing the adapter's own carefully worded message
is the point. `min_count must be >= 2` reaching nobody is exactly the defect the API side fixed.

## Failure has a taxonomy, because the remedies differ

This is the part of the app with the most engineering per pixel, and it is all product reasoning.

**Unreachable is not the same as absent.** A 502 or 504 from nginx means the upstream is down. A 404
from a service that plainly answered means *that route was never registered*, which under the
evaluation plane's design is the deployment saying "this image has no research plane". The two are
kept deliberately disjoint in `http.ts` so they cannot drift into overlapping. Note the shape of the
deployment forces this: behind nginx a stopped upstream never reaches the browser as a transport
error, because the proxy is up and answers 502 with an HTML body. So both a true transport failure
and a 502/503/504 have to count as unreachable.

**Two failures, two remedies, on the Monitor gate.** Monitor's three views all read the same API, so
one health check up front beats three identical failures on the page. But `/health` runs `SELECT 1`,
so it proves *database* connectivity and not merely liveness, which means a perfectly healthy API in
front of a dead Postgres answers 500. Printing "nothing answered, start the API" at that moment
sends the operator to restart a service that is already running, and the command offered would
repoint the dashboard at an empty SQLite file. So the gate reads the kind of failure and names only
the cause it has evidence for:

```
Read API unreachable                 -> IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api
Read API cannot reach its database   -> docker compose up -d postgres && docker compose logs postgres
```

**Operate and Evaluate sit outside that gate on purpose.** Operate talks only to the Pipeline API
and reports its own reachability, and Evaluate degrades to a saved report. Composing a run while the
read plane is down is a legitimate thing to want.

**A single endpoint failing mid-page is a third case,** covered by `QueryState`, which wraps every
query on every page. Nothing in this app retries on its own, so without a retry button a single
transient failure is permanent until the operator guesses that a full page reload is the escape.

## The Evaluate mode is gated by a probe, not by a flag

The evaluation plane once had two server flags. `IOTSHIELD_EVAL_API` decides whether the Pipeline
API registers the `/eval` router at all; `IOTSHIELD_EVAL_UI` showed the mode in the Streamlit app.
A static bundle cannot read server environment variables, so the second flag was unreachable from
here, and it has been removed along with the app that read it.

It was also always the weaker of the two. Without the API flag the router is *absent*, not guarded,
so the UI flag could only ever hide a mode that already could not work. The SPA therefore **probes**
`GET /pipeline/eval/corpora` at startup and reads the answer:

- **404**: the router was never registered. Hide the mode entirely.
- **transport failure or 5xx-from-the-proxy**: the plane exists, the API is down or still starting.
  Keep the mode visible and offer the saved-report upload.
- **anything else that failed** (a 500, an auth proxy's 401, a zod contract failure): keep the mode
  visible and render the *real* error. This third state exists because folding it into
  "unreachable" printed a fabricated remedy, telling the operator to set a flag they had already
  set, while the actual error naming the drifting field was shown nowhere.

One flag now decides that plane end to end.

A detail worth copying if you add a probed feature: while the probe is in flight the sidebar renders
a fixed-height blank in the group's place. Branching on `loading` renders nothing either way, and
then the sidebar grows under the cursor when the answer lands.

## Login is probed for too

The same pattern decides whether there is a login (ADR-0039). `lib/auth.tsx` asks
`GET /pipeline/auth/me` once and reads the answer as a state, not an error:

- **404**: the `/auth` router is absent (`IOTSHIELD_AUTH` unset). No login; every page is open and
  the sidebar has no Organization group — the dashboard as it was before accounts.
- **401**: login required, no session. `RequireAuth` sends the browser to `/login`, remembering
  where it was going (only ever an in-app path — `safeRedirect`).
- **200**: the user and their organization, shown at the foot of the sidebar with **Sign out**.
- **unreachable**: let through. Monitor reads the read API, which enforces login itself, so a
  needed login still surfaces as the first 401 — while a down Pipeline API does not lock the read
  plane.

A session can end while a page is open (expiry, or an admin removed the user), so `http.ts`
reports any 401 from a non-`/auth` request to one handler, `SessionExpiryBridge`, which flips the
state to logged-out. Signing in, signing out and that flip all **clear the query cache** first:
queries are keyed by endpoint rather than by tenant, so a cache kept across users would show the
next person the previous organization's alerts.

The pages never see a token. The cookie is `HttpOnly`; the bundle only ever learns *whether* it is
still good, by asking.

## Monitor

### Triage

The queue, and the page the product exists for. A KPI row from `/summary`, a severity bar chart, the
ranked alert table, and an inspector for the selected alert.

The alert table is the read API's ordering, not the page's: alerts arrive priority-descending and
are rendered in the order they came. Sorting here would mean the UI had an opinion about ranking,
which is the alert engine's job.

The inspector is where an alert stops being a number:

- the **rationale**, the engine's own sentence;
- the **score components**, rendered as whatever keys the engine actually emitted rather than a
  fixed list, so a new factor shows up without a frontend change;
- the **recommended action**, rendered as a checklist, because it is a checklist flattened into one
  string on the wire and `lib/evidence.ts` is the documented inverse of the Python side's encoder;
- **every MITRE technique** cited by the evidence, which `threat_class` alone drops, since it keeps
  only the single strongest claim;
- the **evidence table** from `/alerts/{id}/detections`, one row per contributing detection, each
  expandable to its raw evidence payload.

One naming rule in that table is easy to get wrong and worth stating. It carries a **finding**
column, drawn from `evidence.explanation`, beside `method`. `method` was a good name for a finding
only by accident, back when one detector meant one kind of finding, and it stopped being one when a
single rule detector began reporting every rule on its lane under one name. The walkthrough's camera
alert carries four rule findings - a telnet probe and a 4.2 MB upload on flow, an sshd break-in
warning and a firmware-integrity CRITICAL on logs - which `method` renders as two rows reading
`rules:flow` and two reading `rules:log`, differing only in score. Naming each row by what it
found fixes that for every detector at once instead of special-casing one, and the same column is on
the Assets page for the same reason.

### Assets & telemetry

An asset picker, a caption of type / criticality / location, a telemetry timeline, and a detections
table.

Two decisions that look arbitrary and are not. The **chart is telemetry-only**, because it plots
telemetry values and overlaying a flow or log detection's window on a value series it does not
belong to would be misleading. The **table lists every lane**, so a flow-only or log-only asset is
not invisible here; before that split, such an asset showed "no telemetry" and nothing else while
its alert sat in Triage.

Auto-discovered assets say so in the picker. Discovery is visible, never implied.

### Data health

What was ingested and what could not be parsed. Malformed inputs are captured, never dropped, and
this page is where they surface with their raw text, the parse error, the source format and the
origin.

It also carries the rule the whole app follows about truncation: **a page that states a total and
shows a table in the same breath must say when the table is capped**, and which slice it is. An
unmarked cap reads as a contradiction the operator has to resolve alone.

### IT and OT overviews

The iotShield prototype's two dashboards, rebuilt over iotshield's own results (ADR-0044,
`pages/overview/`, charts in `charts/overview.ts`). All counting happens on the read API's
`/overview` routes, so the charts are complete whatever the range. Each page keeps its range of
UTC days in the URL (`?start=&end=`) and, when the range holds no data, offers to jump to the week
ending at the latest day that does.

- **IT overview**: flows, how many fall inside a flow-lane detection on the same device (the
  prototype's "attack vs normal", stated as what iotshield can claim), detections per day (every
  day drawn, an empty one at zero), detections per detector, and the flows table (capped at 500,
  saying so with the true total).
- **OT overview**: SensiPro's six anomaly types. Anomalies per day, or per hour when the range is
  one day; a pie and a table of the six with SensiPro's codes and where each is found (detector or
  ingest); the top twenty meters; and the records, filterable by type. Each type keeps one
  Okabe–Ito colour on every chart, with a legend, the table and an (i) beside it.
- **Meter page** (`/overview/ot/meter/:id`, from any meter link): one UTC day of its hourly
  consumption, one line per anomaly type over the range, and its records.
- **Replay** (all three pages): ▶ plays the range forward bucket by bucket (up to a week is drawn
  by hour, longer by day) — time charts grow across a fixed axis behind a dashed "now" line,
  counters, pies and per-meter bars show only what has arrived, and an "arriving now" feed lists the
  newest records. ⏸ pauses, ⟲ resets, 1×/2×/4× sets the pace (`lib/replay.ts`). It replays stored
  results without fetching anything, never starts by itself, and returns to the full range when done.

## Operate: Compose & run

A four-step **wizard**: *Data → Detection → Alerts & output → Review & run*. It is the write plane,
so it is also the only page in the app that changes anything. The wizard changes how the choices are
presented, never what is sent: all state lives once in `ComposePage.tsx`, `pages/compose/model.ts:
buildSpec` turns it into the same `PipelineSpec` the single-page form sent, and the same validation
and receipt follow. The current step rides in the URL (`?step=`), clamped to the first step whose
prerequisites are unmet; **Next** is disabled with the reason beside it (a source with no content,
no detector or an untrained one, no sink), and the stepper only lets you jump to reachable steps.
Detectors your sources can feed are listed first, and one nothing feeds is labelled so — offered,
not hidden, because a deliberately silent run is still a real measurement (ADR-0038 D1).

**Step 1 is a wizard of sources** (`pages/compose/SourceCard.tsx`). The sources are its steps —
Source 1, Source 2, … in a row with a status each (✓, *needs content*, *wrong format?*, *has an error*), then
**+ Add source** — and only the selected source is on screen, never stacked. That source is one
card with every field at once (sample, normalizer and format, parameters, upload or paste), and at
its foot the first five lines that will be read and the chain check's findings whose `stage` is
`sources[i]`. **Next: Source N** moves on, or **Add another source** on the last one. Moving between
sources never blocks **Next** — content does, as before — so the wizard changes how sources are
asked for, never what is sent.

**A file in the wrong format is caught on its source.** The chain check reads the spec's shape,
never the records, so each source is also read by its own normalizer through `POST /runs/preview`
(`compose/model.ts: useSourcePreviews`, debounced like validation and remembered per source and
content, so editing one source does not re-read the others). The verdict is the run's own
ADR-0017 reject rate, shown at the foot of the card with the first rejected lines by number:
*unreadable* (not one record read) is an error titled **Wrong format?**, marks the tab and **blocks
Next**, because such a run would produce only malformed records; *mostly unreadable* (half or
more) is a warning; a few rejects are a note that they will land on Data health; a clean read says
**Ready** with the count. A check that itself failed is a warning and blocks nothing.

**Every field has an (i).** Each label on steps 1–3 — source, detector, engine and sink fields,
and every adapter parameter — carries an (i) (`components/ui/info.tsx: InfoTip`) explaining what
it expects. It opens on hover or keyboard focus, a click pins it (so it works on a phone), and
Escape or a click elsewhere closes it; it sits beside the `<label>`, so the control's accessible
name is unchanged. The copy lives in one table, `pages/compose/help.ts`, because `/modules`
publishes a param's name, type, default and choices but no prose: the *meaning* of each param is
keyed there by adapter, and `paramHelp` appends the default (or "Optional."). A param the table has
not caught up with still gets an (i) stating its type — so adding a param to an adapter should add
a line there too. The Normalizer and format (i)s describe the selected format's wire shape. The
Evaluate page does not use it yet.

**Samples and scenarios.** `uv run python scripts/make_sample_data.py` writes a small library of
inputs to `web/public/samples/` (served at `/samples/` beside the bundle — static files, so no new
endpoint) and an identical copy to `data/samples/` for the CLI, plus `manifest.json`. Every source
card offers *Use a sample file*, which sets the sample's normalizer, format, content and filename
together. Step 1 also offers **scenarios** — each card shows its title only, with the description and what it
holds (sources, detectors, expected alerts) behind an (i) — that fill every step and land on Review, which then shows
what the scenario is *tested* to produce and, after the run, whether it did — but only while the
spec and inputs are still exactly the scenario's; change anything and the promise is withdrawn.

| Scenario | Inputs | Expected |
|---|---|---|
| Compromised camera | the three `data/demo/` files | 110 events, 2 malformed, 8 detections, 2 alerts |
| Port scan | `portscan_flow.log` | 1 detection (`flow_burst:port_scan`), 1 alert |
| SSH brute force | `bruteforce_syslog.log` | 1 detection (`log_burst:auth_failure`), 1 alert |
| Overheating pump | `pump_telemetry.csv` | 4 detections (`stub_threshold`), 1 alert |
| Quiet network (control) | three quiet files, every detector on | 0 detections — any is a false alarm |
| Broken input | `broken_syslog.log` | 6 events, 4 malformed (see Data health) |

Those numbers are not captions: `tests/test_sample_data.py` runs every scenario through the real
pipeline and asserts them, and holds both copies of the files to what the generator writes.

**Live chain validation.** The assembled spec is sent to `/runs/validate` on every change, debounced
350 ms, and the guard's issues render inline as errors or warnings with the stage that raised them.
That debounce creates a window where the verdict on screen describes a spec that no longer exists,
so during it the previous verdict is **withheld rather than dimmed**, and Run is disabled. A dimmed
green badge is still a green badge to anyone scanning the page.

**What the guard cannot see, and why the page says so itself.** The guard's defining property is
that it reasons about a spec *without constructing adapters*. "Is this detector fitted?" is only
knowable by constructing it. So a chain naming an untrained learning detector validates clean, and
the refusal arrives from the runner after the operator has pasted their data. The page closes that
gap with its own warning, from the `artifact_param` the module catalogue publishes:

> **meter_iforest** has no fitted artifact, so the run will be refused before it judges anything.
> Name one in its parameters, or choose a detector that is ready to run. The chain check above
> cannot see this - it never builds the detector.

That warning is the exception to "the dashboard computes nothing", and its boundaries are worth
noting: it reads a published declaration, and it reads the *widget* value rather than the submitted
spec, because the submitted spec omits a parameter still sitting at its catalogue default. Judging
the spec would warn about a chain that runs perfectly well.

**Nothing preselects by sort order.** Preselection walks a list of predicates in order of preference:
a source prefers the normalizer that declares the reference telemetry format, a detector prefers one
that both accepts what the sources produce and is ready to run. "Whichever name sorts first" was
silently wrong the moment a second lane arrived, and wrong again when `flow` was registered and
sorted ahead of `stub`, so a fresh session opened on a flow chain fed telemetry CSV.

**A run produces a receipt, not a results view.** Events, malformed, detections, alerts, plus any
warnings and the sinks actually written. If the postgres sink was among them and there are alerts,
the page hands off to Triage; if it was not, it says so, because Triage reads the postgres store
only and would otherwise look empty after a successful run.

**This page is where the demo lives.** The thirteen `config/demo*.yaml` that indexed the adapters
one at a time are gone; `config/demo.yaml` survives as the same walkthrough without a browser,
which is what keeps the CLI and the compose `runner` exercised on the same bytes. The demo proper
is two runs composed here, differing only in how many lanes they judge.
`uv run python scripts/make_demo_data.py` writes the three inputs - one compromised IP camera on
all three lanes, a gateway being administered from outside, and a sensor that behaves. Upload
`camera_flow.log` alone with `rules` on the flow lane, and the receipt reads 12 events, 0
malformed, 3 detections, 2 alerts, with the queue:

```
[HIGH]   10.0.0.24   corroboration 1.00   2 detection(s) across 1 lane(s)
[MEDIUM] 10.0.0.11   corroboration 1.00   1 detection(s) across 1 lane(s)
```

Add `camera_syslog.log` and `camera_telemetry.csv`, with `rules` on the log lane and
`stub_threshold` on telemetry. 110 events, 2 malformed, 8 detections, still 2 alerts:

```
[CRITICAL] 10.0.0.24   corroboration 2.00   7 detection(s) across 3 lane(s)
[MEDIUM]   10.0.0.11   corroboration 1.00   1 detection(s) across 1 lane(s)
```

The camera moves from HIGH to CRITICAL and the gateway does not move at all, which is the
whole claim: a priority that rose everywhere would only show that scoring had changed. The two
malformed syslog lines are deliberate, so Data health has something real to show rather than an
empty table.

**Upload limits are enforced in three places for three different reasons,** and this is the one part
of the page that is currently inconsistent. The server bounds one source at 4,000,000 characters,
nginx bounds the whole request at 8 MB, and the browser bounds one uploaded file at 8 MB. The
browser's limit exists because `JSON.stringify` over tens of megabytes locks the tab long before the
proxy would answer. See "Known drift" below.

## Operate: Data sources

`/data-sources` (`pages/DataSourcesPage.tsx`) is the organization's register of where its data
comes from — iotShield's IT/OT data sources, carried over (ADR-0041). The list shows each source's
type, name, how it is read (`normalizer · format`, or *not set*) and description. Admins (and
everyone, with no login) get an add/edit form with an (i) on every field and Edit / Remove per row;
a member sees the list and a note that only an admin can change it. The form asks for an input
format only when the chosen normalizer reads more than one, mirroring the server's check.

On Compose, the source card shows a **Data source** picker once the organization has any. Picking
one fills in the normalizer and format it names (or, for a source that names none, says so and
leaves them); choosing another normalizer by hand clears the pick. Nothing about the pick is sent
with the run.

A source with a **connection** (ADR-0042) also gets a **Pull** panel (`compose/PullPanel.tsx`):
from/to dates, the connector's own params (SensiPro: utility), and a filterable device list
(meters or hosts; none selected means all of them). **Pull** asks the server for that window, and
the answer fills the source exactly as an uploaded file would: its content, its origin
(`SensiPro meters 2023-07-01..2023-07-02`) and the connector's normalizer and format. The
wrong-format check and the run see no difference. A refused pull (too many days or rows) says why
and leaves the source as it was. On the Data sources page, an admin picks the connection; the
normalizer fields then disappear, because the connector decides how the source is read.

## Evaluate: Benchmarks

Two modes, and the distinction between them is the point of the page.

**Run a committed bench spec** is the reproducibility mode. `/eval/specs` publishes each
`config/bench/*.yaml` already parsed, and the page forwards the spec object **untouched**. It never
names a path and it never edits what it was given, so a number on this page and the same number from
the CLI are the same measurement. This is a fix, not a design flourish: composing a lookalike from
the same detector name silently dropped the `assets:` block and every source and engine parameter,
and the file and the reconstruction published different priority scores with nothing on either
surface disclosing which had run.

Only specs whose source shape matches the chosen corpus are offered, which is the pairing the server
derives. Offering the rest would put a guaranteed alignment failure in a dropdown.

**Compose my own** builds specs in the page, with a sweep box for a hyperparameter and an explicit
choice between using an artifact's trained weights and fitting from scratch. Those are two exclusive
modes rather than one form, because an artifact is atomic: its weights are aligned to the features
its own hyperparameters produced, so editing one knob while keeping the weights is refused at
construction. Offering the modes separately means the UI never builds that request.

The fit-protocol radio offers `none` and `prefix` with their help text. There is deliberately no
`holdout`, and the list is keyed off the page's own table rather than the server's enum, so a
protocol added server-side and not here is simply not offered. That is the safe direction.

Two behaviours that are easy to miss:

**A finished report survives a look at Triage.** A benchmark runs for minutes, and the key to its
cached result is held above the routes rather than in the page's state, so navigating away and back
does not discard a report still sitting in memory.

**"Refresh data" cannot re-run a benchmark.** The refresh button invalidates the query cache, and
invalidation refetches *active* queries regardless of caching policy, so a naive sweep would silently
re-run whatever benchmark is on screen: minutes of server work from a button labelled "Refresh
data". The benchmark result key is the one exclusion, expressed as that single exclusion rather than
as an allowlist of what is safe.

**The saved-report fallback is an upload.** The old page read `reports/evaluation.json` off the
server's disk. A browser cannot, and adding an endpoint to serve it would put arbitrary file reads on
the control plane. So the page offers a file picker with the CLI command shown beside it, keeping the
CLI to file to dashboard path working with no new endpoint. A corrupt file is an empty state, and a
report from an older harness is told so specifically rather than described as malformed.

## Charts are ported specs, not styling

`web/src/charts/specs.ts` holds six builders: the telemetry timeline, the severity bar, benchmark
spec scores, the sweep curve, per-case variance, and one case's timeline. They are Vega-Lite specs
ported one-to-one from the Streamlit app's chart module, which is why they are grouped in one file
rather than living beside the pages that draw them.

That file is not styling. It encodes decisions that have to survive any restyle:

- a **single hue** for a magnitude comparison, because identity rides on the axis and a second hue
  adds noise rather than information;
- a **reserved status red** that means "our detector said something" and is never a data series;
- ground truth as a deliberate **neutral backdrop**;
- **UTC wall-clock feeding**;
- a **visible floor for zero-length spans**, so a point anomaly is not silently omitted;
- **undefined dropped rather than drawn as zero**.

Every builder returns `null` for "there is nothing to draw", so the caller renders its empty state
instead of an axis with no marks.

UTC needs two separate mechanisms in Vega-Lite and this is a standing trap: `scale: {type: "utc"}`
fixes the axis ticks and nothing else, while tooltips are formatted independently and come out in
the viewer's zone unless `timeUnit: "utc..."` carries UTC into the field. A tooltip is a rendered
timestamp, so both are required.

## Rules the whole app keeps

**Every timestamp is UTC, engineered rather than hoped for.** `Date#toLocaleString` and friends
render in the viewer's zone, so a dashboard opened in Nicosia would show 15:00 for the same instant
the API called 12:00. `formatUtc` is the only formatter, the sidebar says "All timestamps UTC", and
the chart axes and tooltips are pinned by tests.

**Undefined renders as `-`, never as a fabricated 0.** A missing number and a zero are different
claims about the world, and the evaluation report depends on the distinction.

**Selection lives in the URL.** In a router-based app the URL *is* the view state, so anything kept
outside it is state the user can lose without being told. Leaving Triage and coming back used to
reset the inspector to the top of the queue, and no alert was linkable into a ticket. The selection
is written with `replace: true`, which is the load-bearing part: without it every click is a history
entry, so Back walks the inspector one alert at a time instead of leaving the page.

**Nothing polls, and nothing refetches on focus.** A queue that re-ranks under the cursor is worse
than a stale one. That makes the explicit refresh button the only way to ask for current data
without reloading, which is why it exists.

**Colour never carries identity alone.** A severity badge always shows its text; colour supplements
it. The same rule the charts follow.

**Tables that are driven by the data stay driven by the data.** The evaluation report's head-to-head
and per-case tables render whatever keys the report carries, in the order the API serialized them.
Fixing a column list would silently hide a metric the harness later adds, which is the one thing
those two tables must not do. Same for an alert's score components.

**A row that does something is still a table row.** The clickable control is a real `<button>` in
the first cell, not `role="button"` on the `<tr>`, which would sever the row-to-cell relationships
assistive technology navigates by.

## Look and feel

Every colour in the app is a design token in `web/src/index.css`, consumed through the Tailwind
config. A component never names a hex value, so a restyle is one file.

The ramp is the warm gray the charts already used (paper `#fcfcfb`, grid `#e1e0d9`, muted `#898781`),
the accent is their series blue and the status red is their reserved critical, so a chart and the
page around it cannot drift apart. Dark theme is the same ramp inverted with the blue and red
**re-chosen** rather than reused, because the light-theme blue on near-black falls under WCAG AA, and
a legible chart in both themes was a completion requirement.

The UI primitives in `components/ui/` are owned source in shadcn's style (CVA variants, `cn`,
token-driven classes) but built on **native** `<select>`, `<input>` and `<textarea>` rather than a
headless component library. This is a dense data-entry console, and the platform controls bring
keyboard, mobile and assistive behaviour for free. If a control ever genuinely needs a headless
primitive, adding one then is a small local change.

## Running it

```
cd web
npm install          # node >= 20
npm run dev          # :5173, proxying /api -> :8000 and /pipeline -> :8100
npm run check        # the gate: typecheck + lint + vitest + build
npm run gen:types    # regenerate the TS view of both APIs after changing either one
```

`npm run check` is to `web/` what `scripts/check.sh` is to the Python side, and CI runs it as its own
job. As of this writing it is green at **125 tests across 10 files**, with the production build
transforming 2,539 modules.

In compose the service is `dashboard-web` on `127.0.0.1:8080`, built from `web/Dockerfile`: node
builds the bundle, nginx serves it. Deliberately its own image rather than an addition to the Python
one, since the two have nothing in common at runtime.

Tests mock at the **network boundary** with MSW rather than stubbing the client, so the fetch
wrapper, the zod validation and the error mapping are all exercised rather than skipped past. The
fixtures still encode the seeded world the Python dashboard suite asserted against (`pump-02`, one
detection, one HIGH alert, one malformed record). They were shaped so the two suites told the same
story and a divergence surfaced as a disagreement rather than as two independent green runs; that
suite went with the Streamlit app, so they are now simply this suite's fixtures.

One test-config note that will look strange: `fileParallelism: false`. Parallel workers race on
Vite's transform cache under Windows file locking, which surfaced as whole test *files* being
silently dropped from the run. A green suite that quietly skipped two files is worse than a slower
one.

## Extending it

**Absorbing an API change.** Change the contract, then `npm run gen:types`, then fix what stops
compiling. If the response is one the app validates, extend its zod schema too, and the compile-time
assertion tying schema to generated type will tell you if you missed a field. This is the intended
order and it is why drift is a build failure rather than a support ticket.

**A new read view.** Add the query to `lib/queries.ts` with its key in the `keys` table (keys defined
outside that table are the ones a scoped invalidation silently misses), add a route in `App.tsx`, and
put the page behind `MonitorGate` if it reads the read API. Wrap each query in `QueryState` and you
inherit the loading, error and retry behaviour.

**A new chart.** Add a builder to `charts/specs.ts` returning a spec or `null`, feed it through
`utcInstant`, and unit-test it by inspecting the spec. The existing tests assert the decisions
(the zero-span floor, the fixed severity order, one hue, UTC feeding) rather than snapshotting
output, which is what keeps them meaningful after a restyle.

**A new adapter needs no dashboard change at all.** This is worth stating plainly, because it is the
main reason the Compose page is built the way it is. Detectors, normalizers, engines and sinks are
rendered from `/modules`: their parameters, their types, their defaults, their closed vocabularies
and their discovered vocabularies all arrive as data. Register an adapter with a params model and it
appears in the page, with the right widgets, with no frontend commit. The corollary is that anything
the page needs to know must be *published* rather than inferred, which is why `artifact_param` and
the discovered-id vocabularies exist on the wire at all.

## Known drift

Recorded rather than fixed, because this guide is documentation and these are code changes:

- **Three body-size limits that no longer agree.** `MAX_SOURCE_CHARS` is 4,000,000 characters per
  source server-side; nginx allows 8 MB for the whole request; the browser allows 8 MB per uploaded
  file. So a 6 MB single source passes the browser and is refused by the server, and three 3 MB
  sources pass the browser and are refused by the proxy. `src/iotshield/api/control.py` explicitly
  asks that these be kept in agreement.
- **Two stale references to a 64 MB proxy limit**, in `ComposePage.tsx`'s comment on
  `MAX_UPLOAD_BYTES` and in ADR-0031 D1. The template is 8 MB and carries the reasoning that
  supersedes both.
- **`X-Total-Count` is described but never declared.** The read API sets the header on every list
  and its `offset` parameter tells you to read it, but that instruction lives in a description
  string rather than in a `responses.headers` block, so the generated types carry no header type
  and `http.ts` never reads response headers at all. The client therefore re-derives "is this
  capped?" with a `limit + 1` probe, which yields a boolean where the header already carries an
  exact count.
- **The Compose page cannot register assets.** `PipelineSpec.assets` exists and the alert engine
  reads `Asset.criticality` as its `asset_weight` factor, but the page has no section for it, so a
  run driven entirely from here auto-discovers every device at MEDIUM. The walkthrough is pinned
  at what this page produces for exactly that reason: `tests/pipeline/test_runner.py::
  TestWalkthroughDemo` registers nothing and reads 52.7 then 80.0. Declaring the camera HIGH from
  YAML weights it 1.20 instead of 1.10 and reads 55.7 then 81.5 - a real ranking factor the UI
  cannot reach, so the first of those two is pinned separately. The severity bands are identical
  either way (HIGH, then CRITICAL), so nothing the walkthrough claims rests on it - and pinning
  the page's numbers rather than YAML's is what stops a figure on screen disagreeing with a figure
  in the docs.
- **Alert status is read-only.** Every alert is `new`, because no endpoint mutates one. Adding
  acknowledge or dismiss is where the read plane stops being read-only, and it is a read-API change
  before it is a dashboard change.

## Where to look

| | |
|---|---|
| Routes and the query client | `web/src/App.tsx` |
| The sidebar, the planes, the refresh exclusion | `web/src/components/AppShell.tsx` |
| The one door: transport, status, contract errors | `web/src/lib/api/http.ts` |
| Typed request wrappers per endpoint | `web/src/lib/api/client.ts` |
| Runtime validation, and its ties to the generated types | `web/src/lib/api/schemas.ts` |
| Generated from each app's `/openapi.json` | `web/src/lib/api/*.gen.ts` |
| Server state, the key table, the eval probe | `web/src/lib/queries.ts` |
| The read-side health gate and its two remedies | `web/src/pages/MonitorGate.tsx` |
| The in-page failure boundary | `web/src/components/QueryState.tsx` |
| The queue and the alert inspector | `web/src/pages/TriagePage.tsx` |
| Enrichment's read path in TypeScript | `web/src/lib/evidence.ts` |
| Compose: preselection, live validation, the untrained warning | `web/src/pages/ComposePage.tsx` |
| Compose: the (i) text behind every field | `web/src/pages/compose/help.ts` |
| Data sources: the IT/OT register and its form | `web/src/pages/DataSourcesPage.tsx`, `src/iotshield/api/data_sources.py` |
| IT/OT overviews: the pages, the chart builders, the counting | `web/src/pages/overview/`, `web/src/charts/overview.ts`, `src/iotshield/storage/overview.py` |
| Pulling from a source database: connectors and the Pull panel | `src/iotshield/connect/`, `web/src/pages/compose/PullPanel.tsx`, `config/connections.yaml` |
| The walkthrough's three inputs, and the scenario they encode | `scripts/make_demo_data.py` |
| Artifact vs hyperparameters, shared by both pages that choose weights | `web/src/lib/artifact.ts` |
| Parameter widgets from published declarations | `web/src/lib/params.ts` |
| The two Evaluate modes | `web/src/pages/evaluate/` |
| The six chart builders | `web/src/charts/specs.ts` |
| Design tokens, light and dark | `web/src/index.css` |
| Same-origin serving and the two proxy requirements | `web/nginx.conf.template` |
| Dev proxy, build output, test runner config | `web/vite.config.ts` |
| The rewrite's decisions, and what was done differently | `docs/adr/0031-react-dashboard.md` |
| Compose offers an artifact, not hyperparameters | `docs/adr/0036-a-judge-only-page-offers-no-hyperparameters.md` |
