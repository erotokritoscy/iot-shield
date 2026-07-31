# The evaluation harness

Two commands answer the two questions somebody extending this repo has:

```
uv run pytest                          # does my adapter FIT?     (the port suites)
uv run python -m iotshield.eval ...    # is my adapter GOOD?      (this)
```

The port suite already told you your detector was legal. Nothing told you whether it worked.
This is that half.

Read `docs/guide/detectors.md` and `docs/guide/alerts.md` first. This guide assumes you know
what a lane is, what a `DetectionResult` carries, and how the alert engine buckets them.

## What makes a number here trustworthy

Two properties, both structural rather than promised.

**It measures the real pipeline.** `evaluate_case` does not re-implement anything. It composes
`pipeline.normalize_sources` and `pipeline.compute_result`, the exact two functions
`compute_run` composes, with a fitting step between them. A harness that scored a parallel
code path would eventually measure something the product does not do.

**It cannot write.** Every function here takes a `PipelineSpec`, which has no `database_url`
field at all, so twenty benchmark runs cannot leak synthetic alerts into an operator's triage
queue. That is enforced by the type checker, not by discipline.

The dependency arrow points one way: `eval` imports `pipeline`, and nothing in `pipeline`
imports `eval`. Delete the package and the product still builds. That is what makes it a
separable plane rather than a feature.

## Getting a number

```
uv run python -m iotshield.eval list
```

```
Corpora in .../data/eval:
  device_trimodal    3 recording(s)  One device observed on telemetry, syslog and flow at once...
  fleet_scan         5 recording(s)  Synthetic flow captures: a fleet with scanning...
  meter_fleet        4 recording(s)  Hourly utility metering on a water and an electricity meter...
  skab_clean         1 recording(s)  SKAB anomaly-free recording (no faults at all)
  skab_valve1       16 recording(s)  SKAB valve1 closure faults (one fault per recording)
  skab_valve2        4 recording(s)  SKAB valve2 faults (one fault per recording)
  synthetic         11 recording(s)  Synthetic rig telemetry: spike / drift / stuck faults...

Available to convert locally (not redistributed):
  SKAB - Skoltech Anomaly Benchmark (valve faults + anomaly-free) - run: python -m iotshield.eval fetch skab

Corpora and the shipped specs that fit them:
  --corpus device_trimodal  --spec config/bench/trimodal_all.yaml, config/bench/trimodal_telemetry.yaml
  --corpus fleet_scan       --spec config/bench/flow.yaml, config/bench/rules_flow.yaml
  --corpus meter_fleet      --spec config/bench/meter.yaml, config/bench/meter_team3.yaml
  --corpus skab_valve1      --spec config/bench/stub.yaml, config/bench/team3.yaml, ...
```

Then run one:

```
uv run python -m iotshield.eval --corpus skab_valve1 --spec config/bench/team3.yaml
```

`--corpus` and `--spec` are both required and **neither has a default**. That is deliberate.
They used to default to `synthetic` plus `team3.yaml`, which is the one pairing the docs say
cannot work: team3's shipped artifact is fitted to SKAB's eight channels and the synthetic rig
has none of them. So the shortest command in the documentation printed eleven rows of `0.00`,
which is exactly the failure the rest of this harness is built to prevent. Which pairs are
legal is *derivable*, so the answer to a missing flag is the table, not a guess.

The pairing table is derived, never declared. A spec's `sources` and a corpus's
`source_formats` must match positionally and by format, so the legal set is computable from
what both sides already publish. A hand-maintained `corpus:` field on each spec would say the
same thing while being free to drift.

A bench spec **is** a `PipelineSpec`, the same YAML you write for a run, minus two things: no
`database_url` or `sinks` (a benchmark computes rather than stores) and no source `path` (the
corpus supplies the content).

## Ground truth, and its coordinate system

Everything else in this package is machinery around one idea, so it is worth stating first.

**Truth is indexed by (asset, lane, instant), because that is the finest thing a detector can
claim.**

A detector's entire output is `DetectionResult(asset_id, lane, window_start, window_end)`.
Those are the only coordinates it speaks in, so the answer key's unit is *forced* by the other
side rather than chosen.

**Not a row.** A row count is a fact about an encoding, not about the world. SKAB's native
format is wide (one row carrying every channel) and our contract is long (one row per
reading), so the same recording is 1,148 rows in one and 9,184 in the other, with one fault
either way. A label per row would change size when somebody reformatted the file.

**Not finer than an instant.** `DetectionResult` carries no metric, and a multivariate
detector genuinely claims an instant rather than a channel. Scoring per channel would mark a
correct detection wrong for declining to localize.

