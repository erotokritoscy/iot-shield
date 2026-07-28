# 28. The answer key's coordinate system

Date: 2026-07-28

## Status

Accepted. Amends ADR-0021 (D2 scoring), ADR-0026 (D1 per-asset truth) and ADR-0027 (D1 label
derivation, D2 per-lane clocks, D4 pooled rates). Bumps the on-disk answer key to **v4**.

Closes four items ADR-0027 left deferred: the convex-hull label (D3 here),
`unlabelled_lane_detections` (D4), `_attribution`'s macro-averaged per-lane recall (D4), and
`summary()` not being serialized (D4).

## Context

ADR-0027 was written against the complaint that the generator and the harness "should talk the
same language but instead conflict each other". It fixed fifteen findings and the numbers moved.
The complaint came back anyway, in a sharper form: *we keep patching, and we still struggle to
say which lane or which device a fault belongs to.*

That instinct was right, and the reason it kept recurring is that **four ADRs had been completing
one coordinate system one axis at a time without ever writing the coordinate system down.**

| | the answer key could say | added by |
|---|---|---|
| v1 | "something was wrong at 00:05" | ADR-0021 |
| v2 | "the camera was wrong at 00:05" | ADR-0026 |
| v3 | "the camera's *network clock* was wrong at 00:05" | ADR-0027 D2 |
| v4 | "the camera's network was wrong at 00:05" | this record |

Read as history that is four repairs. Read as structure it is one system being completed, and each
step felt like a repair only because it was discovered by a number coming out wrong rather than by
someone stating the system first. So D1 states it, and the rest of this record follows from it.

Three concrete defects were measured before being written down.

**The lane was an axis implemented as a set-valued attribute.** `Incident.lanes` had to be read
two contradictory ways: `truth.Incident.affects` read an empty set as "this fault reached every
lane", while `score._lane_recall` read the same empty set as "this corpus makes no per-lane claim,
report nothing". The docstring documented the contradiction and warned the next reader to "choose
deliberately rather than by default". ADR-0027 D2 had considered per-lane intervals and rejected
them — but on the wrong axis: it weighed whether the *interval* differed materially (it barely
does) and never weighed whether one field carrying two opposite meanings belongs in a design.

**One deliberately-noisy control device was setting the corpus's headline.** `fleet_scan`'s update
mirror is configured to sit *above* `flow_burst`'s fan-out threshold, which is excellent corpus
design — ADR-0024 predicted a fleet-wide constant would false-alarm on chatty devices, and a corpus
is where a prediction becomes a number. But fan-out counts distinct peers inside a sliding minute,
so a device that exceeds it **must** emit at least that many flows per minute. `every_steps=2` was
forced by `distinct_peers=26`, making the one device we configured to false-alarm the fastest
sampler in the corpus:

```
asset            rows   ptTP   ptFP   ptFN   role
10.0.0.53        2250      0   2250      0   MIRROR (26 peers every 2s, deliberately over 20)
10.0.0.24         306     24     10     80   camera (the scanner)
10.0.0.25          56     12      5      0   camera2 (second scanner)
TOTAL            7862     36   2265     80

mirror share of all point false positives: 2250/2265 = 99.3%
point precision with mirror   : 0.016
point precision without mirror: 0.706
```

The published `ptF1 = 0.03` for the flow lane measured one line of `flow._PROFILES`. This is
exactly the circularity ADR-0021 D7 refuses for `asset_weight` — "a criticality we type in would
be indistinguishable from a ranking improvement" — applied to the point metrics instead of the
ranking metrics, where nobody was looking.

**`intensity` was one scalar with three incompatible unit systems.** `FaultSpec.intensity`'s
docstring defends a single scalar over a per-lane dict on extensibility grounds, and that argument
is sound. Leaving the scalar *dimensioned* was not: the same number meant "30 network peers" to the
flow behaviour and "multiply the thermal rise by 30" to the sensor rig. `device_trimodal`'s
compromise needs 30 peers to clear fan-out, so:

```
FaultSpec(kind='compromise', asset='10.0.0.24', length=40, intensity=30)
Temperature: baseline 75.0 -> max 316.7      (stub_threshold's threshold: 80.0)
```

Against `_thermal_rise`'s own docstring — *"kept modest on purpose — the telemetry signal alone
should be arguable, so that the multimodal corpus measures corroboration rather than a spike anyone
would catch"* — and `_trimodal_recordings`' *"each is arguable alone — which is the point"*. The
telemetry-only arm reported `lane_recall telemetry = 1.00`, so the corroboration A/B was measuring
whether corroboration helps a problem telemetry had already solved. Every test passed, because no
test asserted the one property the corpus was designed around.

Symptom of all three at once, printed on one line: on `device_trimodal`, adding the lanes the
corpus exists to vindicate made pooled point-F1 **fall 0.56 → 0.23** while event-F1 rose 0.80 →
0.98. A reader had to be told which column to believe.

## Decision

### D1 — Truth is indexed by (asset, lane, instant)

Because that is the finest thing a detector can claim. `DetectionResult(asset_id, lane,
window_start, window_end)` is the whole of a detector's output, so truth is comparable in those
coordinates and no others: the unit is *forced* by the other side rather than chosen.

Two consequences that were previously stated wrongly or not at all.

**Not a row.** `truth.py` documented the answer key as "one `(timestamp, is_anomalous)` per source
row". A row count is a fact about an *encoding*, not the world — SKAB is wide (one row, every
channel), our contract is long (one row per reading, ADR-0003 D1). The same recording:

```
case: valve1-0
stub-csv source rows in OUR bundle  : 9184
distinct instants (SKAB source rows): 1148
channels                            :    8
clock entries in the answer key     : 1148
```

One fault either way. "One label per row" is not a property any design could have had; a label per
*instant* does not change size when someone reformats the file. `GroundTruth.anomalous_rows`
becomes `anomalous_instants`, and the prose in `truth`, `bundle` and CLAUDE.md follows.

**Not finer than an instant**, and that is a statement about detectors rather than about ambition —
see the rejected options.

This decision changes no behaviour by itself. It is here because D2–D7 are its corollaries, and
because the absence of this paragraph is what made four ADRs read as four patches.

### D2 — A lane is an axis of the incident: one incident per (fault, lane)

`Incident.lanes: frozenset[Lane]` becomes `Incident.lane: Lane`, required. A fault observable on
three modalities is three incidents sharing one `fault_id`.

This is the right shape for what is being described. A compromised camera is **one cause with
several observable projections**: it scans in bursts, fails an integrity check whenever one runs,
and warms up and stays warm. Those have different extents and there is no truer fourth interval
behind them, because the cause itself is unobservable. A telemetry detector cannot be blamed for
missing the scan — it has no vocabulary for it. `affects()` was groping toward this and implemented
it as an overloaded field.

What the change **deletes**, which is the case for making it:

- `Incident.affects()` — an incident *is* on a lane; nothing to interpret.
- the empty-set fallback — a converted corpus now names the modality it actually covers
  (`from_timeline` already took a `lane`), which is *true*, rather than "empty, meaning all, except
  where it means undefined".
- `score._lane_recall` — per-lane recall is now `LaneCounts.rates()['event_recall']`, formed by the
  same rule as every other rate rather than by a bespoke nested scan.
- the pooled-clock dependency in `build._incident` (see D3).

And it **adds** derivability rather than removing it. `fault_id` groups the projections, so both
questions stay answerable as plain group-bys:

- the **event** level divides by projections (`Counts.incidents`), because a telemetry detector can
  only be asked about the telemetry projection;
- the **alert** level divides by distinct faults (`Counts.faults`), because `PrioritizedAlert`
  carries no lane, correlating across lanes is what it exists to do, and an analyst investigates
  the cause once. Dividing alerts by projections would report a correctly corroborated alert as
  having covered a third of an incident.

`alerts_per_incident` and `incident_coverage` keep their published names — `CaseReport` is the JSON
a CI gate keys into — and change denominator. `fault_id` is therefore no longer decorative:
`from_timeline` synthesizes one per run, since an empty id would collapse every fault in a recording
into one.

