# 26. Multi-asset ground truth, and a scenario-driven corpus generator

Date: 2026-07-27

## Status

Accepted. Amends ADR-0021 D5 (the labels format) and D5b (the generator).

Numbering note: `docs/design/weeks-3-4.md` §7 provisionally reserved 0025/0026 for the
evidence-schema and scikit-learn records, but 0025 was taken by the pass-through normalizer.
Numbers are first-come, so this is 0026 and those two shift to 0027/0028.

## Context

The product judges three lanes. Only one had ever been benchmarked.

`flow_burst` ships with a fan-out threshold of 20 distinct peers per minute, chosen by argument
in ADR-0024. The only evidence for it was `data/demo/demo_flow.log` — a file we wrote, in which
the camera contacts 43 peers. **A demo you author yourself cannot falsify you.** The same holds
for the product's headline claim: `demo_trimodal`'s 44.1 → 76.4 corroboration lift is one
hand-built file, and ADR-0021 D7 says so explicitly, deferring the measurement until a
synthetic multimodal corpus exists.

This is the condition ADR-0021 was written to end. Its own Context opens with ADR-0020 shipping
on an unreproducible number and a factually wrong justification. We then shipped two more lanes
in exactly that state.

Two things blocked closing it, and only one of them was the generator.

**`eval/generate.py` could only produce single-asset telemetry.** One function (`_case`) fused
three concerns: what the world did, how it is written down, and what the answer key says. Every
extension hit all three at once, which is why it felt like patching rather than building.
`Recipe` had no way to describe a second device, and `_baseline` drew every channel from one
sequential RNG stream, so adding a device — or even a channel — would have shifted every value
after it and rewritten the whole corpus.

**`truth.Incident` had no asset dimension, and that is the load-bearing half.** Scoring matched
detections to incidents on **time alone**. For a single-rig recording that is complete, because
with one device time *is* identity. A fleet destroys it: an innocent gateway tripping a detector
inside the incident window scored as a **correct catch**, and every metric downstream inherited
the error. A flow corpus is mostly innocent devices, so a perfect multi-host generator would
have produced corpora the scorer silently mis-judged. Doing only the generator was not an
option.

## Decision

### D1 — Truth is per asset, and the roster is a declaration

`Incident` gains a required `asset_id` (plus descriptive `kind`, `fault_id`, `lanes`). A case's
answer key becomes a list of `AssetTruth` — one per device the corpus has an opinion about —
each carrying **its own row clock**, because the devices in one recording genuinely do not share
one: a rig samples every second, a network host emits a flow when it happens to talk.

Which assets appear is **declared**, never inferred from the readings, and there are three
states rather than two:

| State | Meaning | Scoring |
|---|---|---|
| listed, with incidents | faulty in those windows, clean outside them | full precision/recall |
| **listed, no incidents** | **asserted clean** — every detection is a false alarm | counts against precision |
| **not listed** | out of scope; the corpus makes no claim | excluded from every rate, counted separately |

Collapsing the last two would be this harness's cardinal sin. A bring-your-own bundle may cover
500 hosts and label 3; treating the other 497 as clean publishes a precision number the corpus
never justified. A *generated* corpus is the opposite case — we built every device, so every
device is listed, which is precisely what makes "that gateway should have stayed quiet" a
measurable claim.

No manifest flag was needed. The clock's presence *is* the statement, so the data model carries
the meaning.

### D2 — Scoring matches on (asset, time), and reports what that changes

An incident is hit only by a finding **on its own asset**; a finding is true only if it overlaps
an incident on **its own** asset. Five reported numbers follow, each previously wrong or absent:

- `misattributed_detections` — right moment, wrong device. Under time-only matching every one
  of these scored as a catch. It is reported rather than merely counted wrong because "the
  detector is noisy" and "the detector fired on the wrong thing" need different fixes.
- `clean_assets` / `clean_assets_detected` / `clean_assets_alerted` — the fleet-scale
  false-alarm rate a fleet-wide threshold has never had to answer for.
- `unlabelled_detections` / `unlabelled_alerts` — what was set aside, so a partially labelled
  corpus cannot quietly inflate a rate. Unlabelled alerts are *removed before ranking* rather
  than scored either way: counting one true is a lie about a device nobody adjudicated,
  counting it false is worse, and removing it preserves the order of every judgeable alert.
