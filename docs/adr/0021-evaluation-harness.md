# 21. Evaluation harness

Date: 2026-07-19

## Status

Accepted.

## Context

The project's original purpose was "a platform for researchers to **plug in and evaluate**
detection modules". We built *plug in* to a high standard — four ports, a capability guard,
a registry, a port suite every adapter must pass. We never built *evaluate*. A researcher
who adds an adapter is told whether it **fits**; nothing tells them whether it is **good**.

Two concrete failures made that gap load-bearing rather than theoretical:

1. **ADR-0020 shipped resting on an unreproducible number.** Its D4 cited team3's
   IsolationForest scoring event-F1 0.047 (91 predicted events for one SKAB fault) as the
   reason to merge burst episodes. That number came from a throwaway script that no longer
   exists. A committed architecture decision rested on a measurement nobody could re-run.

2. **ADR-0020's stated justification was factually wrong**, and only measuring it revealed
   that. "A 90-attempt brute force is one alert, not 90" is false: unmerged, the alert
   engine's bucketing collapses those 86 detections into one alert anyway. The merge is
   still right, for a different reason (corrected in ADR-0020 D4) — but we had been
   reasoning about our own system's behaviour without checking it.

Everything below follows from one property of the new ingredient. **Labels only exist in a
lab.** A detection product exists because nobody knows where production anomalies are; a
labelled corpus is recorded by whoever broke the equipment on purpose. That single fact
determines where the harness lives, what it can measure, and what it must refuse to claim.

We have the labels: SKAB's `valve1`/`valve2` carry a per-row `anomaly` column — 20
recordings, **exactly one fault each**, which is an unusually clean benchmark shape.

## Decision

### D1 — A third plane, outside the product

`Monitor` and `Operate` *use* the pipeline; evaluation *judges* it, the way a test harness
sits above the code under test. It asks a different question ("which pipeline should I
ship?"), over different data (labelled corpora, not production), for a different audience
(researcher, not analyst), and it is the first thing you would drop from a product image.
So it is a third plane, not a panel inside Monitor — and it **cannot** run on production
data at all, since there is nothing there to compare against.

Concretely:

- `src/iotshield/eval/` imports the pipeline; **nothing in the pipeline imports it.** The
  one-way arrow is what makes it separable rather than a feature.
- The dashboard gains an `Evaluate` mode behind `IOTSHIELD_EVAL_UI`, **default off** — the
  same env-injection pattern as `IOTSHIELD_DATABASE_URL`. An on-prem customer never sees it.
- **A benchmark never writes to a sink.** Twenty SKAB runs dumping synthetic testbed alerts
  into an operator's triage queue must be impossible, not merely discouraged. It is enforced
  by *types*: `compute_run` and the compatibility guard now take `PipelineSpec`, which has no
  `database_url` field at all, and `load_spec` loads a bench spec without one.
- The report models live in `eval/report.py`, **not** `contracts/`. `contracts/` is the
  product's vocabulary; a benchmark report is the harness's.

### D2 — Two levels of metric, read as a pair

Scoring happens at two altitudes, and conflating them is the failure mode:

- **Detector level** — did it turn its observations into one finding per real fault? This is
  genuinely its own job, because forming judgments is *part of* detecting: a collective
  anomaly (twelve failed logins in a minute) has no per-row label to emit, since no
  individual row is anomalous.
- **Alert level** — did the alert layer group findings *across detectors* into a sane queue,
  and rank the real ones on top?

Neither implies the other, because **the alert engine is a compressor**. Measured on our own
code: 91 fragments of one fault become 4 alerts; 86 become 1. So the pair yields a third
number the levels cannot give alone — the **compression ratio** — and only the pair
distinguishes "the detector fragments" from "the alert layer is fragmenting a clean
detector".

Within the detector level, **event** metrics headline over **point** metrics. Point scoring
walks rows and is what published benchmarks report; event scoring collapses label runs into
incidents and credits each incident once, so 40 fragments of one fault score full recall and
collapsing precision. Event scoring matches what an analyst experiences. Both are reported.

**An undefined rate is `None`, never `0.0`.** A rate with no denominator — recall on a clean
recording, precision with no predictions — is *not applicable*, and collapsing it to zero
would make a detector that correctly stayed silent numerically identical to one that missed
everything. That matters beyond the console: the JSON report is what a CI gate keys into,
and a gate that cannot separate the best outcome from the worst is worse than none. `None`
is already the harness's word for this (precision@k on a short queue, latency with no
catch) and the renderer already prints it as `-`, so the rule is consistent rather than
special. Averages skip undefined cases, so one clean recording in a mixed corpus cannot drag
every rate down.

