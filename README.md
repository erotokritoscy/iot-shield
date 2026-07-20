# iotshield MVP

Containerized integration of the iotshield.ai modular detection pipeline:
IT/OT input → normalization → per-modality anomaly detection → correlation &
alert prioritization → PostgreSQL → FastAPI → dashboard.

CYENS Centre of Excellence internship project (July 2026), integrating existing
graduate-student modules as swappable adapters behind frozen data contracts.

## Quick start (compose)

The whole stack on real Postgres, reproducibly, in two commands:

```
docker compose up -d           # postgres -> init -> read API + Pipeline API -> dashboard
docker compose run --rm runner # run the demo pipeline to populate the database
```

Then open the dashboard at http://localhost:8501, the read API at
http://localhost:8000/docs, and the write-side **Pipeline API** at
http://localhost:8100/docs (compose a pipeline and trigger runs — also driven from
the dashboard's "Compose & run" page). Services find each other by name on the
compose network (the APIs reach Postgres at `postgres`, the dashboard reaches them
at `api` and `pipeline-api`); credentials come from `.env` (copy `.env.example`),
with defaults that run out of the box. `docker compose down -v` tears it all down,
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

```
$env:IOTSHIELD_API_URL = "http://127.0.0.1:8000"            # read API (default)
$env:IOTSHIELD_PIPELINE_API_URL = "http://127.0.0.1:8100"   # Pipeline API (default)
uv run streamlit run src/iotshield/dashboard/streamlit_app.py
```

Four pages: Triage (ranked alert queue + drill-down to evidence), Assets &
telemetry (per-asset timeline with detection overlays), Data health (counts + the
malformed-input audit), and **Compose & run** (pick modules, validate the chain,
and trigger a run against the Pipeline API — its results then appear under Triage).

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
spike-0                     1    60       1    1.00    60.00     1.00       31s
drift-0                     1    56       1    1.00    56.00     1.00       47s
stuck-0                     1     0       0    0.00     0.00     0.00         -
clean-0                     0     0       0       -        -        -         -
```

Three things that table is showing. **`stuck` is invisible** to a fixed threshold — the
sensor freezes at an in-range value, so only a detector that models *normal* can see it;
that is the shape the corpus exists to discriminate on. **`det/inc 60`** is fragmentation:
one fault became 60 detections, which the alert layer compressed to 1. And **`-` means
undefined, not zero** — a clean recording has no fault to recall.

### Corpora

```
uv run python -m iotshield.eval list        # what is here
uv run python -m iotshield.eval generate    # rebuild the committed corpus
uv run python -m iotshield.eval fetch skab  # convert a SKAB checkout into local bundles
```

A corpus is a **bundle** in `data/eval/` — a manifest, gzipped readings, interval labels —
and they are *discovered*, so **adding your own is dropping in a directory, not writing
code**. `synthetic` ships in this repository, so a fresh clone can benchmark immediately.
SKAB is GPLv3 and therefore converted locally rather than redistributed.

Add `--fit-protocol prefix` to let detectors implementing the optional `Fittable` capability
relearn their baseline from each case's own clean history; ones that cannot are evaluated as
configured and the report says so. `--out report.json` writes a diffable artifact.

### In the dashboard

Set `IOTSHIELD_EVAL_API=1` on the Pipeline API and `IOTSHIELD_EVAL_UI=1` on the dashboard
(compose sets both) and an **Evaluate** mode appears: pick a corpus and detectors, run a
benchmark, and read it as charts — a head-to-head bar, a per-case strip showing consistency,
and a timeline drawing truth against detections and alerts on one clock. Both flags are off
by default; it is a research instrument, not a product feature.

See `CLAUDE.md` for the developer guide and `docs/adr/` for architecture decisions.
Reference student modules live outside this repo in `../reference/` (read-only).