Matching becomes lane-aware at the detector level, which yields one new counter.
`off_lane_detections`: right device, right moment, **wrong modality**. Under lane-blind event
matching every one of these scored as a correct catch, for exactly the reason a wrong-device
coincidence used to (ADR-0026) — one axis over. It is the twin of `misattributed_detections`, and
the two are reported separately because "nearly right" has two distinct shapes and one number would
name the wrong cause.

*Cost.* Labels schema **v4**; v3 files are refused with the regenerate command. The refusal is
sharper than usual: splitting a v3 lane set into v4 incidents would have to guess each projection's
extent, and the union it stored is the one thing that cannot be un-unioned.

### D3 — A label is split at the gaps, not hulled

`build._incident` becomes `_incidents` and walks each lane's own clock with `detect.merge_runs` —
the function a row-scoring detector uses to form its judgments, and the one
`AssetTruth.from_timeline` uses on a converted corpus. Truth and detections are now formed by one
rule at both ends, which is the property that makes the event comparison apples-to-apples.

ADR-0027 deferred this with the note that the hull cost zero over-labelled instants today "but that
is a parameter coincidence". The coincidence is sharper than that: the exfiltration fault injects
every ~6.7 s into a camera whose normal flows are every 20 s, so every normal flow inside the hull
lands on a second an injection had already perturbed and **vanishes into the instant set**. The
row-level over-labelling was real; the dedup concealed it. Move either number and it appears.

Per-lane incidents are what make the split cheap: the predecessor's interval spanned lanes, so it
would have had to walk the asset's *pooled* clock and split on gaps no single lane saw.

*Observable consequence: none.* All 21 expressed faults in the shipped corpora yield exactly one
run per lane, so this converts a parameter coincidence into a structural property for free.

### D4 — Point and event rates are counted per lane, then pooled

`score.LaneCounts` holds one lane's tallies; `Counts.lanes` maps lane to it and **derives** every
lane-bearing total (`incidents`, `incidents_caught`, `scoreable_detections`, `true_detections`, the
three point counts) from it. Storing a lane-summed copy beside the mapping would be two
representations of one fact, which is the sin ADR-0027 D4 spent a decision removing one level up.
`Counts.__add__` is written out rather than folded over `fields()`, because a Mapping does not
support `+` and a fold that silently did the wrong thing for one field would be worse.

Pooling first weights each modality by **how fast it samples**, which is a corpus-authoring choice
masquerading as a measurement. The pooled figures are kept — two specs on one corpus share every
denominator exactly, so the pooled number is a fair head-to-head even where it is not interpretable
as an absolute precision — but they may no longer stand alone. `_attribution` prints per-lane
recall *and* per-lane point precision/recall, **pooled from counts**, which is also what closes
ADR-0027's own deferred note that per-lane recall was still macro-averaged immediately below a
decision saying mean-of-rates is wrong.

Three more ADR-0027 deferrals fall out of the same structure:

- `unlabelled_lane_detections` — a detection on a lane the corpus keeps no clock for. Invisible to
  point metrics (no instants to be wrong about) and previously still counted in event precision: a
  silent one-sided penalty. Now excluded in both directions and disclosed, exactly as an unlabelled
  *asset* is.
- `SpecReport.pooled` / `pooled_by_lane` — the corpus-level headline, serialized. A CI gate had to
  re-pool from `cases[].*`, which is a third place that would know how a rate is formed. Serializing
  the *result* of the one definition is not a second definition.
- `CaseReport`'s lane-summed counts become `computed_field` properties: derived in the model, still
  present in the JSON.

### D5 — A flow profile can burst, so a cardinality control stops owning the row count

`_Profile` gains `burst_every_steps` / `burst_span_steps`; the mirror and the resolver sync
periodically instead of trickling. `__post_init__` requires exactly one timing mode, because a
field the other mode ignores is a field that lies about the table it sits in.