Also measured: detection latency, detections-per-incident (fragmentation), alerts-per-incident,
incident coverage, precision@k over the ranked queue, mean rank of true alerts, and
false-alarm counts on clean recordings.

### D3 — An optional `Fittable` capability; fitting is dev-time only

A z-score detector must know what "normal" is before it can judge, and ADR-0012 D3 measured
that a baseline learned from a *foreign* recording fails outright. So benchmarking our
shipped artifact across a corpus of other recordings largely measures **domain shift** —
publishing that as the method's score would be misleading.

`Fittable` is therefore a new **optional** port capability: `fit(events) -> Detector`.

- **The pipeline never calls it.** No `fit` in `compute_run`, the Pipeline API, or the CLI.
  This is why it is not a contract change: the product's runtime is untouched.
- **It returns a new detector rather than mutating**, so the port's determinism law survives
  (no hidden fitted state) and one case's fit cannot leak into another's.
- **Not implementing it is not a defect.** `stub_threshold` and `log_burst` have no baseline
  to learn and remain first-class detectors. The harness checks `isinstance(d, Fittable)` and
  falls back to evaluating as configured, **reporting that it did so** — it never fails a run.
  Every other degradation (no clean prefix, no holdout, a detector refusing its training
  data) is likewise a reported note, not an exception.
- Only the *baseline* is relearned; hyperparameters belong to the adapter's construction.

### D4 — The fit protocol is an explicit, reported choice

