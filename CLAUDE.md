# iotshield MVP — developer guide

Docker Compose MVP that turns raw IT/OT input (telemetry, logs, flows) into prioritized,
explained alerts: ingest → normalize → detect (per-modality lanes) → correlate/prioritize →
Postgres → FastAPI → dashboard. Built by wrapping existing student modules as adapters.
CYENS internship project, July 2026 (intern: Andreas Tziakouris).

## Golden rules

1. **Contracts are law.** `src/iotshield/contracts/` defines the shared objects (NormalizedEvent,
   MalformedRecord, DetectionResult, PrioritizedAlert, Asset). Any change to a contract requires
   a new ADR in `docs/adr/` explaining what breaks and why it's worth it.
2. **Ports and adapters.** Modules never talk to each other directly — only through contracts.
   A new detector/normalizer/prioritizer = a new adapter that passes the *same* port contract
   test suite as every other adapter of that port, and declares a **capability** (what it
   consumes/produces, co-located with the adapter) so the compatibility guard and orchestration
   UI can read its shape without constructing it (ADR-0014). Result **sinks** (`sink/`, ADR-0018)
   are the fourth port — the pluggable output — but being *terminal* consumers of the whole run
   they declare no capability and sit outside the guard; they are still registered and swapped by
   name like the others. If an adapter needs a contract change, that's a design smell — stop and
   discuss.
3. **Reference code is read-only.** Student modules live in `../reference/` (see
   `../reference/INDEX.md` — read it before touching any of them; their READMEs are often stale,
   the code is the truth). Never import from `../reference/` at runtime; copy/wrap what we need
   and note provenance in the adapter's docstring.
4. **Timestamps are sacred.** Everything is correlated by (asset, time). All timestamps are
   timezone-aware UTC. Adapters must validate/normalize timestamps at the boundary and reject
   naive datetimes.
5. **Always demoable.** Prefer a stub adapter that keeps the end-to-end path green over a
   half-integrated real module.
6. **Plan first, implement after approval.** Propose and discuss before building; implement
   only once Andreas approves — unless he explicitly says "implement in one shot, without
   approval". (Full working conventions: workspace-root CLAUDE.md, one level up.)
7. **Test thoroughly.** Everything shipped comes with meaningful tests — unit tests for the
   logic, integration tests for the joints, port contract tests for every adapter. Not
   coverage-number worship; the goal is catching errors early. Untested code is not "done".
8. **Docs move with code.** A change that alters behavior, structure, or contracts updates the
   affected docs (CLAUDE.md, README, ADRs, docstrings) in the same change. Stale docs are bugs.

## Commands

```
uv sync                 # create .venv and install everything (incl. dev tools)
uv run pytest           # tests
uv run ruff check .     # lint
uv run ruff format .    # format
uv run pyright          # type check
scripts/check.sh        # all of the above (CI runs the same)
```

Run all four before considering any task done.

```
# Benchmark against labelled data (ADR-0021) — the other half of the port suite:
# pytest says whether an adapter FITS, this says whether it is GOOD.
uv run python -m iotshield.eval list                 # what corpora exist here
uv run python -m iotshield.eval generate             # (re)build every committed corpus
uv run python -m iotshield.eval generate fleet_scan  # ...or just one
uv run python -m iotshield.eval fetch skab           # convert SKAB into local bundles
# Is the flow detector's hand-chosen threshold defensible? (ADR-0026)
uv run python -m iotshield.eval --corpus fleet_scan --spec config/bench/flow.yaml \
    --sweep threshold=8,15,20,27,35
# Does cross-lane corroboration improve the queue? Two specs, one bundle, same bytes.
uv run python -m iotshield.eval --corpus device_trimodal \
    --spec config/bench/trimodal_telemetry.yaml --spec config/bench/trimodal_all.yaml
uv run python -m iotshield.eval --corpus synthetic --spec config/bench/stub.yaml \
    --with-timeline --out reports/evaluation.json
uv run python -m iotshield.eval --corpus skab_valve1 --fit-protocol prefix \
    --spec config/bench/team3.yaml
# Sweep a hyperparameter (ADR-0022): N labelled rows in the same head-to-head table.
uv run python -m iotshield.eval --corpus skab_valve1 --fit-protocol prefix \
    --spec config/bench/team3_scratch.yaml --spec config/bench/stub.yaml \
    --sweep window_size=20,60,120
# In a spec with several detectors a bare name may not identify one knob — qualify it.
uv run python -m iotshield.eval --corpus device_trimodal \
    --spec config/bench/trimodal_all.yaml --sweep flow_burst.threshold=10,20,30
```