- `lane_recall` — per-lane event recall over the incidents each lane is *expected* to express.
  Empty for a converted corpus, which declares no lanes: "the flow lane found nothing" and
  "nobody claimed it should" must not read alike.
- `mean_true_priority` / `mean_false_priority` / `mean_lanes_per_true_alert` — what the
  corroboration A/B reads. Still **discrimination, not calibration** (ADR-0021 D7): comparing
  true against false, or one arm's true-alert scores against another's over identical evidence,
  never claiming a score is correct.

`fit.normal_prefix` becomes per asset too. A single corpus-wide cutoff at the earliest incident
anywhere would truncate every *other* device's clean history there — on a fleet where one host
is compromised early, almost the entire corpus. Latent today (no non-telemetry detector is
`Fittable`), wrong regardless.

**The safety argument:** on a single-asset corpus with a one-entry roster, every new formula
reduces to the old one exactly. Verified, not assumed — see Consequences.

### D3 — Labels v2 on disk, and v1 is rejected rather than upgraded

Per-asset clocks and intervals, with `"incidents": []` as the asserted-clean statement, readable
by eye under `gzip -dc`. No size cost: the per-asset clocks sum to the row count the single
clock held.

A v1 file is **rejected with the command to regenerate**, following the team3 artifact
precedent, and for a sharper reason: an upgrade would have to *invent* an asset id, and the only
place to find one is the readings — which is exactly what this format exists to avoid needing.
A guessed asset id is a number nobody measured. Every bundle in existence is either generated or
fetched, so regeneration is one command.

### D4 — The generator becomes a world, two ports, and a builder

A recording is not a file. **A recording is a small world**: some devices, over some stretch of
time, some of which go wrong. Files are a *rendering* of that world.