`none` (as configured) · `prefix` (relearn from the case's own events before its first
incident — `train.py`'s rule, generalized, and what a real deployment does) · `holdout`
(a separate clean recording).

Making it a switch rather than a mode turns it into an experiment: running `none` against
`prefix` on one corpus answers whether an artifact generalizes across recordings of the same
rig, which decides whether the product ships one artifact per device class or must fit per
asset. See Consequences — the answer was not what we expected.

### D5 — Corpora are *data on disk*, discovered — not code

**Amended 2026-07-20.** This decision originally made `CORPORA` a name→loader dict in Python,
mirroring `DETECTORS`. That shape had two faults, and the second is disqualifying for what
this repository is for.

**Adding a corpus required writing code.** A loader function plus a registry entry is a
contribution to our source tree; a labelled dataset is data. Making a researcher edit Python
to register data is the difference between a harness they can extend and one only its author
can.

**No corpus actually lived in the repository.** The loaders read `../reference/`, outside
the repo. Post-internship a researcher clones `iotshield/` alone — so of four corpora, three
were unreachable and the fourth was a 300-row slice. The harness worked on one machine.

So a corpus is now a **bundle**: a self-describing directory under `data/eval/` holding a
manifest, gzipped readings in a real ingest format, and an answer key. Everything under the
corpus root with a manifest *is* a corpus — no registry, no loader, no import. Adding one is
a drop-in, and the CLI, API and dashboard all see it with no change to any of them.

Three consequences worth stating:

- **Labels are intervals plus the row clock**, not per-row booleans (`GroundTruth.from_incidents`).
  Smaller — measured, uncompressed labels were *larger* than the readings they annotate — and
  strictly more expressive: two incidents that touch are one run of flags but two intervals.
- **Readings stay real text**, so a benchmark exercises normalization and measures the
  *pipeline*, not a detector in isolation. Each input is **tagged** with its `source_format`
  and checked against the spec's `sources`, for the same reason ADR-0019 routes by explicit
  tags: an untagged mismatch is not an error but a *misleading number*, since the wrong
  normalizer quarantines every line and the report then reads exactly like a bad detector.
- **The root is `IOTSHIELD_CORPUS_PATH`-overridable**, defaulting to the repo's own — the
  same 12-factor pattern as `IOTSHIELD_DATABASE_URL`, and never caller-supplied, because a
  request naming a path is a request to read arbitrary files (ADR-0018 §9-A).

#### D5a — Three tiers, because licensing decides what may ship

The blocker was not size (SKAB's labelled subsets are 599 KB gzipped — smaller than CSVs we
already commit). It is that **SKAB is GPLv3**: copyleft, with share-alike obligations on
derivative works, and our labels derive from its `anomaly` column. This repository declares
no licence and its stated direction is a commercial product, so committing those bytes would
bake a licensing question into git history, where it is expensive to undo.

| Tier | How it arrives | Committed? |
|---|---|---|
| **Ours** | `data/eval/synthetic`, built by `iotshield.eval generate` | ✅ |
| **Third-party** | `iotshield.eval fetch skab` converts a local checkout into bundles | ❌ gitignored |
| **Bring-your-own** | drop a bundle in the corpus root | n/a |

A manifest **must** carry `provenance` and `licence` — a bundle that cannot say where it came
from is one nobody can safely reuse, which is exactly the problem this tiering solves.

#### D5b — A synthetic generator, so a fresh clone has a real benchmark

Without one, a cloner gets no substantial corpus at all. Anomalies injected by construction
*are* the labels, so there is no annotation step and no licence question, and it does things
a fixed dataset cannot: arbitrary size, and fault shapes chosen to *discriminate* rather than
whatever a testbed happened to do. Three shapes ship, and the third is the point:

- **spike** — a violent excursion. Any threshold catches it; it is the floor.
- **drift** — a slow ramp ending past the threshold but never locally sudden, so a fixed rule
  catches it *late*. This is where latency separates detectors a hit/miss score would tie.
- **stuck** — the sensor freezes at a plausible value. In range the whole time, so a fixed
  threshold is **structurally blind**; only a detector that models normal can see it.

Measured on the shipped bundle, `stub_threshold` scores recall 1.00 on spike and drift and
**0.00 on all three stuck cases** — the corpus discriminates by construction, and that is
pinned by a test.

The output is written once and committed, like the team3 artifact (ADR-0012) and for the same
reason: generated inputs must be *fixed*, or every benchmark silently measures a different
dataset. Writes are byte-reproducible (gzip `mtime=0`), so regenerating is not a diff.

### D6 — CLI-first, and (amended) a gated API + a dashboard that can run one

The researcher's loop is two symmetric commands: `uv run pytest` (does it fit?) and
`uv run python -m iotshield.eval` (is it good?). CLI-first because the report is a diffable
JSON artifact CI could gate on, and because it is scriptable.

**Amended 2026-07-20.** This decision originally added "the dashboard renders a saved report
and never launches one" and "no Pipeline-API endpoint", on two grounds: a multi-minute run
under Streamlit's re-execute-on-every-interaction model is a spinner of death, and an
endpoint would let a caller pin a worker for minutes on a corpus they do not own.

Both were true when written, and the *measurement changed*: the efficiency pass took a full
16-recording benchmark from **12.9 s to 3.2 s**. "Minutes" no longer describes anything we
have. Recorded rather than quietly reversed, because the useful part is that the decision was
downstream of a number, and the number moved.

What the reversal keeps:

- **The run is button-gated and cached** (`st.cache_data`), which is what actually makes
  Streamlit safe — not duration. Work in the page body re-runs on every click; behind a
  button with a cache, it happens once and every later interaction replays instantly.
- **The dashboard stays a pure API client** (ADR-0008). It POSTs and renders; it never
  computes. Its `iotshield.eval` imports are deferred into the functions that need them, so
  the package stays deletable from a product image — pinned by a test that imports the
  dashboard and asserts `iotshield.eval` is absent from `sys.modules`.
- **The endpoints do not exist unless opted in.** `IOTSHIELD_EVAL_API` decides whether the
  router is *registered*, not whether a live handler rejects the call. A product deployment
  has no `/eval` route to reach or probe, and none in its OpenAPI schema. The flag is read
  from `api/_eval_gate.py`, which imports nothing from `eval/`, so asking "is the plane on?"
  never drags the plane in.
- **The router takes no database.** A benchmark computes and returns; that it needs no
  resources is the third plane visible in the wiring, and a test proves the eval routes work
  against an engine `/health` cannot reach.
- **Synchronous.** The trigger for a job/poll pattern is a corpus that takes longer than
  ~30 s, which none does.

The UI composes specs from *detector* picks, because a researcher thinks "compare these
detectors" but head-to-head comparison needs N specs of one detector each — several
detectors in one spec run *together* rather than competing. The full-power path (arbitrary
hand-written specs) stays on the CLI, and the API accepts full specs, so the UI is never the
ceiling.

#### D6a — What the dashboard shows, and why those three views

A table was the original output, and tables hide exactly our best findings:

- **Head-to-head bar** — event F1 per spec. A magnitude comparison, so one hue with identity
  on the axis.
- **Per-case variance strip** — one dot per recording. The reason it exists: SKAB's fitted
  run scored a *mean* of 0.47, which reads as "mediocre everywhere" and is wrong — thirteen
  recordings scored near 1.0 and three collapsed to 0. A mean cannot show that.
- **Case timeline** — truth, detections and alerts against one clock. `det/inc 300` is a
  number a reader must decode; three lanes show it as texture, with latency visible as the
  gap before the first detection. This needs per-case spans, so `CaseTimeline` is an
  **optional** report field (capped at 2,000 spans per lane, truncation flagged) — off by
  default, because the report's other job is to stay small and diffable.

No new colours were introduced: identity rides on the y-axis in all three, so none is a
categorical palette. The one candidate pair was run through the palette validator and
rejected for the right reason — a deliberate neutral fails the chroma floor because it *is*
gray, which is the signal that ground truth is a reference backdrop like the grid, not a
series competing for identity.

### D7 — What we deliberately do NOT measure

- **Calibration.** Whether `priority_score` 56.5 is the *right* number, or whether alert A is
  more urgent than alert B. A corpus records that a fault happened, never how much it
  mattered. We measure the prioritizer's **discrimination** (can it separate true from false
  and push true upward — precision@k, mean rank), never its calibration. Ideal-ordering truth
  needs analyst feedback, deferred with its trigger in the week-2 design §8.