Corpora are **bundles** under `data/eval/` — a manifest, gzipped readings in a real ingest
format, and **per-asset** interval labels. They are *discovered*, not registered: anything under
the root with a manifest is a corpus, so adding one is dropping in data, not writing code. The
root is `IOTSHIELD_CORPUS_PATH`-overridable (default `data/eval/`).

**Truth is indexed by (asset, lane, instant) — ADR-0028 D1, and everything below follows from
it.** That is the whole of what a detector claims (`DetectionResult(asset_id, lane,
window_start, window_end)`), so it is the only coordinate system truth can be compared in: the
unit is forced by the other side, not chosen. Five labels-schema versions were this system being
completed one axis at a time (v1 "something was wrong", v2 the asset, v3 the lane *on the clock*,
v4 the lane *on the incident*, v5 the incident's extent as a list of **spans**) — which reads as
five patches only because nobody wrote the system down first.

It is **not a row**: a row count is a fact about an encoding, not the world. The same SKAB
recording is 1,148 rows wide-form and 9,184 in our long-form contract (ADR-0003 D1 fans a
multi-channel reading into one event per channel), with one fault either way. It is not finer
than an instant either — `DetectionResult` carries no metric and `team3_zscore` is multivariate,
so scoring per channel would mark a correct detection wrong for declining to localize.

**One incident per (fault, lane), sharing a `fault_id` (ADR-0028 D2).** A compromised camera is
one cause with several observable *projections* of different extents — it scans in bursts, fails
an integrity check when one runs, runs hot and stays hot — and there is no truer fourth interval
behind them. So a lane is an axis, not a set-valued attribute: the predecessor's
`Incident.lanes` had to mean "every lane" to point scoring and "no claim" to per-lane recall,
one field read two contradictory ways. Consequences: `affects()` and the empty-set fallback are
gone, per-lane recall is a group-by rather than a special case, and **both denominators stay
derivable** — the event level divides by projections (a telemetry detector can only be asked
about the telemetry projection), the alert level by distinct **faults** (an alert carries no
lane, and an analyst investigates the cause once). `device_trimodal` is 7 projections of 3
faults; a single-lane corpus has them equal.

**An incident is that projection, not an interval: its extent is a list of `spans` (ADR-0029).**
Two questions were sharing one shape. Point scoring needs the **gaps** — a quiet instant inside a
fault is genuinely not faulty, and labelling it charges a detector for correctly staying silent.
Event scoring must **ignore** them — one fault on one lane is one thing. D3 answered the first by
emitting one incident per contiguous run, which made the second's denominator an accident: on the
log and flow lanes a fault *adds* records while routine traffic continues, so whether a heartbeat
lands between two injections or on top of one decides the fragment count. Measured, two identical
error storms produced 2 incidents and 1, moving event recall 0.50 → 0.60 and turning a genuine
tie between two `log_burst` presets into 0.57 vs 0.75. Now the runs live inside one incident:
point scoring reads every span, the event level counts incidents, `ovl` still measures against
the union (so a gapped catch scores 0.93, not 1.00), and the timeline draws one bar per span
rather than painting over the quiet stretches. Telemetry never showed this — a fault there
*modifies* readings that already exist, so nothing interleaves.

A detection is matched on **(asset, lane, time)**; an alert on (asset, time). So there are two
shapes of "nearly right", reported separately because one number would name the wrong cause:
`misattributed_detections` (right moment, wrong device) and `off_lane_detections` (right device,
wrong modality — these used to score as catches, for exactly the reason wrong-device
coincidences used to).

**The roster is a declaration.** Three states, and the middle one is the point: listed *with*
incidents is faulty in those windows; listed with `"incidents": []` is **asserted clean**, so
every detection on it is a false alarm; **absent entirely** is out of scope and its findings are
counted but never scored. Conflating the last two would publish a precision number the corpus
never justified. A generated corpus lists every device it built, which is what makes "that
gateway should have stayed quiet" measurable. `unlabelled_detections` and
`unlabelled_lane_detections` disclose what was set aside in each dimension.

**Point and event rates are counted per lane, then pooled (ADR-0028 D4).** Pooling first weights
each modality by how fast it samples — a corpus-authoring choice masquerading as a measurement.
The pooled figures are kept (two specs on one corpus share every denominator, so they are a fair
head-to-head) but never stand alone: the report prints per-lane recall and per-lane point
precision/recall, pooled from `CaseReport.lanes` rather than averaged across cases. `Counts`
derives every lane-bearing total from that mapping, so a per-lane and a pooled copy of one count
cannot disagree.

Three corpora are generated and committed: `synthetic` (telemetry, 3 fault shapes),
`fleet_scan` (multi-device flow captures with the volume/direction/cardinality controls), and
`device_trimodal` (one device on all three lanes, for the corroboration A/B). SKAB is
**GPLv3**, so it is not redistributed — `fetch skab` converts a local checkout into gitignored
bundles. An absent corpus is a *skip* (exit 0), not a failure, so CI stays green on a fresh
clone — but an **unknown** corpus name is exit 2, because a typo in a CI job must not pass green
having measured nothing. **Labels are v5**; an older bundle is rejected with the command to
regenerate, because an upgrade would have to invent the missing dimension — for v3→v4 it would
have to guess each projection's extent out of the union it stored, which is the one thing that
cannot be un-unioned. v4→v5 is the exception that proves the rule: it *is* mechanically
upgradable (one span each), and is still refused, because that upgrade would faithfully preserve
the inflated denominator v5 exists to remove.

`--corpus` and `--spec` are **required and have no defaults**. Which pairs are legal is derived
from both sides' declared shapes (`eval/bench.py`), and `eval list` prints the table; omitting
either flag prints it too. The old defaults benchmarked `synthetic` with team3 — the one pairing
that cannot work — and printed a table of zeros.

The generator (`eval/generate/`) is a **world** (devices + a clock + declared faults, no
formats) rendered through two ports: a `Behaviour` per lane (what normal looks like, and what a
fault does to it) and a `Renderer` per wire format (events → text). **A label is made of the
instants a behaviour actually perturbed** (`Observed.expressed`), never of the declared fault
window — so the answer key cannot describe something absent from the bytes (ADR-0027 D1) — and
those instants are split into contiguous runs of the lane's **own** clock with the same
`merge_runs` a detector uses, so a quiet stretch mid-fault is not labelled (ADR-0028 D3) — the
runs becoming the *spans of one incident* rather than several (ADR-0029). A fault that changes
nothing is refused at generation time rather than shipped labelled, and so is one that could not
change anything: an **asset id names exactly one device** (two would be handed the same
`FaultSpec` and the builder's merge would keep one device's instants), a scenario records **one
format per lane** (a run pools its sources, so two dialects of one lane double every event
against a truth that counted each once), and an intensity that would make a fault run
**downward** is refused with the per-channel floor named — `intensity` is multiples of an alarm
line, but the handlers reach an absolute target, so anything under `baseline/boundary` (0.938 for
a rig's Temperature) inverted the fault while the label still named it. Adding a
lane is one behaviour + one renderer; a dialect is one renderer; a corpus is one `Scenario`. A
renderer is the **inverse of a normalizer**, and the meta-test that composes the two (emit →
parse → compare, every field) is what caught an RFC 3164 corpus silently scoring zero after New
Year; a companion test refuses a bundle-storable format with no renderer. Streams are seeded by
*name*, so adding a device leaves every other device's bytes untouched.

A fault shape must also be well-defined at **one sample**, because a device's sampling rate
decides how many readings a declared window covers: 40 steps is 40 samples on a 1 Hz rig and one
on a gateway reporting twice a minute. `spike` and `drift` were built from `linspace`, which
spends its endpoints on the boundary values — so a one-sample spike multiplied its amplitude by
`sin(0)` and perturbed nothing, and the scenario was refused for having an unfindable *label*.
`_bump`/`_ramp` sample the interior instead. Only `stuck` keeps a declared minimum (`_MIN_SAMPLES`),
because "the sensor froze" is a claim about two readings being identical and is genuinely
inexpressible in one.

**`FaultSpec.intensity` is dimensionless: multiples of the detector boundary a behaviour
declares for that fault kind (ADR-0028 D6).** `1.0` means "exactly at the alarm line" on every
lane at once, and each behaviour converts into its own units — peers, ports, byte-chunks, log
lines, an absolute channel value. A single scalar was always right; leaving it *dimensioned* was
not: the 30 that `device_trimodal`'s compromise needed for 30 fan-out peers also meant "×30 the
thermal rise", driving the camera to **316.7 °C against a threshold of 80** while its docstring
called it modest — so the corroboration A/B compared corroboration against a problem telemetry
had already solved. `Calibration` is therefore load-bearing rather than documentary, declared
per fault kind (fan-out's 20 peers, port-scan's 15 ports and exfiltration's 50 MB are three
alarm lines on one lane), and two meta-tests hold it: every expressible fault has a boundary
matching the registry, and a scenario can assert its own **difficulty** — the test whose absence
let the 316.7 ship.

Detectors judge every channel of every device against one absolute value, so a device *kind*
selects a telemetry profile too (ADR-0028 D7): a gateway reports CPU load twice a minute rather
than a rig's pressure at 1 Hz. Both realism and measurement — three 1 Hz devices made telemetry
2,700 of `device_trimodal`'s 3,597 clock entries. A flow profile may also **burst**
(`burst_every_steps`, ADR-0028 D5), which is what decouples "chatty in cardinality" from "chatty
in volume": a device above fan-out's 20-peers-per-minute boundary must emit 20+ flows per
minute, so the deliberately-over-threshold mirror was forced to be the corpus's fastest sampler
and owned **99.3%** of `fleet_scan`'s point false positives (flow-lane point precision 0.016
with it, 0.706 without). That is ADR-0021 D7's circularity — a number we typed in becoming the
published result — in the point metrics rather than the ranking ones.

**Corpus-level rates are pooled from counts, not averaged from per-case rates** (ADR-0027 D4).
A rate is undefined on a clean recording and averaging skips undefined values, so a spec that
false-alarmed on the clean recordings used to score identically to one that stayed quiet. The
same rule now reaches the per-lane columns (ADR-0028 D4), and `SpecReport.pooled` /
`pooled_by_lane` serialize the headline so a CI gate need not re-pool. `ovl` is the width column:
of a true detection's window, how much was actually fault — a recording-wide detection scores
evP 1.00 and ovl 0.04.

`--fit-protocol prefix` reserves the first 20% of each asset's clock (capped at its first
fault) for training and **does not score it**. Rates are defined once, on `score.Counts`:
`score_detections` asks it for one case's, `SpecReport.summary` for the sum of them, so a
per-case column and the headline cannot disagree about a denominator. There is no `holdout`: no bundle format can
carry a holdout recording, so the option was removed rather than left as a table of zeros.

