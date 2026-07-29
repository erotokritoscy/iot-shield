# iotshield MVP

Containerized integration of the iotshield.ai modular detection pipeline:
IT/OT input → normalization → per-modality anomaly detection → correlation &
alert prioritization → PostgreSQL → FastAPI → dashboard.

CYENS Centre of Excellence internship project (July 2026), integrating existing
graduate-student modules as swappable adapters behind frozen data contracts.

## Quick start (compose)

The whole stack on real Postgres, reproducibly, in two commands:

```
docker compose up -d           # postgres -> init -> read API + Pipeline API -> dashboards
docker compose run --rm runner # run the demo pipeline to populate the database
```

Then open the dashboard at http://localhost:8080, the read API at
http://localhost:8000/docs, and the write-side **Pipeline API** at
http://localhost:8100/docs (compose a pipeline and trigger runs — also driven from
the dashboard's "Compose & run" page). The original Streamlit dashboard is still
served at http://localhost:8501 during the changeover (ADR-0031): both are pure
clients of the same two APIs. Services find each other by name on the compose
network (the APIs reach Postgres at `postgres`, the dashboards reach them at `api`
and `pipeline-api`); credentials come from `.env` (copy `.env.example`), with
defaults that run out of the box. `docker compose down -v` tears it all down,
volume included.

The `runner` is a job, not a long-lived service — it runs once and exits, so it
sits behind the `jobs` profile and is invoked with `docker compose run`, not
`up`. Point it at other data by overriding the command (the service has no
entrypoint, so pass the whole command, not just flags): `docker compose run --rm
runner python -m iotshield.pipeline --input=data/demo/demo_team3.csv
--config=config/demo_team3.yaml`.

## Quick start (dev, no Docker)

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

A second demo swaps in the `syslog` normalizer (re-implemented from team4,
ADR-0011) — logs instead of telemetry:

```
uv run python -m iotshield.pipeline --input data/demo/demo_syslog.log --config config/demo_syslog.yaml
```

Syslog records land in the LOG lane, so this run's visible results are events and
captured malformed lines, not detections (the telemetry detector ignores them).

A third demo swaps in the `team3_zscore` detector (a robust z-score model vendored
from team3, ADR-0012) over a real SKAB valve fault — the detector lane's payoff:

```
uv run python -m iotshield.pipeline --input data/demo/demo_team3.csv --config config/demo_team3.yaml
```

The input is telemetry that ramps from normal into a sustained valve anomaly, so
this run produces a real detection (one collective anomaly episode) and a
prioritized alert on the `skab-testbed` asset.

The alert engine is swappable the same way. `config/demo_weighted.yaml` runs the
same detections through the `weighted` engine (team2-derived multiplicative,
explainable scoring — ADR-0013) instead of the stub:

```
uv run python -m iotshield.pipeline --input data/demo/demo.csv --config config/demo_weighted.yaml
```

Same detections, richer prioritization and per-factor rationale — compare its
alerts against the stub engine's from `config/demo.yaml`.

Finally, `config/demo_triage.yaml` puts it all together — **both** detectors
(`team3_zscore` and `stub_threshold`) feeding the `weighted` engine over four
assets:

```
uv run python -m iotshield.pipeline --input data/demo/demo_triage.csv --config config/demo_triage.yaml
```

The result is a *ranked* triage queue, not a single alert: the real SKAB anomaly
tops it (saturated severity), two pumps with an identical threshold breach split
across the HIGH/MEDIUM line purely by asset criticality, and a fourth is escalated
by repeated (volume) detections — the three single-lane priority factors, each
visible in the ordering.

### Network flows: the third lane

`config/demo_flow.yaml` reads a real-shaped Zeek `conn.log` (ADR-0023 / ADR-0024):

```
uv run python -m iotshield.pipeline --config config/demo_flow.yaml
```