| Module | Owns |
|---|---|
| `world` | `Clock`, `Device`, `FaultSpec`, `Recording`, `Scenario`. No formats, channels, or IPs. |
| `port` | `Behaviour` (a lane's normal + what a fault does to it) and `Renderer` (events → wire text) |
| `build` | simulate → render → **derive** the answer key |
| `rng` | one private stream per thing generated |
| `faults` | a fault is a name plus which lanes can show it |
| `scenarios` | the shipped corpora, as data |

Extension points, and what each costs: a **lane** is one `Behaviour` + one `Renderer` + two
registry lines; a **wire dialect** is one `Renderer`; a **fault shape** is a `FAULTS` entry plus
a handler in the behaviours that should show it; a **corpus** is a `Scenario`. Nothing else, in
any case.

Three sub-decisions are load-bearing.

**The intermediate representation is `NormalizedEvent`** — the product's own contract, which
already carries all three lanes (`severity` for logs, `metadata` for a flow record,
`metric`/`value` for telemetry). That makes a renderer the formal **inverse of a normalizer**,
which is *checkable*: emit the text, run the real parser, confirm we get back what was claimed.
The idea was already in the codebase as a comment — `to_stub_csv` called itself "the inverse of
that normalizer's fan-out" — and was never promoted to a concept.

**Rendering is separate from behaviour**, because dialect multiplicity is measured rather than
speculative: six wire formats already exist across three lanes. "How a pump behaves" and "which
dialect it is logged in" are different questions.

**A fault owns no rendering.** The tempting design has a fault know how to damage each modality,
and its cost is fatal: adding a fourth lane would mean teaching *every existing fault* about it.
Inverted, each behaviour decides what a fault name looks like in its own world — and reports
back which faults it actually perturbed, which is how an incident's `lanes` becomes a derived
fact. A behaviour with no vocabulary for a fault expresses nothing, and that is correct: a
compromised camera really does still read 22 °C (ADR-0024 D2).

### D5 — Determinism by name, never by sequence

Every random stream is derived from *its own name* (scenario, recording, device, lane, purpose)
via `blake2b`, so adding a device changes only that device's bytes, adding a lane only that
lane's file, and adding a recording only that recording's. Reordering devices changes nothing,
because the builder sorts before rendering.

**Never `hash()`.** Python randomizes string hashing per process, so a seed derived from it
would produce different data on every run — silently destroying the one property a committed
corpus must have. Pinned by a subprocess test across two `PYTHONHASHSEED` values.

This pays off twice: it is also what lets one lane's payload be byte-identical across two
bundles, so a cross-corpus comparison is apples-to-apples by construction.

### D6 — Four invariants refused at generation time

Each would produce a corpus that *looks* fine and scores wrong, so each raises rather than
ships: a fault aimed at a device not in the recording; a device that emitted nothing (it cannot
be in the roster, so findings on it would be unscoreable rather than false); a fault **no lane
expressed** (a label nothing can see drags recall down for a reason no reader could diagnose);
and a behaviour producing events for a device other than its own.

The asset-identity invariant is the practical one. The flow normalizer decides which endpoint is
"the asset" from `home_networks` (ADR-0023 D2), so a multimodal device named outside those
networks would have its events attributed to the *peer* — three lanes that never correlate, a
corroboration factor pinned at 1.0, and a corpus that looks fine. The round-trip test catches it
at generation time instead.

### D7 — RFC 5424, not RFC 3164, and it is not a style choice

`normalize.syslog` resolves RFC 3164's missing year with `datetime.now(UTC).year` — the only
defensible choice when reading a live log. A *corpus* whose labels are pinned at a fixed epoch
and whose payload is RFC 3164 would parse into the wrong year the moment the calendar turns
over: every timestamp would miss every labelled interval, every LOG-lane benchmark would score
zero, and nothing in the output would say why. RFC 5424 carries a full offset-bearing timestamp.

Found by writing the round-trip test, not by reasoning. It is the clearest argument for D4's
inverse-of-a-normalizer framing: the bug was undetectable by inspection and impossible to ship
once composition was checked.

### D8 — Two new corpora, built to be falsifiable

**`fleet_scan`** — a 15-to-16-device fleet across five recordings. The measurement is *the
fleet*, not the scanner:
catching a sweep is easy. It contains the two controls `demo_flow.yaml` claims and never
measured (a high-volume single-destination gateway; an inbound-heavy server), a chatty device
just *under* the 20-peer boundary, and one deliberately *over* it. Plus a `port_sweep` recording
— the shape `fan_out` is structurally blind to, the flow analogue of a stuck sensor — an
`exfiltration` recording, and a clean recording.

**`device_trimodal`** — one device on telemetry, syslog and flow at once, with **single-lane
decoys**. That design point is not decoration: corroboration *multiplies* an alert's priority
and never creates one, so the only metrics that can move are the ranking ones. With one device
and one incident the true alert is rank 1 in both arms and the corpus would pass its own test
while measuring nothing.

Both bench specs register **no assets at all**. ADR-0021 D7 warns that `asset_weight` is
circular; with nothing registered every device auto-discovers at the same weight, so a ranking
movement cannot be our own thumb on the scale. A test asserts the block stays absent.

The corroboration A/B is **two specs over one bundle**, differing only in the detector list —
not two bundles. A case's inputs align positionally with a spec's `sources`, so both arms
declare all three; arm A judges one lane and ignores the other two. The control is then the
*same bytes* rather than two files a test must prove identical, both arms land in one report
table, and the ingest cache makes the second nearly free.

## Consequences

- **Every published single-asset number reproduces exactly.** SKAB `valve1`: `team3_zscore`
  0.73 / 0.69 / 1.00 / 0.68, det/inc 8.88, alerts/inc 1.50; under `prefix` 0.47 / 0.37 / 0.81 /
  0.46; `stub_threshold` 0.52 / 0.35 / 1.00 / 0.52, det/inc 1135. On `skab_clean`, 1 false alert
  against 34. All identical to ADR-0021. That is the safety net for a breaking scorer change,
  demonstrated rather than argued.

- **The telemetry corpus keeps its discriminating property and changed its bytes.** Recall 1.00
  on spike and drift, **0.00 on all three stuck**, no false alarms on clean, headline event F1
  0.67 — unchanged. Per-case detection counts moved slightly (60 → 62, 70 → 67) because the RNG
  is now scoped per channel. Disclosed, test-pinned, and a one-time cost.

- **The flow lane's threshold is no longer a judgment.** First measured sweep on `fleet_scan`:

  | `fan_out` threshold | event F1 | event P | event R |
  |---|---|---|---|
  | 8 | 0.29 | 0.17 | 0.50 |
  | 15 | 0.29 | 0.17 | 0.50 |
  | **20 (shipped)** | **0.37** | **0.23** | **0.50** |
  | 27 | **0.50** | 1.00 | 0.50 |
  | 35 | 0.42 | 1.00 | 0.38 |

  **The shipped 20 is not the best operating point on this corpus**; 27 holds recall and takes
  precision to 1.00. Stated with its caveat: the corpus contains a chatty device at 26 peers by
  our own choice, so this shows *that a fleet-wide constant has a cost and roughly where it lies
  for this fleet* — not that 27 is universally right. What is not our choice is the shape: a
  single constant cannot separate a scanner from a busy server, which is what ADR-0024 D7's
  deferred per-asset baseline exists to fix.

- **ADR-0024's predicted false alarm is now a number.** Its Consequences said a fleet-wide
  threshold "will produce false positives on genuinely chatty devices (a NAT gateway, an update
  mirror)". On `fleet_scan`, asserted-clean assets do raise alerts, and the harness reports how
  many. The two claimed controls hold: neither the volume gateway nor the inbound-heavy server
  is called a scanner.