Raising `every_steps` was not available: at one flow every twenty seconds the mirror can never
reach twenty peers inside a minute and the control evaporates. The missing expressiveness was
burstiness — which is also what an update mirror actually does. The mirror still trips fan-out
inside each burst, so ADR-0024's **event-level** prediction is preserved exactly; only the row
weight it carries into pooled point metrics drops, from 450 flows per recording to ~78.

### D6 — `intensity` is dimensionless: multiples of the boundary it is calibrated against

`Calibration` is promoted from documentation to the **unit** every fault magnitude is quoted in,
and is declared per *fault kind* (`Behaviour.boundary(kind)`) rather than per behaviour — fan-out's
20 peers, the port scan's 15 ports and exfiltration's 50 MB are three different alarm lines on one
modality. `Behaviour.calibration` is derived from `boundary` over `expresses`, so the two cannot
disagree.

`intensity=1.0` now means "exactly at the alarm line" on every lane at once, and each behaviour
converts in its own units: peers, ports, byte-chunks, log lines, or an absolute channel value. The
detectors use a strict `>` (the port mandates it), so 1.0 sits *on* the line and a fault meant to
fire needs a little more — that asymmetry belongs to the detector. A shape with no magnitude
(`stuck`) ignores it.

Two meta-tests become possible, and they are the real prize:

- every kind in `expresses` has a boundary that matches the registry's registered default. This
  immediately exposed a gap: `flow.CALIBRATION` declared fan-out and port-scan but **not** the
  exfiltration preset, so `_EXFIL_CHUNK_BYTES` was pinned to nothing.
- a scenario can assert its own *difficulty*. `TestDeclaredDifficulty` pins that
  `device_trimodal`'s compromise is marginal on every lane, from both ends: the declared intensity,
  and the peak value in the generated bytes. That is the test whose absence let a 316.7-degree
  "modest" thermal rise ship.

### D7 — The telemetry behaviour honours `Device.kind`

`SensorRig` gains a profile table. `Device.kind`'s docstring promised profile selection with a
default fallback; that was true of the flow and log behaviours and false here, so
`device_trimodal`'s uplink gateway and update mirror emitted industrial pressure readings at 1 Hz.

Beyond realism it was the largest single distortion of the pooled point metrics: three devices at
900 instants each made telemetry 2,700 of the corpus's 3,597 clock entries, so a "corpus-wide"
point precision was 76% a telemetry number. D4 stops that being *hidden*; giving a gateway a
gateway's sampling rate stops it being *true*. The rig profile is unchanged, because its channels
are what make `synthetic` discriminate (ADR-0021 D5b).

## Rejected

**Per-metric point truth.** Rejected on evidence, not cost. `DetectionResult` carries no metric —
only a free-form `evidence` dict that `stub_threshold` happens to populate — and `team3_zscore` is
multivariate: it computes a robust z-score across channels and claims an *instant*, not a channel.
Scoring per metric would mark a correct multivariate detection wrong for declining to localize, and
would need a contract change (golden rule 1) to be expressible at all. The unit of judgment
genuinely is the instant. This is why D1 says the coordinate system is not finer.

**Labelling exact events instead of windows.** Attractive — it dissolves the row/instant question
entirely, since an event is an event. Fatal: `NormalizedEvent.event_id` is
`default_factory=uuid4`, generated fresh on every parse, so the ids do not exist until ingest and
cannot be written into a file. Identifying an event durably means `(asset, lane, timestamp,
metric)`, which is this coordinate system plus the metric axis rejected above. That collapse is
itself evidence the system is forced rather than chosen.

**Dropping point metrics entirely.** The largest simplification available: event metrics need only
intervals, so the clock, the row/instant question, and D4's whole weighting problem disappear.
Rejected for two reasons — point metrics are what the published literature reports, so losing them
makes our SKAB numbers incomparable with anyone else's; and the clock also serves
`--fit-protocol prefix`, which must know an asset's history extent. Recorded because it is a real
option someone may want to revisit, not a straw man.

**Moving the mirror into its own recording** (instead of D5). Would measure the control once and
attributably, but costs fleet realism in the other four recordings, and "the measurement is the
fleet, not the scanner" is `_flow_recordings`' stated design.