Note `team3_zscore`'s committed artifact is trained on SKAB's channels, so it cannot judge
the synthetic corpus (the channel-coverage guard skips it; `--fit-protocol prefix` reports
why). Compare detectors on `synthetic` with `stub_threshold`, on SKAB with `team3_zscore`.

A bench spec for a corpus must declare **one source per input position, in the corpus's order**
— `CorpusInfo.source_formats` (and `/eval/corpora`) publish that shape, and `/eval/specs`
publishes each committed `config/bench/*.yaml` with the corpora it fits, so the Evaluate page
can run the *committed file verbatim* rather than composing a lookalike that silently drops
`assets:` and every source and engine param. `/eval/corpora` also
publishes `lanes` (which detector lanes those formats feed, decoded server-side so no client
parses a `lane:` prefix), which is how the Evaluate page offers only detectors the corpus can
actually feed. Flow and trimodal bench specs deliberately register **no `assets:`**, because
`asset_weight` is something we type in and would be indistinguishable from a real ranking
improvement (ADR-0021 D7).

A benchmark can be *aligned* and still incoherent one joint further along — ingest the FLOW lane,
hand it to a TELEMETRY detector, and every input matches while the detector is handed nothing.
`evaluate` therefore consults the compatibility guard and carries its warnings — **and the
ingest warnings `normalize_sources` computes**, including "this normalizer rejected 100% of
input", which catches a normalizer misconfigured by *params* that alignment cannot see — into
the report's per-case `notes` (rendered as `! …`), so a row of zeros always arrives with its
reason instead of reading like a bad detector. Reported, not refused: a deliberately silent
detector is a real measurement (ADR-0026 amendment, ADR-0027 D8).