A *flow record* is the summary of one network conversation — who talked to whom, on
which port, how much — which is what routers and network sensors actually export.
Here an IP camera whose normal world is an MQTT broker and an NTP server suddenly
contacts 41 distinct hosts on port 23, unanswered: the Mirai propagation signature.
The `fan_out` preset counts *distinct destinations* per sliding minute, so the sweep
becomes one detection rather than 41. A gateway sending 30 flows to a *single*
destination in the same window stays silent — cardinality is the signal, not volume.

This lane earns its place by seeing what the others cannot: the camera's telemetry is
normal throughout and it writes no log lines about being compromised.

### All three lanes at once

```
uv run python -m iotshield.pipeline --config config/demo_trimodal.yaml
```

Telemetry, syslog and flow for the same camera in one run. The alert engine raises an
alert's priority when independent lanes agree (`corroboration = 1 + 0.5 x (lanes - 1)`),
so three judged lanes reach 2.0 — the highest the factor can express. The identical
flow sweep scores **44.1 / MEDIUM on its own** and **76.4 / CRITICAL corroborated**:
same evidence, different confidence.

Note that flows identify a device by IP (that is the only identity a flow carries), so
this demo keys all three sources on the camera's IP address. Reconciling hostname-keyed
and IP-keyed identities in general needs an asset-aliasing layer that does not exist yet.

### Named threats: the rule detector

Every detector above answers *is this unusual?* — a threshold, a learned baseline, a rate.
The rule detector answers the other question a security tool has to answer: *is this a
known-bad thing?* (ADR-0032)

```
uv run python -m iotshield.pipeline --config config/demo_rules.yaml
```

Watch what does **not** happen: `flow_burst:fan_out` is configured in this run and finds
nothing. The camera probes six hosts on telnet — fan-out's boundary is twenty — and uploads
4.2 MB, where exfiltration's boundary is 50 MB. Both are invisible to rate detection and
obvious to a signature, because a camera dialling port 23 has no legitimate reason to do so
even once. Waiting for twenty is waiting for confirmation of something already certain.

The other half is what an analyst reads. Until this, every alert this system produced ended
with `threat class: unknown` and an instruction that restated the alert. Here the camera's
four detections across two lanes become one **73.9 / CRITICAL** alert reading *"Remote
Services (T1021)"* with real next steps — *"Isolate the device and inspect its firmware for
compromise…"* — written by a security practitioner rather than generated by us. When several
findings correlate, the most specific claim names the alert and the actions from all of them
are combined.