**Per asset.** This model originally held one timeline and compared detections to incidents on
time alone. For a single-rig recording that is complete, because with one device time *is*
identity. A fleet destroys it: "something was wrong at 00:05" cannot distinguish a scanning
camera from the innocent gateway beside it, so an innocent device tripping a detector inside
the incident window scored as a correct catch.

**Per lane as well.** Same argument, one dimension further, and this one is not a refinement.
A pooled clock hands every lane to whichever samples fastest: one flow detection covering
sixty seconds gets charged sixty *telemetry* false positives, for instants it was never asked
about. Measured, point precision on the compromise case read 0.041, and the corroboration A/B
that corpus exists for showed as a collapse from 0.83 to 0.11.

### The roster, and the difference between innocent and unknown

Which assets appear in an answer key is a **declaration**, never inferred from the readings.
Three states, and the middle one is the point:

| State | Meaning |
|---|---|
| listed, with incidents | faulty in those windows, clean outside them |
| listed, with **no** incidents | **asserted clean**: every detection on it is a false alarm |
| **not listed at all** | out of scope; counted and reported, never scored |

Collapsing the last two would be the harness's cardinal sin. A bring-your-own bundle may
cover 500 hosts and label 3; treating the other 497 as clean publishes a precision number the
corpus never justified. A generated corpus is the opposite case: we built every device, so
every device is listed, which is what makes "that gateway should have stayed quiet" a
measurable claim.

## An incident is a projection, not an interval

Two decisions here, both of which changed published numbers.

**One incident per (fault, lane), sharing a `fault_id`.** A compromised camera is one cause
with several observable projections: it scans the network in bursts, fails an integrity check
whenever one runs, and warms up and stays warm. Those have different extents, and there is no
truer fourth interval behind them, because the cause itself is unobservable. A telemetry
detector cannot be blamed for missing the scan; it has no vocabulary for it.

An `Incident` used to carry a *set* of lanes, and that set had to be read two contradictory
ways by two callers: one read an empty set as "this fault reached every lane", the other as
"this corpus makes no per-lane claim, report nothing". One field, two opposite meanings. A
lane is an axis, not a set-valued attribute.

**The extent is a list of spans, not an interval**, because point and event scoring want
opposite things from the gaps. Point scoring asks "at instant t, was this asset faulty on this
lane?" and needs the gaps, because a quiet instant inside a fault genuinely is not faulty and
labelling it charges a detector for correctly staying silent. Event scoring asks "how many
distinct things went wrong?" and must ignore them.

The intermediate design split a fault's perturbed instants into contiguous runs and emitted
one incident per run. That is right about the instants and wrong about the *count*, and the
count is a denominator. How many pieces a fault falls into turns out to be an accident of
arithmetic: a fault on the log or flow lane **adds** records while normal traffic continues
underneath, so whether a normal event lands between two injected ones or on top of one decides
the fragment count. Measured on a syslog corpus, an identical 8-line error storm produced two
incidents in one recording and one in another, moving reported event recall from 0.50 to 0.60
for identical detector behaviour. The telemetry lane never shows it, because a fault there
*modifies* readings that already exist and nothing interleaves.

`Incident` is keyword-only for construction, and its validator refuses the one mistake the
field invites: `spans=((a, b))` without the trailing comma is a pair of datetimes, not a
one-element tuple of pairs. It also refuses an incident with no spans at all, which would flag
no instant, could never be caught, and would therefore be a denominator and nothing else.

### Two denominators, on purpose

| Term | Is | Divides |
|---|---|---|
| **incident** | one (fault, lane) projection | event-level recall |
| **fault** | one cause, keyed `(asset_id, fault_id)` | alert-level coverage and `alt/inc` |

The event level divides by projections, because a telemetry detector can only be asked about
the telemetry projection it could have seen. The alert level divides by faults, because an
analyst investigates the cause once and a correctly *corroborated* alert would otherwise read
as though it had split one incident three ways.

The report header shows both when they differ, which makes a corpus's multimodality visible:

```
3 case(s), 7 labelled incident(s) across 3 fault(s)   fit protocol: none
```

## The two levels of scoring

They answer different questions and must never be conflated.

- **Detector level.** Did it turn its observations into one finding per real fault? Forming
  judgments is part of detecting, so this is its own job.
- **Alert level.** Did the alert layer group findings *across detectors* into a sane queue and
  rank the real ones on top? A different job at a different altitude.

Neither implies the other, because the alert engine is a **compressor**: it buckets detections
by (asset, window), so a detector that fragments badly may still produce few alerts. Only
reading both reveals what correlation actually did, and only the pair distinguishes "the
detector fragments" from "the alert layer is fragmenting a clean detector".

The number that shows the two stages interacting is `compression_ratio`, scoreable detections
divided by scoreable alerts. Above 1 means correlation compressed; about 1 means it passed
through; **below 1 for a clean detector means the alert layer is splitting incidents rather
than merging them.**