- **Corroboration.** Whether cross-lane agreement improves the queue — our headline claim.
  SKAB is telemetry-only, so the factor is pinned at 1.0 and contributes nothing; proving it
  needs a labelled *multimodal* corpus, which does not exist. **`demo_multimodal`'s 27→56
  remains a demonstration, not a benchmark**, until the deferred synthetic generator exists.
- **`asset_weight` is circular** by construction — criticality is something we type into the
  config, so "did it rank the critical asset first" measures our own input. Harmless on SKAB
  (one testbed, constant weight); a trap for any future corpus.

### D8 — Judgment formation is guidance, not an enforced law

The alert engine's `volume` factor counts detections in a bucket as corroborating evidence,
which is only meaningful if each detection is a **distinct judgment**. That precondition was
never written down. It now is, in `detect/port.py`, with the shared helper (`merge_runs`) that
makes complying easy.

It is **not** a suite-enforced law, because "distinct" has no general test: a multi-metric
detector may legitimately emit overlapping detections for different channels, and any
structural rule we can write would reject that valid adapter. The harness *measures*
compliance (detections-per-incident) so the cost is visible; nothing rejects an adapter.

## Consequences

- **The repo now does the second half of what it was named for.** A researcher gets one
  command for "does it fit" and one for "is it good".