**A per-lane `intensity` dict** (instead of D6). This is what `FaultSpec.intensity`'s docstring
already refuses, and the refusal is right: the moment a fault carries per-lane knobs, the scenario
encodes lane-specific detail and the world/rendering separation collapses. The bug was the units,
not the arity.

## Consequences

**Every multi-lane number moves; single-lane numbers do not.** That asymmetry is the evidence the
change is what it claims to be. `skab_valve1` under `team3` + `prefix` still scores evF1 **0.55**,
the figure ADR-0027 recorded, because a single-lane corpus's per-lane counts *are* its pooled
counts. `device_trimodal` goes from 3 incidents to **7** (3 projections + 3 + 1 log decoy) over 3
faults, so every rate with an incident denominator is a different quantity.

**The corroboration A/B is coherent for the first time.** Both levels now move the same way:

```
                       evF1   evP    evR    ovl   ptF1
trimodal_telemetry     0.44   1.00   0.29   1.00  0.30
trimodal_all           0.82   0.70   1.00   0.95  0.65
```

Previously ptF1 *fell* 0.56 → 0.23 across those arms while evF1 rose. The per-lane lines explain the
telemetry arm's 0.29 in one glance — `flow=0.00, log=0.00, telemetry=1.00` — which is a coverage
fact, not a bad detector.

**`fleet_scan`'s point metrics are diagnosable.** Pooled ptF1 0.03 → **0.44**, flow-lane
P0.39/R0.51, `ovl` 0.11 → 0.39 (the mirror's recording-wide window is gone). Event precision *drops*
0.38 → 0.17, and that is an honest consequence of D5 rather than a regression: a bursting mirror
trips fan-out three times per recording instead of being continuously over the line and merging
into one episode. Three false alarms from a device deliberately configured to false-alarm is the
same claim, counted more finely.

**Bundles must be regenerated and SKAB re-fetched.** Both commands are in the CLI, and the
generated corpora's *readings* change this time (D5, D6, D7), unlike ADR-0027.

**An older `reports/evaluation.json` loses its count columns when re-read.** They are
`computed_field`s now, derived from `lanes`, which an old report does not carry. Old reports are
already incomparable — the metric definitions moved twice — and the dashboard's saved-report path
is an explicit fallback, so this is disclosed rather than mitigated.

**`Incident` is keyword-only.** The previous shape relied on `asset_id` being positionally first so
a stale call would bind a datetime into an asset field and fail loudly; that protects exactly one
refactor and this is the second. A plain dataclass validates nothing, so a mis-ordered positional
call would have bound a `datetime` into `lane` in silence.

**Deferred, deliberately.**

- `holdout`, still: a `CaseManifest` field plus a generator that emits a clean companion recording
  (ADR-0027 D9).
- **A fault whose signal is *absence* is inexpressible.** A row-clock answer key cannot label
  silence: an instant with no reading has no entry to mark, and every handler here is additive
  (telemetry diffs two fixed-length arrays; flow and log handlers return records to *add*). Sensor
  dropout and a device falling off the network are canonical IoT faults. Labelling one needs the
  *expected* clock stored beside the observed one — a fourth kind of truth, not a refinement of
  this one.
- `Clock.spread` still collapses silently when a burst is denser than its span (35 records across
  12 steps yields 12 distinct instants). The label is still exactly the instants that changed, so
  this is an authoring ergonomic rather than a correctness bug — but `intensity` and `length_steps`
  interact with nothing saying so.
- A scenario declaring two dialects of one lane is expressible, and `rng`'s lane-scoping was
  designed for exactly that parity test, but it remains unbenchmarkable: positional alignment would
  force a spec with two sources of one lane, and the pipeline would pool the same events twice.
- Two identical `FaultSpec`s in one recording still collapse to one `expressed` key while yielding
  two identical incidents.
- The vocabulary still collides: `entry` means four different things across `corpus.py`,
  `sweep.py`, `__main__.py` and the dashboard.
- Asset ids are still hand-copied from `scenarios.py` into the bench specs' `assets:` blocks.