```
docker compose up -d            # postgres -> init -> read API (:8000) + Pipeline API (:8100) -> dashboard (:8501)
docker compose run --rm runner  # batch job (profile "jobs"): populate the DB
docker compose down -v          # stop and drop the Postgres volume
```

The DB location is injected via `IOTSHIELD_DATABASE_URL` (compose → Postgres),
which overrides the config file's `database_url` (ADR-0010); the config file
keeps its SQLite default for host runs. Schema creation is owned by the one-shot
`python -m iotshield.storage.init` step (which the API, dashboard, and runner
depend on in compose); the read-only API never creates the schema, and the
runner still creates it for standalone host runs. The runtime dependency surface was
numpy-only from team3 (ADR-0012) until the meter detector brought **scikit-learn** in
(ADR-0030) — the first model library in the image, ~100 MB with scipy, which makes image
hardening more valuable rather than less. `statsmodels` stays out (MSTL only). torch is
still deferred to its own per-model ADR.

A run ingests **one or more sources** (ADR-0019): `PipelineSpec.sources` is a list of
`{normalizer, source_format, path?, params?}`, each routed to its normalizer by its declared
format (explicit tagging, never sniffing) and pooled into the detector lanes — so one run can
mix modalities. The legacy top-level `source_format`/`normalizer` desugars to a one-element
`sources` list, so old configs still work. Detectors are handed their lane's events sorted
by `(asset_id, timestamp)` — a port guarantee windowed models rely on.