It is deliberately not the quotient of the two `/inc` columns, because since the
projection/fault split those columns have different denominators. Dividing counts by counts is
the denominator-free reading.

### What a finding is matched on

A **detection** is compared to an incident on (asset, lane, time), the full set of coordinates
it declares. An **alert** is compared on (asset, time) only, because it carries no lane:
correlating across lanes is the thing it exists to do.

### The event rule is asymmetric, and that is what makes fragmentation visible

**Recall** credits an incident once, no matter how many detections hit it. **Precision** judges
every detection on its own.

So 91 fragments of one real fault score full recall and about 0.01 precision. That is the
analyst's experience (91 things to look at, one of them worth it), not the row-wise view that
would call it near-perfect.

### `ovl`: the dimension interval matching is blind to

Event precision asks only *whether* a detection touches an incident. So one window spanning
the whole recording scores a perfect row: precision 1.00, recall 1.00, latency 0s, one
detection per incident, and it carries no information at all.

`mean_true_detection_overlap` is how much of a true detection's window is actually fault
rather than padding. Forty seconds of real fault inside a 900-second window is 0.04. It sits
beside precision in the table because it qualifies it.

Measured, not refused. A wide window is sometimes the honest output of a detector that cannot
localize better, so the harness reports the width rather than rejecting it. A zero-width
window scores 1.0 when it lands inside a fault, because a point detection that hits is
entirely inside by definition.

## Everything is counted per lane, then pooled

`LaneCounts` is the real unit. Counts rather than rates, so pooling is addition: one case's
counts plus another's, one lane's plus another's, all the same operation. There is exactly one
place a rate is defined (`Counts.rates`), and the per-case column, the per-lane column and the
corpus headline all ask that same object. "The corpus number is the same rule one level up" is
true by construction rather than by two matching transcriptions.

A pooled point number is **comparable but not interpretable**. Two specs benchmarked on one
corpus share every denominator exactly, so the pooled figure is a fair head-to-head. What it
must not be is the only point number on screen, because its composition is set by sampling
rates. Hence the per-lane line under every spec:

```
  * per-lane event recall: flow=1.00, log=1.00, telemetry=1.00
  * per-lane point precision/recall: flow=P0.35/R1.00, log=P1.00/R1.00, telemetry=P1.00/R1.00
```

Note `flow=P0.35` against a pooled `ptF1 0.65`. Both are true; only the per-lane one is
diagnosable.

A lane the corpus declares but no detector covers still appears, with recall 0.00. That is the
honest answer to "did this run judge everything the corpus labelled?", and it is why the
scoring loop is driven by the answer key's lanes rather than by the detectors' lanes.

## Undefined is not zero

`ratio()` returns `None` when the denominator is zero, and the renderer prints `-`. This is
load-bearing, not defensive.

On a clean recording a detector that correctly emits nothing has no recall to report. Zero
would make it indistinguishable from one that missed everything, in the JSON a CI gate reads
and in the dashboard, not merely in a console table.

Four related rules:

- **`f1` propagates undefined-ness from recall only.** With no incidents there is no F1. But a
  detector that found nothing *while there was something* has recall 0 and undefined precision
  (it made no predictions), and that is a score of 0, not a non-answer.
- **`precision_at_k` is `None` when the queue is shorter than k.** Reporting 0 would read as
  "the top 10 were all wrong" when there were only 3 alerts.
- **Latency averages only over incidents actually caught.** Averaging a miss as infinitely
  late would conflate two different failures (missed it versus found it slowly), and recall
  already reports the misses.
- **`mean_lanes_per_true_alert` skips an alert whose detections cannot be resolved**, rather
  than counting it as zero lanes.

### And the consequence: the headline pools counts, it does not average rates

This is the subtlest thing in the module. Because a rate is legitimately undefined on a clean
recording, and averaging skips undefined values, a spec that stayed silent on the clean
recordings and one that fired four false alarms on them reported **identical** event and point
F1. Every shipped corpus includes clean recordings specifically to measure that, and the table
was structurally incapable of showing it.

Pooling fixes it because a false alarm on a clean case still lands in the *denominator* of
precision: there is no incident to recall, but there is a detection to be wrong about. It also
removes an unstated macro-average, where a 3-case recording weighed as much as a 300-case one.

The same reasoning applies to the absolute columns. `det` and `alerts` are corpus **totals**,
not per-case means. As means they needed decimals they did not have: a spec that fired on two
of four recordings has a mean of 0.5, which at zero decimal places formats as `0` (Python
rounds halves to even, so not even `1`). The headline read `det 0  alerts 0` for a detector
the table two lines below showed firing.

## The four ways to be nearly right

Each of these is a number that used to be wrong or absent, and each gets its own line in the
report rather than being folded into a rate:

| Field | Means |
|---|---|
| `misattributed_detections` | right time, **wrong device**. Under time-only matching every one scored as a correct catch. |
| `off_lane_detections` | right time, right device, **wrong modality**. The corpus says this lane showed nothing, so the detector agreed with a fault it could not have seen. |
| `unlabelled_detections` / `unlabelled_alerts` | on an asset outside the roster. Excluded from every rate, **never counted as false**. |
| `unlabelled_lane_detections` | on a lane the corpus keeps no clock for. Excluded in both directions. |

That last one is subtle: such a detection cannot touch point metrics (there are no instants to
be wrong about), so leaving it in event precision alone would have made it a silent one-sided
penalty.

Disclosure matters as much as exclusion. Twenty-four detections on an unlabelled host once
turned one correct catch into "25 detections per incident", printed on the same page as a line
promising those findings were excluded from every rate. `detections_per_incident` now divides
by the scoreable count like every other rate.

Asserted-clean assets get their own line, because a device that should have stayed quiet and
did not is the false-alarm rate a fleet-wide threshold has never had to answer for:

```
  * 3 of 6 asserted-clean asset(s) raised alerts
```

## Fit protocols

A detector that learns a baseline is only as good as the normal data it learned from, and a
baseline from a *foreign* recording was measured to fail outright: the threshold becomes
unreachable. So benchmarking a shipped artifact across a corpus of other recordings largely
measures **domain shift**, and reporting that as the method's score would be misleading.

Hence the protocol is an explicit, reportable choice:

| `--fit-protocol` | Does |
|---|---|
| `none` (default) | judge with the adapter exactly as configured, shipped artifact and all |
| `prefix` | relearn from a reserved head of each asset's own clock, which is then **not scored** |

`prefix` is what a real deployment does: learn this asset's recent clean history, then watch
what comes next. Running the two against each other is a real experiment. It answers whether
an artifact generalizes across recordings of the same rig, which decides whether the product
ships one artifact per device class or must fit per asset.

### The reservation is one object because it is one decision

What a protocol takes for training is exactly what scoring must not look at. `Reservation`
carries both ends: the per-asset cutoffs, and the answer key with the reserved rows removed.

Splitting them is how a leak got in. The rule used to be "everything strictly before this
asset's first incident", which sounds disciplined and is not: an asset with *no* incidents has
no first incident, so it contributed **100% of its events** to training and was then scored on
the very same rows. Measured on one fleet case, 1,515 of 1,580 scored events were also
training events, and all fourteen asserted-clean assets were trained on everything they were
judged against. So a detector that merely memorises normal reported a near-perfect false-alarm
rate on precisely the assets the roster exists to measure.

Reserving a fixed leading fraction (`TRAIN_FRACTION = 0.2`) and dropping it from the answer key
makes "clean history" and "test set" disjoint by construction rather than by luck of where a
fault landed. The reservation is also capped at the asset's first incident, because training on
a fault is the other half of the same mistake.

Four details worth knowing:

- **The cut is per asset, against that asset's pooled clock**, not per lane. A lane-specific
  cutoff would train the flow model on a window the telemetry model was judged on.
- **Detection still runs over the whole recording** (a windowed detector needs its warm-up).
  Only findings that reach into the scored region are judged, and a detection that fires
  *across* the boundary is kept and judged on the part that counts.
- **An asset whose reservation would be empty gets none** and is scored whole: a very short
  clock, or a recording that opens mid-fault. Both halves matter, because leaving it out of
  `cutoffs` while still training on it would restore the leak.
- **Events on assets outside the roster are kept for training.** No claim of a fault is no
  reason to withhold them, and they can never leak because they are never scored.

### Fitting is optional, never enforced

A detector that does not implement `Fittable` is evaluated as configured and the report says
so. Nothing fails. That is a deliberate product stance: a rule-based detector with no baseline
to learn is not a second-class detector.

Every fallback is reported rather than raised, and the reasons are distinguished:

- not fittable at all;
- fittable, but this case had no clean prefix;
- fittable, and it **refused** the data (team3 rejects a recording missing a pinned channel).

`prepare_detector` catches `TypeError` as well as `ValueError` on `fit`, because
`runtime_checkable` matches on method *name* only: an adapter with a sklearn-shaped
`fit(self, X, y)` satisfies the `isinstance` check and then fails on call. Neither may kill a
benchmark twelve cases in.

A detector the protocol could not fit and that has no trained state to fall back on is
**dropped rather than called**, because `detect()` would raise `NotFitted` and take the whole
benchmark down. What the reader needs instead is this case scored honestly at zero with the
reason attached.

The `fitted` column is `attempted`-aware: it reads "did every detector the protocol *asked* to
fit actually fit", not "did all of them". Asking it of every detector made one rule-based
control zero the count for a whole run, so a report reading `0/11` sat beside a score only a
successful fit could produce.