- **First measured results (SKAB `valve1`, 16 recordings, 16 faults):**

  | spec | protocol | event F1 | event P | event R | point F1 | det/inc | alerts/inc |
  |---|---|---|---|---|---|---|---|
  | `team3_zscore` | none | **0.73** | 0.69 | 1.00 | 0.68 | 8.9 | 1.5 |
  | `team3_zscore` | prefix | 0.47 | 0.37 | 0.81 | 0.46 | 8.7 | 2.3 |
  | `stub_threshold` | either | 0.52 | 0.35 | 1.00 | 0.52 | **1135** | 5.0 |

  On the clean recording (9,401 rows, no faults): `team3_zscore` raises **1** false alert,
  `stub_threshold` raises **34**.

- **Per-recording fitting made team3 *worse*, and that is a real finding.** Recall fell
  1.00 → 0.81. The mechanism, confirmed per case: 3 of 16 recordings have an unstable normal
  prefix whose p99 threshold inflates to 2531 / 1121 / 676 versus a typical ~25, making the
  fault unreachable — and those exact 3 lost their incident. So the honest refinement of
  ADR-0012 D3 is not "fit on your own history always wins" but **fit quality depends on the
  prefix being long and stable enough, and per-recording fitting is high-variance.** Guarding
  a fit against a pathological baseline is now a known open problem, discovered by measuring.
  (Fitting on `valve1/13`'s prefix reproduces the committed artifact exactly — threshold
  24.54, 45 features — which validates the seam.)

- **`stub_threshold` is a live demonstration of D8.** It scores rows and never merges them,
  so one fault becomes ~1135 detections, which the alert layer compresses to ~5 alerts — a
  compression ratio over 200. Its point F1 (0.52) is respectable while its event precision
  (0.35) is not: exactly the gap the two-level split exists to expose.

- **`compute_run` split into three named phases** — `build_detectors`, `normalize_sources`,
  `compute_result`. Behaviour is unchanged (all seven demos byte-identical). The point is
  that the harness is not an exception to any signature: the product composes
  *build → normalize → judge* and a benchmark composes *build → normalize → **fit** → judge*,
  two equal compositions of the same phases, both running the product's exact code rather
  than a parallel implementation.

- **The port suite gained a law**: a fitted detector must still obey every other law. Fitting
  must not become a hole in the suite. Adapters that are not `Fittable` skip it.

- **ADR-0020's `0.047` is still a citation, not reproducible.** The harness cannot re-derive
  it, because it came from team3's own IsolationForest and we never built an IForest adapter.
  What it delivers is that "event F1" is now a committed, tested definition, so the claim is
  **auditable** and re-runnable against any adapter we do have.

- **A clone of this repository can benchmark with no other setup.** `data/eval/synthetic`
  ships (276 KB), so `uv run python -m iotshield.eval` works on a fresh checkout — which is
  the whole point of the bundle work, and was not true of the first version.

- **The team3 artifact is SKAB-specific, and the harness says so rather than scoring it
  zero.** `team3_zscore` cannot judge the synthetic corpus: its committed artifact is trained
  on SKAB's eight channels, and the channel-coverage guard (ADR-0012 D4) correctly skips an
  asset that lacks them. Under `--fit-protocol none` that reads as recall 0, which is the
  misleading-number failure again — under `prefix` the refusal is reported as a note (`fit
  refused (fit input is missing trained channels: …)`). **Deferred:** a synthetic-trained
  artifact so both detectors can be compared on the committed corpus. Until then, compare
  detectors on `synthetic` with `stub_threshold`, and on SKAB with `team3_zscore`.

- **Deferred**, each with a trigger: a labelled *multimodal* corpus for the corroboration A/B
  (the generator now exists, so this is a recipe away); CICIDS2017 once a flow lane exists;
  learned/calibrated prioritization (needs analyst feedback); guarding a fit against a
  pathological baseline; a synthetic-trained team3 artifact (above); declaring `fittable` in
  the registry rather than discovering it by `isinstance` (only worth it if the question
  moves onto a hot path).