Normalizers take `params` like every other port (ADR-0023 D8); the runner caches them by
name **and** params, so two sources of one normalizer with different settings stay distinct.
`detectors` is a list of *instances*, so the same adapter may appear more than once with
different params — that is how two presets run together, and the compose page renders it as
a list of rows (ADR-0023 D9). Two *identical* entries earn a guard WARNING: they judge the
same events twice, which the alert engine's `volume` factor miscounts as corroboration.

**All three lanes are now judged.** Flow events are one event per flow record (not a
per-metric fan-out): the tuple is the signal, so it rides in `metadata` behind the typed
`FlowRecord` in `src/iotshield/flow.py` — the FLOW lane's vocabulary module, which exists
because flow is the first modality whose payload outgrows the contract's own fields
(ADR-0023 D5). Flows identify a device by **IP**; telemetry and logs identify by name, so
cross-lane correlation involving flows currently requires the sources to agree on an
identifier (asset aliasing is an open item — ADR-0023 Consequences).

**Utility meters are the second vocabulary module** (`src/iotshield/meter.py`, ADR-0030), for
the same reason: a meter reports a **cumulative counter**, but the meaningful observation is
**consumption over an interval**, and one `value` cannot hold both. So consumption is the
event's `value` and the raw counter, interval width and utility ride in `metadata` behind the
typed `MeterReading` — which is what keeps the boundary **invertible** (a renderer is the
inverse of a normalizer) and lets a later data-quality detector see a rollback with no second
ingest path. A consumption event is stamped at the interval's **END**, the *opposite* of the
FLOW lane's start-stamp; neither is inferable from a timestamp, so `interval_hours` travels
with every event. A **gap** yields no event at all (hourly consumption across it is unknown;
inventing it is the "gap-boundary false spike"), and a **counter rollback** is quarantined —
real data, but consumption across a reset is undefined, so we refuse visibly rather than
invent. Differencing lives in the normalizer; the commissioning/service-start trims and the
usability verdict are modelling judgments and live in the detector, because a normalizer that
silently dropped a meter's first months could not be inverted (ADR-0030 D3/D4).