### There is no `holdout`

It was offered by the CLI, the API and the dashboard, and no bundle could ever satisfy it: the
case manifest has no field to carry a holdout recording, so the option could only print rows
of zeros and one line of explanation. An option that cannot succeed is worse than an absent
one. Reinstating it means a manifest field plus a generator that emits a clean companion
recording, which is a labels-schema change and belongs in an ADR rather than in a dropdown.

## Reading a real experiment

`device_trimodal` exists for one question: does cross-lane corroboration earn its keep? The
two shipped specs differ only in how many lanes they judge.

```
uv run python -m iotshield.eval --corpus device_trimodal \
    --spec config/bench/trimodal_all.yaml --spec config/bench/trimodal_telemetry.yaml
```

```
3 case(s), 7 labelled incident(s) across 3 fault(s)   fit protocol: none

spec                  detectors               evF1   evP   evR   ovl  ptF1  det/inc alt/inc cover   det alerts fitted
trimodal_all          stub_threshold,log_burst,f…  0.82  0.70  1.00  0.95  0.65     4.29    3.00  1.00    30      9    0/3
trimodal_telemetry    stub_threshold          0.44  1.00  0.29  1.00  0.30     2.29    0.67  0.67    16      2    0/3
```

Read across. Judging all three lanes catches **every** fault (`evR 1.00`, `cover 1.00`) where
telemetry alone catches under a third (`evR 0.29`, `cover 0.67`). Telemetry alone is perfectly
precise about what it does find (`evP 1.00`), and that is the trade: the three-lane spec pays
`evP 0.70` and, from its attribution lines, three of six asserted-clean assets raising alerts.

The per-lane lines say where the recall came from:

```
trimodal_all         per-lane event recall: flow=1.00, log=1.00, telemetry=1.00
trimodal_telemetry   per-lane event recall: flow=0.00, log=0.00, telemetry=1.00
```

And the telemetry-only spec's zeros arrive with their reason attached, from the compatibility
guard rather than from a human remembering to write a caption:

```
  ! ingest: source lane 'flow' has no detector that consumes it; those events will produce no detections
  ! ingest: source lane 'log' has no detector that consumes it; those events will produce no detections
```

The other shipped experiment is the fit protocol, on SKAB:

```
spec           protocol   evF1   evP   evR   ovl  ptF1  det/inc alt/inc   det  alerts fitted
team3          none       0.30  0.18  1.00  0.74  0.63     8.88    1.31   142      21   0/16
team3_scratch  prefix     0.55  0.38  1.00  0.97  0.69    17.00    1.62   272      26  16/16
```

Both find every fault. Refitting per recording roughly doubles event F1 and moves `ovl` from
0.74 to 0.97, meaning its windows sit far more tightly on the fault, while fragmenting about
twice as much. Which of those you want is a product decision the harness is not trying to make
for you; it is trying to make both visible in one table.

One caution if you read ADR-0021's own results table: those numbers were measured before the
scoring changes in ADR-0027 (pool counts, do not average per-case rates) and ADR-0033 (the
reservation rule), and they do not reproduce today. The ADR's headline finding that
per-recording fitting made team3 *worse* is an artifact of macro-averaging. Re-measure rather
than quoting any table older than the scoring rules it was produced under.

The third experiment exists because one arm is not a measurement. `config/bench/meter.yaml`
ran alone for a while, and its own header calls that a gap: the metering corpus had no floor
to compare against, because `stub_threshold` judges an absolute 80.0 while a meter draws well
under one unit an hour, so pairing them would have scored zero for a *units* reason rather
than a detection one. `config/bench/meter_team3.yaml` is an arm that can be asked the same
question of the same bytes, and the reason it can is the payload axis: `team3_zscore` consumes
the telemetry lane and no payload, so a meter bundle satisfies it, while `meter_iforest`
additionally reads the typed `MeterReading`. Both fitted per meter from that meter's own
reserved prefix:

```
uv run python -m iotshield.eval --corpus meter_fleet --fit-protocol prefix \
    --spec config/bench/meter.yaml --spec config/bench/meter_team3.yaml
```

| spec | detector | evF1 | evP | evR | det |
|---|---|---|---|---|---|
| `meter` | `meter_iforest` | **0.23** | 0.14 | 0.67 | 79 |
| `meter_team3` | `team3_zscore` | 0.16 | 0.09 | **1.00** | 319 |

The general-purpose detector finds *every* incident, which the purpose-built one does not, and
speaks four times as often to do it. So the forest buys precision rather than sensitivity,
which is a more useful answer than a win: it says what the extra machinery is actually for.
Both figures are modest, and that is the other half of the result. Hourly metering at these
operating points is a hard lane for either, and a table printing only the winner would have
hidden it.

