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

### D5 — Corpora are a registry, not a fifth port

`CORPORA` is a name→loader dict, deliberately the same shape as `DETECTORS`/`NORMALIZERS`, so
"bring your own labelled dataset" is the same gesture as "bring your own adapter". It is
explicitly **not** a port: no Protocol, no port contract suite. Ports are the pipeline's
runtime joints and nothing here runs at product runtime, so a registry plus a dataclass is
the right altitude.

Because we push `iotshield/` only, a clone has no `reference/` tree and therefore no SKAB.
So corpus paths are configurable, a missing corpus raises `CorpusUnavailable` (which the CLI
reports as a **skip, exit 0**, so CI stays green), and one committed micro-corpus
(`data/eval/demo_team3.labels.json`) needs nothing external.

Corpora produce raw **text**, not events, so a benchmark exercises normalization too — it
measures the pipeline, not just a detector. Each input is *tagged* with its
`source_format` (`CaseInput`) and checked against the spec's `sources`, for the same reason
ADR-0019 routes by explicit tags: an untagged mismatch is not an error but a *misleading
number*, since the wrong normalizer quarantines every line, the detector sees nothing, and
the report reads exactly like a detector that failed. The harness exists to stop publishing
numbers like that, so the mismatch is refused.

### D6 — CLI-first; no API endpoint

The researcher's loop becomes two symmetric commands: `uv run pytest` (does it fit?) and
`uv run python -m iotshield.eval` (is it good?). CLI-first because the report is a diffable
JSON artifact CI could gate on, benchmarks are long-running and not request-shaped, and it
is scriptable. **The dashboard renders a saved report and never launches one** — Streamlit
re-executes on every interaction, so a multi-minute corpus run there is a spinner of death.

No Pipeline-API endpoint: that API runs *the operator's* pipeline on *their* data, and a
benchmark endpoint would let any caller pin a worker for minutes on a corpus they do not own.

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

- **Deferred**, each with a trigger: a labelled multimodal corpus for the corroboration A/B
  (needs the synthetic generator); CICIDS2017 once a flow lane exists; learned/calibrated
  prioritization (needs analyst feedback); a benchmark API endpoint (needs a hosted
  "benchmark on your own data" product); guarding a fit against a pathological baseline
  (newly discovered above).