Eleven demo configs (`config/demo*.yaml`) exercise the adapters; the main ones:
`config/demo.yaml` + `data/demo/demo.csv`
(telemetry → the `stub` normalizer → `stub_threshold` detector → detections/alerts);
`config/demo_syslog.yaml` + `data/demo/demo_syslog.log` (syslog → the `syslog`
normalizer, re-implemented from team4 → LOG-lane events across three hosts, both
RFC dialects + quarantined malformed lines; no detections by design — ADR-0011);
`config/demo_team3.yaml` + `data/demo/demo_team3.csv` (telemetry → the `team3_zscore`
detector, vendored from team3, on a real SKAB valve fault → one collective
detection → one alert — ADR-0012); `config/demo_triage.yaml` +
`data/demo/demo_triage.csv` (both detectors → the `weighted` engine over four
assets → a *ranked* queue where severity, asset criticality, and detection volume
each visibly reorder the alerts — ADR-0013); `config/demo_file.yaml` +
`data/demo/demo.csv` (the SAME pipeline as `demo.yaml`, but a **file/JSONL sink**
instead of Postgres — no database, no dashboard — demonstrating the pluggable
result store, ADR-0018); `config/demo_log.yaml` + `data/demo/demo_log.log` (an sshd brute
force → the `log_burst` detector's `auth_failure` preset → twelve failures in ~60s merged
into ONE collective detection → one HIGH alert; every line is severity INFO, so the
`severity_spike` preset would see nothing — ADR-0020); and `config/demo_multimodal.yaml`
(two sources in ONE run — `data/demo/demo_multimodal.csv` telemetry +
`data/demo/demo_multimodal.log` syslog on the same asset — routed, pooled into two lanes,
and **both judged**: the telemetry spike plus a log burst correlate into one alert whose
`corroboration` factor is 1.5, the first time cross-lane agreement has ever fired — the
same spike scores ~27/LOW on telemetry alone vs ~56/HIGH corroborated — ADR-0019 + ADR-0020);
`config/demo_flow.yaml` + `data/demo/demo_flow.log` (a real-shaped Zeek `conn.log` → the
`flow` normalizer → the `flow_burst` detector's `fan_out` preset → a compromised IP camera
sweeping 41 hosts for telnet merges into ONE collective detection, score 43 vs boundary 20
→ 44.1/MEDIUM; a gateway sending 30 flows to *one* destination correctly stays silent, an
inbound SSH session does not count as fan-out, and a flow between two foreign hosts is
quarantined not dropped — ADR-0023 + ADR-0024); and `config/demo_trimodal.yaml`
(**all three lanes judging one asset in one run** — telemetry + syslog + flow on the camera
→ 4 detections across 3 lanes → `corroboration` 2.0 (its highest value with today's three
lanes; the factor `1 + 0.5·(lanes−1)` is not itself capped);
the identical flow sweep scores 44.1/MEDIUM alone vs **76.4/CRITICAL** corroborated); and
`config/demo_passthrough.yaml` + `data/demo/demo_passthrough.jsonl` (a partner system that
already emits `NormalizedEvent` JSONL → the `passthrough` normalizer **validates and
envelopes** rather than parses → 7 events, 3 quarantined lines (naive timestamp, wrong-lane
record, non-JSON line), the `stub_threshold` detector ranks the pump's two correlated spikes
above the discovered sensor — the smallest adapter that honors every port law, and the first
normalizer whose produced lane is a config **param** so the guard stays accurate — ADR-0025).

`config/demo_meter.yaml` + `data/demo/demo_meter.csv` and `data/demo/demo_meter_sensipro.csv`
(**two different meter platforms in ONE run, judged by the same detector** — ADR-0030: an EOA
water CSV written newest-first in naive Cyprus local time, plus a SensiPro hourly export
carrying water *and* electricity → the `meter` normalizer's two dialects difference both
counters gap-safely → 332 + 1006 consumption events, 3 quarantined rows, a 2-hour gap that
correctly yields nothing → the `meter_iforest` detector flags ONE thing: a continuous
overnight draw at ~2x the evening peak held for three hours, score 0.7994 vs a threshold
0.7513 *learned* on ten weeks of that meter's own clean history at ~1 false alarm per
meter-month. The evidence names `deviation` at z=25.4 — "consumption far above the usual level
for this hour of the week" — which is the case a magnitude threshold cannot express, since the
volume is ordinary for an evening and extraordinary for 3am. The SensiPro meters have no
fitted model in the demo artifact, so they are **skipped, not scored**, which is what
one-model-per-meter means.)