## Sweeps

One spec in, N labelled specs out:

```
uv run python -m iotshield.eval --corpus skab_valve1 --spec config/bench/team3_scratch.yaml \
    --fit-protocol prefix --sweep window_size=20,60,120
```

```
team3_scratch[window_size=20]   0.52  0.36  0.94  0.98  0.73    21.50    1.75   344   28  16/16
team3_scratch[window_size=60]   0.55  0.38  1.00  0.97  0.69    17.00    1.62   272   26  16/16
team3_scratch[window_size=120]  0.52  0.35  1.00  0.91  0.67     9.19    1.50   147   24  16/16
```

Six times the window barely moves F1 (0.52 to 0.55) and more than halves fragmentation
(`det/inc` 21.50 to 9.19). That is the kind of thing a sweep is for: the headline metric is
insensitive and the column nobody would have looked at is not.

Sweeps are deliberately nothing more than spec-rewriting. `expand` produces the same
`list[(label, PipelineSpec)]` that `evaluate` already takes, so the harness, the report, the
console table and the dashboard render a sweep with no knowledge that one happened. Four
values are four ordinary rows.

A swept key is `[<detector>.]<param>`:

```
window_size                 every detector in the spec that declares it
team3_zscore.window_size    exactly that detector
```

The qualified form exists because a parameter name is only unique *inside* a detector, which
stopped being a corner case once one spec could hold several: `threshold` is 80.0 on the
telemetry lane and 20 distinct peers on the flow lane.

Everything incoherent is refused before anything runs, and validated against **all** the specs
at once, because sweeping one detector's knob while a rule-based baseline rides along as the
control is the normal shape of an experiment:

```
no detector accepts a 'nope' parameter; the specs declare: artifact, channels, fft_enabled, ...

sweep 'threshold' is accepted by more than one detector in a spec (stub_threshold,
flow_burst); it would vary all of them at once under one label. Qualify it to vary one:
stub_threshold.threshold, flow_burst.threshold

sweep 'team3_zscore.window_size' is ambiguous: 'team3_zscore' appears 2 times in a spec.
Put the instances in separate specs so each carries its own sweep.

this sweep expands to 25 runs per spec (limit 24); narrow the value lists
```

`MAX_EXPANSION = 24` is a cap rather than a warning because the caller is often a button, and
a UI that let a stray comma launch three hundred benchmark runs would be a
denial-of-service on its own server.

Sweeping a spec that names an artifact is refused by the adapter, not by the sweep:

```
error: invalid params for detector 'team3_zscore': cannot override ['window_size'] while
loading a trained artifact - its weights are aligned to the features its own hyperparameters
produced. It was fitted with:
      window_size: 60
      ...
```

That is the port law from the detectors guide showing up here: an artifact is atomic. Sweep
`team3_scratch.yaml` instead, which is the same spec with the `artifact:` line removed.

## A row of zeros always arrives with its reason

This is the harness's design goal, restated: the single most misleading thing it could print
is `evF1 0.00` for a detector that never ran, indistinguishable from one that ran and was bad.
Four mechanisms keep that from happening.

**1. Input alignment is refused, not scored.** Count and declared format, both:

```
error: case 'valve1-0' input[0] is 'stub-csv' but the spec's sources[0] declares 'flow-csv'
```

Feed stub CSV to a syslog spec and every line is quarantined as malformed, the detector sees
nothing, and the report reads like a detector that simply failed.

**2. Compatibility warnings are attached.** Alignment compares a case's inputs to the spec's
*sources*, which leaves one joint unchecked: a spec can be perfectly aligned and incoherent
downstream. Ingest flow and hand it to a telemetry detector and every input matches. The guard
already diagnoses that in words and was simply never asked; now it is, once per spec.

Reported, not refused. A detector that stays deliberately silent on a corpus is a legitimate
measurement, so the honest outcome is the zero, scored, with its reason attached.

**3. Ingest-quality warnings ride along.** A spec whose *params* quarantine every record
passes the format check, so `[input:...]` warnings from `normalize_sources` are carried into
the notes.