The rules are YAML, and the detection knowledge in them is
[Antonia Voutouri's](https://github.com/AntoniaVoutouri/iot-ot-alert-correlation), ported
with her MITRE mappings intact. **Adding a detection is writing a file** — no Python, no
rebuild — which makes this the first part of the pipeline a domain expert can extend
directly. The field list a rule may address is published from the code that implements it:

```
uv run python -m iotshield.detect.rules
```

`docs/integration/rule-authoring-guide.md` is the full recipe.

### Already-normalized input: the pass-through normalizer

Not every source needs parsing. Some upstream systems already emit our canonical event
shape. `config/demo_passthrough.yaml` reads JSONL from a partner that does (ADR-0025):

```
uv run python -m iotshield.pipeline --config config/demo_passthrough.yaml
```

The `passthrough` normalizer **validates and envelopes** rather than parses — every record
is still checked against the contract (timezone-aware timestamps, finite values, no unknown
fields) and every failure is still quarantined, never dropped. Here 7 events flow through
and 3 lines are captured as malformed: a naive timestamp, a record that declares the wrong
lane for this source, and a non-JSON line the exporter left in the stream. It is the
smallest a working normalizer can be — the hello-world for adding your own module.

One design point it introduces: because a pass-through record carries its own lane in the
data (which the compatibility guard cannot see statically), the lane a source carries is a
config `param`. That keeps the guard's coverage feedback accurate and lets the adapter
quarantine a misrouted record — the first normalizer whose declared shape follows its params.

## Serve the API

```
$env:IOTSHIELD_DATABASE_URL = "sqlite:///demo.db"   # or the Postgres URL
uv run python -m iotshield.api
```

Read-side endpoints over whatever the pipeline stored: `/health`, `/assets`,
`/events`, `/detections`, `/alerts`, `/alerts/{id}/detections`, `/malformed`,
`/summary` — interactive docs at `/docs`. Responses are the data contracts,
serialized as-is.

## Serve the Pipeline API (compose & run)

```
$env:IOTSHIELD_DATABASE_URL = "sqlite:///demo.db"   # same database as the read API
uv run python -m iotshield.api.control
```

The write side (ADR-0015), on port 8100 by default. `GET /modules` lists the
available adapters with their capabilities and parameters; `POST /runs/validate`
checks a composed chain without running it; `POST /runs` validates, runs, and
returns a summary (results persist — read them back from the read API). Interactive
docs at `/docs`. It's a separate service from the read API so the read side stays
write-free and independently scalable.

## Open the dashboard

The dashboard is a React single-page app in `web/`, served by nginx in compose (ADR-0031).
For development, with both APIs already running on their default ports:

```
cd web
npm install
npm run dev      # http://localhost:5173, proxying /api and /pipeline to the two APIs
npm run check    # typecheck + lint + test + build — run before considering a change done
npm run gen:types  # regenerate the TS view of both APIs after changing either one
```

The dev server proxies the same two paths nginx does, so the app only ever calls relative
URLs and there is no per-environment API base to configure. Point the proxy elsewhere with
`IOTSHIELD_API_URL` / `IOTSHIELD_PIPELINE_API_URL`, the same variables compose feeds nginx.

A left sidebar groups the views by plane. **Monitor** holds Triage (ranked alert queue +
drill-down to each detection's evidence), Assets & telemetry (per-asset timeline plus an
all-lane detections table), and Data health (counts + the malformed-input audit);
**Operate** is Compose & run (pick modules, validate the chain, trigger a run against the
Pipeline API — results then appear under Triage); **Evaluate** runs benchmarks and
hyperparameter sweeps, and appears only when the deployment serves them (see below).

The original Streamlit dashboard is unchanged and still runnable:

```
$env:IOTSHIELD_API_URL = "http://127.0.0.1:8000"            # read API (default)
$env:IOTSHIELD_PIPELINE_API_URL = "http://127.0.0.1:8100"   # Pipeline API (default)
uv run streamlit run src/iotshield/dashboard/streamlit_app.py
```

## Benchmark detectors against labelled data

The port suite tells you whether an adapter **fits**; the evaluation harness tells you
whether it is **good** (ADR-0021):

```
uv run pytest                                          # does my adapter FIT?
uv run python -m iotshield.eval --corpus synthetic --spec config/bench/stub.yaml
```

```
corpus: synthetic - Synthetic rig telemetry: 11 recordings, 9 injected faults
  11 case(s), 9 labelled incident(s)   fit protocol: none

case                      inc   det  alerts    evF1  det/inc  alt/inc   latency
spike-0                     1    62       1    1.00    62.00     1.00       28s
drift-0                     1    53       1    1.00    53.00     1.00       53s
stuck-0                     1     0       0    0.00     0.00     0.00         -
clean-0                     0     0       0       -        -        -         -
```

Three things that table is showing. **`stuck` is invisible** to a fixed threshold — the
sensor freezes at an in-range value, so only a detector that models *normal* can see it;
that is the shape the corpus exists to discriminate on. **`det/inc 62`** is fragmentation:
one fault became 62 detections, which the alert layer compressed to 1. And **`-` means
undefined, not zero** — a clean recording has no fault to recall.

The corpus-wide summary above it is **pooled from counts, not averaged from these rows**: a
rate is undefined on a clean recording, so averaging would let a spec that false-alarmed on
`clean-0` score exactly as well as one that stayed quiet (ADR-0027). Its `ovl` column is the
one interval matching cannot see — of a true detection's window, how much was actually fault.
A single detection spanning the whole recording scores perfect precision, perfect recall and
zero latency; `ovl` is what says it carried no information.

### Corpora

```
uv run python -m iotshield.eval list        # what is here
uv run python -m iotshield.eval generate    # rebuild every committed corpus
uv run python -m iotshield.eval fetch skab  # convert a SKAB checkout into local bundles
```

A corpus is a **bundle** in `data/eval/` — a manifest, gzipped readings, per-asset interval
labels — and they are *discovered*, so **adding your own is dropping in a directory, not
writing code**. Three ship in this repository, so a fresh clone can benchmark immediately:
`synthetic` (telemetry), `fleet_scan` (multi-device network flow) and `device_trimodal` (one
device on all three lanes). SKAB is GPLv3 and therefore converted locally rather than
redistributed.

**A finding is judged against the incident on its own device**, never on time alone
(ADR-0026). That is what lets a fleet corpus measure the thing worth measuring — not whether
the scanner was caught, but whether the rest of the fleet stayed quiet. A device
listed with no incidents is *asserted clean*, so an alert on it is an unambiguous false alarm;
a device the corpus does not list is out of scope and its findings are counted but never
scored. The report adds `misattributed_detections` for a detection that fired at the right
moment on the wrong device — which, under time-only matching, counted as a correct catch.

Two questions the shipped corpora answer that a demo cannot:

```
# Is the flow detector's hand-chosen 20-peer threshold defensible?
uv run python -m iotshield.eval --corpus fleet_scan --spec config/bench/flow.yaml \
    --sweep threshold=8,15,20,27,35

# Does cross-lane corroboration improve the queue? Same bundle, same bytes, two specs
# differing only in how many lanes are judged.
uv run python -m iotshield.eval --corpus device_trimodal \
    --spec config/bench/trimodal_telemetry.yaml --spec config/bench/trimodal_all.yaml
```

Writing your own corpus generator is two objects: a `Behaviour` (what a device's normal looks
like on one lane, and what a fault does to it) and a `Renderer` (events to wire text). The
labels are then *derived* from what the behaviour actually perturbed rather than annotated by
hand, and a meta-test holds the renderer to being the exact inverse of the matching normalizer.

Add `--fit-protocol prefix` to let detectors implementing the optional `Fittable` capability
relearn their baseline from a reserved head of each asset's own clean history — which is then
**not scored**, so training data and test data are disjoint by construction. Detectors that
cannot train are evaluated as configured and the report says so. `--out report.json` writes a
diffable artifact.

`--corpus` and `--spec` are both required; `eval list` prints which pairs are legal, derived
from the corpus's declared input shape and each spec's declared sources rather than from a
list anyone maintains.

### Choosing hyperparameters

Every knob a detector has is a parameter of its adapter, so a config can set it and a
benchmark can vary it (ADR-0022):

```
uv run python -m iotshield.eval --corpus skab_valve1 --fit-protocol prefix \
    --spec config/bench/team3_scratch.yaml --spec config/bench/stub.yaml \
    --sweep window_size=20,60,120
```

That is four rows in the same head-to-head table: three fitted `team3` configurations plus
the rule-based baseline, which has no `window_size` and rides along once as the control.

A spec either names a trained `artifact:` — which supplies its weights *and* the
hyperparameters they were fitted with, as one unit — **or** names hyperparameters and fits
from scratch under a fit protocol. Mixing the two is refused, because a stored baseline is
aligned to the features its own hyperparameters produced.

### In the dashboard

Set `IOTSHIELD_EVAL_API=1` on the Pipeline API (compose does) and an **Evaluate** mode
appears: pick a corpus and either run a committed `config/bench/*.yaml` verbatim or compose
your own entries, set each detector's hyperparameters (or sweep one), run a benchmark, and
read it as charts — a head-to-head bar, or a **curve** when you swept a parameter, plus a
per-case strip showing consistency and a timeline drawing truth against detections and
alerts on one clock. It is off by default: a research instrument, not a product feature.

One flag, not two. Without `IOTSHIELD_EVAL_API` the `/eval` router is never registered, so
the React dashboard *probes* for it and hides the mode when it is absent (ADR-0031 D5); the
Streamlit dashboard still needs its own `IOTSHIELD_EVAL_UI=1`, which compose also sets.

See `CLAUDE.md` for the developer guide and `docs/adr/` for architecture decisions.
Reference student modules live outside this repo in `../reference/` (read-only).