The meter artifact is committed (`src/iotshield/detect/meter/artifacts/demo_meter.json`) and
regenerated with `uv run python -m iotshield.detect.meter.train`. Its format differs from
team3's deliberately (ADR-0030 D6): it stores the **clean history each meter was fitted on**
rather than the fitted weights, and re-fits on load. A 200-tree forest is megabytes of
unreviewable JSON or a version-fragile pickle; the training matrix is smaller than the model
it produces, is what a reviewer actually wants to see, and makes model and threshold
impossible to get out of step. That also bounds where the format is appropriate: an artifact
carries data, so committing one is only acceptable for data we may commit (the demo meter is
synthetic). There is **no universal meter model** — a fleet of 1,656 meters needs 1,656 fits,
done at deployment from each meter's own clean history.

`meter_iforest` is the first registered detector that is **untrained by default**, and
`ModuleInfo` now publishes `artifact_param` so the compose page preselects a detector that is
*ready to run* — without it the page opened on `meter_iforest` (it sorts first) and therefore
on a chain `compute_run` refuses.

The team3 artifact is committed (`src/iotshield/detect/team3/artifacts/skab_zscore.json`);
regenerate it only when the model/data change: `uv run python -m iotshield.detect.team3.train`.
Its layout is schema v2 — `{params, state}`, the ADR-0022 split made visible in the file:
`params` is what a human chose, `state` is what fitting computed (baseline, threshold, and
the channel/feature schema it learned). A v1 file is rejected with instructions to regenerate.

## Layout

```
src/iotshield/
  contracts/       frozen data contracts (pydantic v2) — the product's core
  capabilities.py  adapter capability vocabulary (what each stage consumes/produces)
  flow.py          FLOW-lane vocabulary: the typed FlowRecord that crosses normalize->detect (ADR-0023)
  meter.py         METER vocabulary: the typed MeterReading (counter + interval + utility) (ADR-0030)
  normalize/    Normalizer port + adapters (stub, syslog, flow+dialects, meter+dialects=EOA/SensiPro cumulative counters -> hourly consumption, passthrough=validate-and-envelope pre-normalized JSONL, ...); a run routes each of its sources here by format (ADR-0019)
  detect/       Detector port + adapters (stub threshold, telemetry-ad, log burst, flow burst, meter iforest, ...)
  alert/        scoring/correlation port + adapters (stub, weighted, ...)
  sink/         ResultSink port + adapters (postgres default, file/JSONL, ...) — pluggable output
  storage/      SQLAlchemy models + repositories (Postgres; SQLite for tests)
  pipeline/     compute (store-agnostic) + registry + compatibility guard (run_pipeline entry point)
  api/          FastAPI services: read side (app.py) + write-side Pipeline API (control.py)
  dashboard/    Streamlit app (consumes the APIs only: Monitor = Triage/Assets/Health; Operate = Compose & run; Evaluate = benchmarks + hyperparameter sweeps over *entries*, env-gated)
  eval/         evaluation harness (ADR-0021) — the THIRD plane; imports the pipeline, never imported by it
                sweep.py = hyperparameter sweeps (ADR-0022): one spec -> N labelled specs
                truth.py/score.py = (asset, lane, instant) answer keys and matching (ADR-0028)
                generate/ = synthetic corpora: world + Behaviour/Renderer ports + scenarios
tests/          mirrors src; port contract tests are parametrized over all adapters
docs/adr/       architecture decision records (numbered, append-only)
config/         pipeline configs (which adapters run); config/bench/ = benchmark specs
data/demo/      small demo inputs (keep tiny; big datasets stay in ../reference/)
data/eval/      corpus bundles (manifest + gzipped readings + interval labels)
```