**4. `why_silent` is asked.** A fitted detector that declines a whole asset by design (team3
without that asset's channels, meter without its model) is otherwise indistinguishable from a
clean recording, and under the default protocol no fit-refusal note would explain it.

## The report is an artifact

The console table is the readable form; the JSON is the durable one.

```
uv run python -m iotshield.eval --corpus device_trimodal --spec config/bench/trimodal_all.yaml \
    --out reports/trimodal.json
```

A benchmark's value compounds when you can compare today's run to last week's, so the report
is a serializable document a CI gate can key into (`cases[].event_f1`). The dashboard's Evaluate
mode runs benchmarks through `POST /eval/run` and renders the report it gets back; where the
eval router is absent it falls back to a report JSON you upload, which is this same document.

Its shape is part of the contract, which is why `CaseReport` is flat rather than two nested
metric objects. Forty-two keys per case row, and three things worth knowing about them:

- **`pooled` and `pooled_by_lane` are computed fields on each spec.** A gate wanting the
  corpus-level figure would otherwise have to sum `cases[].*` and divide, which is a third
  place that knows how a rate is formed. Exposing the *result* of the one definition is not a
  second definition.
- **Lane-summed counts are derived, and still serialized.** `scoreable_detections`,
  `true_detections`, `incidents_caught` and the three point tallies are `computed_field`s over
  `lanes`. As stored fields they were a second copy free to disagree with the breakdown beside
  them; a gate still needs them in the JSON.
- **The models live in `eval/report.py`, not in `contracts/`.** `contracts/` is the *product's*
  vocabulary. Keeping the report out of it is itself the statement that the harness sits
  outside the product.

`--with-timeline` records where truth, detections and alerts actually fell, for charts. Off by
default because spans would dominate an artifact whose other job is to be small and diffable.
Capped at `TIMELINE_SPAN_CAP = 2000` spans, which is not arbitrary: Vega-Lite refuses to render
past 5,000 rows by default and a row-scoring detector produces over a thousand detections per
recording. Truncation is flagged rather than silent.

The timeline is drawn from the **scored** truth and findings, not the raw run, and one bar per
*span* rather than per incident. Otherwise the chart paints over the quiet stretches the point
metrics beside it are scoring as clean, and contradicts the table it is meant to explain.

## Exit codes, and why two of them differ

| Situation | Exit | Output |
|---|---|---|
| a benchmark ran | 0 | the table |
| `--corpus` or `--spec` missing | 2 | the pairing table |
| **unknown** corpus name | 2 | `error: unknown corpus 'nope' ...; available: ...` |
| **unavailable** corpus (fetchable, not fetched) | **0** | `skipped: corpus 'skab_valve1' is not in ...` |
| a bundle that will not load | 2 | `error: ...` |
| an incoherent sweep or spec | 2 | `error: ...` |

The third and fourth rows are the interesting pair, and they used to be the same. A typo and a
missing download both raised the same exception, the CLI treated it as a skip, and
`--corpus sythetic` printed nothing and returned **0**: a CI benchmark job with a mistyped name
passed green having measured nothing. The information to tell them apart was already at the
raise site (a fetchable name has a hint; an unknown one does not), and only the type was
missing, so `UnknownCorpus` subclasses `CorpusUnavailable`. The API's mapping of both to 404 is
untouched.

Exit 0 for an unavailable corpus is right: a fetchable dataset nobody has fetched is a normal
state on a fresh clone, and a CI job that benchmarks over optional corpora should stay green.

## Bringing your own

**A corpus** is a drop-in. Corpora are discovered, not registered: everything under the corpus
root carrying a manifest is a corpus. There is no registry to edit and no loader to write. The
root is `data/eval/`, overridable by `IOTSHIELD_CORPUS_PATH`, never by a caller-supplied value
(a request that names a path is a request to read arbitrary files). See
`docs/guide/corpus.md` for the bundle format.

**A detector** needs no harness change at all. Register it, write a bench spec naming it, and
`list` will show which corpora fit. If it implements `Fittable`, `--fit-protocol prefix`
exercises it.

**A metric** is four edits, in this order: a field on `LaneCounts` or `Counts` if it is a
count; a line in the relevant `rates()`; a field on `DetectorMetrics` or `AlertMetrics` with
the *same name*; and a field on `CaseReport`. `CaseReport.build` filters by `model_fields`, so
the copy is mechanical and a metric you declare on the dataclass but forget on the report is
silently dropped rather than crashing. Add a `_Column` if it belongs in the table.

Do not add a second place that forms a rate. `Counts.rates()` is the one definition, and the
per-case column, the per-lane line and the pooled headline all ask it.

**Over HTTP**, when `IOTSHIELD_EVAL_API` is enabled, the same harness is reachable at
`/eval/corpora`, `/eval/detectors`, `/eval/specs` and `POST /eval/run`. Unset, those routes do
not exist. See `docs/guide/pipeline-api.md`.

## Edge cases and traps

Things that cost real debugging, collected.

**Ingest is memoized across the specs of one call, and the key is subtle.** Ingest is pure and
depends only on the source chain, so a sweep of N values would otherwise re-parse the whole
corpus N times (measured about 3 seconds per extra value on SKAB's 163k rows) for
byte-identical output. Detectors are excluded from the key deliberately, since they consume
ingest's output and never influence it. Two bugs came out of that:

- Keying on the normalizer *name* alone was correct only while normalizers took no params. Once
  they did, two specs differing only in `home_networks` collided and the second was scored
  against the first's events. The key now includes the param fingerprint.
- The memo also carried `normalize_sources`' warnings, and lane-coverage warnings are computed
  **from the detector list**, which the key omits. So in a head-to-head the memo handed spec 2
  spec 1's warnings, and `trimodal_all` was annotated "source lane 'flow' has no detector that
  consumes it" beside its own `flow=1.00` recall. Only the `[input:` half comes from the memo
  now; the compatibility half is recomputed per spec.

**The reservation is memoized too**, for the same reason and with a simpler key: it depends
only on the case and the protocol, both invariant across the specs of one call.

**`Reservation.training` binds `labelled_assets` once.** It is an uncached property that
rebuilds a frozenset from every asset, and reading it inside the loop made the hot path of
every `prefix` fit O(events x assets): 370 ms against 28 ms over 163k events and 30 assets.

**Training selection happens once, not once per detector.** The choice depends on no detector,
so calling it inside the comprehension was a full scan of the recording per detector, and under
`none` a scan whose result was always discarded (18% of a trimodal run).

**`_Spans` exists because the naive version is quadratic.** A detector that does not merge its
own judgments can emit a detection per row, so scanning was measured at 10.5M comparisons over
one corpus. The same index serves both directions: detections merged to test row coverage, and
incidents merged to test whether a finding landed on a real fault (that second one was 54% of
`score_detections` on a 30-asset fleet).

**Per-asset false-detection indexes are built lazily.** On a 30-asset fleet with two faulty
hosts, eagerly indexing the roster builds 28 empty indexes per case, and a spec with no false
detections consults none of them.

**Overlap is closed on both ends, in exactly one place.** A point anomaly is a zero-length
window, so a half-open test would silently never match one. Both scoring levels need the rule
and a half-open edit in one of two copies would corrupt one metric while leaving the other
right.

**Incident spans are flattened, never hulled.** Merging `start`..`end` would re-fill the gaps
and turn every quiet instant a fault straddled back into a labelled one.

**Unlabelled alerts are removed before ranking, not scored either way.** Including one forces a
wrong answer (false is a lie about a device we never adjudicated; true is worse), and removing
it preserves the relative order of every alert that can be judged. On a fully labelled corpus
this is a no-op.

**`--corpus-path` works on either side of the verb, and that took work.** argparse parses a
subcommand into a fresh namespace and copies every attribute onto the real one, so any default
reachable from the subparser overwrites what the root parser stored:
`--corpus-path X list` silently used the default root while `list --corpus-path X` worked. The
fix is `default=argparse.SUPPRESS` with **no** default anywhere, and the fallback read in
`corpus_path_of` instead. An argument default does it, and so does `set_defaults` on the root
parser.

**A broken bundle or spec is listed with its reason, never hidden.** One unparseable file must
not make the other five unlistable, and omitting it silently is worse than either. On the
API path nobody asks for a spec by name (the endpoint publishes the parsed object), so a spec
an operator broke while the server was running simply vanished from the picker, and if it was
the only fitting one the page blamed the corpus.

**`generate` with no arguments rebuilds every scenario.** A regenerate after touching shared
machinery must not leave some bundles built by the old code and some by the new.

## Where to look

| | |
|---|---|
| Ground truth, the coordinate system | `src/iotshield/eval/truth.py` |
| Every metric, and the one place a rate is defined | `src/iotshield/eval/score.py` |
| Composing the real pipeline, the caches, the notes | `src/iotshield/eval/runner.py` |
| Fit protocols and the reservation | `src/iotshield/eval/fit.py` |
| The report models and the rendered table | `src/iotshield/eval/report.py` |
| Sweep grammar and validation | `src/iotshield/eval/sweep.py` |
| Corpus discovery and availability | `src/iotshield/eval/corpus.py` |
| Which specs fit which corpora | `src/iotshield/eval/bench.py` |
| The CLI | `src/iotshield/eval/__main__.py` |
| Shipped bench specs | `config/bench/` |
| The harness as a third plane | `docs/adr/0021-evaluation-harness.md` |
| Hyperparameters vs fitted state | `docs/adr/0022-hyperparameters-and-fitted-state.md` |
| Truth per asset, and the roster | `docs/adr/0026-multi-asset-ground-truth.md` |
| Pooling counts instead of averaging rates | `docs/adr/0027-measuring-what-actually-happened.md` |
| The coordinate system, lanes as an axis | `docs/adr/0028-the-answer-keys-coordinate-system.md` |
| An incident is a projection | `docs/adr/0029-an-incident-is-a-projection-not-an-interval.md` |
| The training leak, and the notes | `docs/adr/0033-pre-handover-review-fixes.md` |