- **Misattribution is real, not theoretical.** On `scan-one`, detections fire at a faulty moment
  on a *different* device. Under the old scoring those counted as correct catches, taking event
  precision from a measured 0.23 to a fictional 1.00.

- **Corroboration is a benchmark for the first time.** On the `compromise` case, over
  byte-identical evidence: true-alert priority **74.90 → 86.74**, backed by **1 → 3 lanes**;
  incident coverage 0.75 → 1.00; and in the three-lane arm true alerts score 86.74 against false
  ones at 17.70. The cost is visible too — event precision 1.00 → 0.65, because `flow_burst`
  raises the chatty-mirror false alarm. ADR-0021 D7's deferral is discharged.

  Stated honestly: the `volume` factor rides along with corroboration and today's engine cannot
  separate them, so this measures the **end-to-end product claim** ("judging more modalities
  improves the queue"), not the isolated coefficient.

- **The Evaluate page would have shipped broken.** `dashboard/app.py`'s bench spec hardcoded a
  single stub-csv source, so a multi-lane corpus would appear in its picker and be refused on
  run. Corpora now publish their input shape in `CorpusInfo`/`CorpusSummary`, the page composes
  one source per position resolving each normalizer by capability, and a test asserts every
  discoverable corpus composes a spec that survives alignment. This retires the "hardcoded
  normalizer" deferral that file already named.

- **A mixed-shape corpus is refused at load.** Cases must supply the same formats in the same
  order, or no single spec can benchmark it; finding that out case-by-case mid-run is worse than
  one message at load time.

- **The committed corpora are now checked against the code.** Nothing previously verified that
  what is committed equals what `build` produces — `test_corpus` pinned write-then-rewrite
  idempotence, a different claim. Total committed corpus size is 605 KB against a 4 MB budget,
  also test-pinned.

- **A test caught the first real mismatch immediately.** A test helper labelled `pump-02` while
  its readings described `skab-testbed`; under time-only matching that was invisible, and under
  per-asset matching it is a hard zero. Exactly the class of silent error D1 exists to remove.

- **`flow_burst`'s `Fittable` is *partially* unblocked, not unblocked.** ADR-0024 D7 gave three
  structural reasons; `fleet_scan` removes the first ("there is no caller — there is no labelled
  flow corpus"). Reason 2 — a contaminated baseline fails sharply here and we could not detect
  it — is untouched by a corpus. Sequencing it is a separate decision.

- **`score_alerts` gained a parameter** (`detections`, optional) and it is exported from
  `eval/__init__`, so this is a public break for the harness plane. `Incident` gained a required
  first field, breaking nine construction sites — all loud, all mechanical.

## Deferred, each with a trigger

| Deferred | Trigger |
|---|---|
| `zeek-conn` rendering of the same scenarios, plus a dialect-parity test | a claim about dialect parity, or a third flow dialect |
| A `normalized-jsonl` renderer, so a lane can be benchmarked before it has a parser | a modality whose wire format we do not model yet |
| Scenarios as YAML rather than Python | the first scenario contributed by someone who does not otherwise touch our source |
| Concurrent faults on one asset (the model allows it; no shipped recording uses it) | a detector whose evidence claims to separate concurrent faults |
| Holdout streams persisted in a bundle (`Case.holdout` exists but is never written) | a `Fittable` detector on a non-telemetry lane |
| Asset aliasing (hostname ↔ IP). The trimodal device uses an IP on all three lanes, as `demo_trimodal.yaml` does, and generation now *refuses* if the lanes disagree | ADR-0023's own trigger: a corpus that must correlate a hostname-keyed source we do not control |
| A CI gate on report deltas | two consecutive releases where a regression was caught by hand |
| Calibration / ideal-ordering truth (ADR-0021 D7, unchanged) | analyst feedback |
| LEAD and other third-party corpora (`weeks-3-4.md` §5.5, Bucket C) | Panayiotis's module landing; a `fetch.DATASETS` entry, untouched by this design |

## Amendment — the corpus drives *both* halves of a composed spec

The original change taught the Evaluate page to derive a spec's `sources` from
`CorpusInfo.source_formats`. That was half the job, and shipping half of it was worse than
shipping none: the page then offered flow and trimodal corpora it could not compose a working
spec for. Three defects, found by running the dashboard against `fleet_scan`.

**A1 — Eligibility is the corpus's lanes, not a hardcoded lane.** The detector picker filtered
on `Lane.TELEMETRY`, so `flow_burst` and `log_burst` were unreachable from the dashboard for
every corpus, and the two corpora D8 added could not be benchmarked from the UI at all.

`/eval/corpora` now publishes `lanes` beside `source_formats` — which detector lanes the corpus
can feed, given the registered normalizers. **Computed server-side and decoded**, for the same
reason `DetectorSummary.lanes` is: the `lane:` prefix is a private detail of `capabilities.py`,
rendered only at the API boundary, so a client deriving this join for itself would have to parse
that prefix. Both sides of the comparison arrive in one vocabulary and the page intersects them.

This is the one place the implementation departed from its plan, which had called for extracting
the Compose page's equivalent filter into a shared helper. The two pages legitimately work in
different vocabularies — Compose joins `/modules` tokens to `/modules` tokens, Evaluate joins
`/eval` bare lanes to `/eval` bare lanes — and forcing one helper across both would have pushed
prefix knowledge into the client to save four lines.

**A2 — A silent zero is worse than a refusal.** The mismatched spec was not rejected: it ran and
reported `evF1 0.00` across five recordings, indistinguishable from a detector that ran and was
bad. `_check_alignment` cannot see it, because the sources genuinely *do* align — the incoherence
is one joint further along (ingest FLOW, judge TELEMETRY).

`evaluate` now consults `pipeline/compat.py`, whose warnings already described the failure in
words and which the eval plane had simply never asked. They land in the report's existing
per-case `notes`, so every client is covered at once and no schema changed:

```
! detectors[0]: detector 'stub_threshold' consumes lane(s) telemetry but no configured
  source produces them (flow); it will not fire
```

Reported, **not refused**: a detector that stays deliberately silent on a corpus is a legitimate
measurement — it is how `severity_spike` shows it sees nothing in a burst of INFO lines — so the
honest outcome is the zero, scored, carrying its reason. ERROR-level issues are left alone;
`build_detectors` already fails on those with its own message.

**A3 — The entry, not the detector, is the unit of comparison.** The page built exactly one
detector per spec. That made head-to-head easy and made the other experiment *inexpressible*:
detectors in one spec pool their detections into a single alert-engine run, which is what
produces corroboration. With one detector per spec every alert cited one lane by construction, so
the headline result of this very ADR could not be reproduced from the UI at any setting. Measured
on `device_trimodal`:

| shape | lanes per true alert | mean true priority |
|---|---|---|
| each detector alone (every shape the UI could express) | 1.00 | 74.90 / 62.96 / 28.95 |
| all three in one entry (`config/bench/trimodal_all.yaml`) | 3.00 | 86.74 |

An *entry* now holds one or more detectors and becomes one spec, one report row. One detector per
entry is the old behaviour; several is a multi-lane pipeline. Nothing is combined on the user's
behalf — cross-producing selections would be neither meaningful nor extensible. Two consequences
fall out: the sweep menu moved from the detector to the entry (which knobs are legal depends on
the whole set, mirroring `sweep.py`'s existing refusal of a name two detectors in one spec
accept), and widget keys became entry-scoped, so the same detector can sit in two entries under
different params.

**What let all three survive:** the test that looked like the guard against exactly this
(`test_every_discoverable_corpus_composes_a_spec_that_survives_alignment`) hardcoded
`stub_threshold` and checked only `_check_alignment`, so it passed green while the page composed
a flow corpus into a telemetry detector. A second test *asserted* the defect
(`assert "log_burst" not in options`) under a comment that had stopped being true. Both axes are
now discovered, and coherence is asserted via the guard rather than alignment alone.