**The evaluation plane (ADR-0021).** `eval/` judges the pipeline rather than using it, so it
needs labelled corpora — which production data by definition never has. The dependency arrow
points one way (eval → pipeline, never back), and a benchmark **cannot** write to a store:
`compute_run` and the guard take `PipelineSpec`, which has no `database_url`, so it is the
type checker rather than discipline that stops twenty benchmark runs flooding an operator's
triage queue.

Two flags, both off unless a deployment opts in (compose sets both — it is the research
stack): `IOTSHIELD_EVAL_API` decides whether the Pipeline API *registers* the `/eval` router
(absent, not merely guarded — a product image has no route and no OpenAPI entry), and
`IOTSHIELD_EVAL_UI` shows the dashboard's Evaluate mode. The dashboard runs benchmarks
through the API and renders them; it never computes, and its `eval` imports are deferred so
the package stays deletable. With no API reachable it falls back to a saved report JSON.

Detectors may implement the **optional** `Fittable` capability (`fit(events) -> Detector`,
returning a new detector). The pipeline never calls it; only a benchmark does, so it can
learn "normal" from an asset's own clean history instead of measuring domain shift. Not
implementing it is not a defect — the harness reports "not fittable" and evaluates as
configured. A detector that scores rows should form judgments with `detect/_runs.py:
merge_runs`; see the port docstring for why the alert engine's `volume` factor depends on it.

`detect/` has three shared skeletons a new adapter inherits rather than re-derives:
`_group.py` (the port's routing laws — foreign-lane isolation, deterministic per-asset
order), `_runs.py` (a per-row verdict mask → spans), and `_windows.py` (a sliding time
window + incremental `Aggregate` → merged episodes, ADR-0024 D4). A volumetric detector
("too much of something, too fast") supplies an aggregate and gets the rest for free;
`log_burst` counts events, `flow_burst` counts distinct peers/ports or sums bytes.

**Hyperparameters vs fitted state (ADR-0022, a LAW for any adapter that learns).**
A detector's hyperparameters are *exactly* its registry factory's parameters — declared as
a frozen pydantic model on `AdapterSpec.params_type`, so config sets them, `/modules`
reports them, and the harness sweeps them. `fit()` recomputes learned parameters *and*
learned schema from the data, using the hyperparameters it was **constructed** with; it may
never read one out of its own previous fitted state. A persisted artifact is an **atomic**
cached fit (its weights are aligned to the features its own hyperparameters produced), so
`{artifact: x, window_size: 20}` is refused at construction with the artifact's settings
printed as copy-pasteable YAML. Naming no artifact yields an *untrained* detector:
`is_fitted` is False, `detect()` raises `NotFitted`, a **run refuses** it (`compute_run`,
before judging and before any sink) and a **benchmark reports** it. The port suite enforces
the law behaviourally — build twice differing in one hyperparameter, fit both on the same
data, the results must differ. Adapters that learn nothing (`stub_threshold`, `log_burst`)
declare neither field and are unaffected.

`config/bench/team3.yaml` names an artifact (fixed hyperparameters, sweepable only across
*detectors*); `config/bench/team3_scratch.yaml` names hyperparameters and no artifact, so it
is the one to `--sweep` — and it requires `--fit-protocol prefix`, since an
untrained model has nothing to judge with.

A swept key is `[<detector>.]<param>`. Bare (`window_size`) reaches every detector in the spec
that declares it, which is the right default when one does. Qualified
(`flow_burst.threshold`) reaches exactly that detector, and exists because a parameter name is
unique only *inside* a detector: `threshold` is 80.0 on the telemetry lane and 20 distinct
peers on the flow lane, so a bare sweep of it across a spec holding both is refused (the error
names the qualified alternatives). The grammar lives only in `eval/sweep.py` — the key is a
plain string in the CLI flag, the API request and the dashboard alike (ADR-0022 amendment).

## Conventions

- Python 3.11+, fully type-hinted; pydantic v2 for contracts; `typing.Protocol` for ports.
- Small files, one concern each. Match existing naming.
- Tests accompany every adapter; the port test suite is the swap-safety net — never skip it.
- Demo datasets: SKAB telemetry (in `../reference/team3-telemetry-ad/Datasets/`) + synthesized
  same-asset logs. Big/raw datasets are never committed to this repo.
