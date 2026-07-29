# 27. Measuring what actually happened

Date: 2026-07-27

## Status

Accepted. Amends ADR-0021 (D2 scoring, D4 fit protocols, D5 the labels format) and ADR-0026
(D1 per-asset truth). Bumps the on-disk answer key to v3.

Numbering note: ADR-0026 predicted 0027 would be the evidence schema. It is not; those shift
again, to 0028/0029.

## Context

A review of the generator and the evaluation harness, aimed at the complaint that the two
"should talk the same language but instead conflict each other". Fifteen findings, every one
reproduced before it was written down. They were not fifteen independent bugs.

**The generator and the evaluator did not share a definition of ground truth.** The generator
wrote down what it *intended* — a declared fault window, on a recording-wide clock. The
evaluator scored what was *observed* — rows, per asset, per lane. Two different objects, with
nothing forcing them to agree, and every symptom below follows from that one gap:

- `build._incident` computed a fault's interval from `FaultSpec.start_step`/`length_steps`,
  unclamped, while `SensorRig._apply` clamped its damage into the recording. Two computations
  of one window, and only one of them touched a byte. A fault at step 100 on a 20-step clock
  was labelled eighty seconds past the last row: an unwinnable recall miss, permanently, for
  every detector that would ever run on it.
- The only link between the two was a boolean, and it was set when a *handler was dispatched*
  rather than when the output changed. So `intensity=0.0` produced byte-identical output and
  still shipped labelled. `_validate`'s `unexpressed` guard could not catch either, because it
  consumed the same boolean.
- An asset's row clock was the deduplicated union across every lane. On `device_trimodal` each
  device carries ~900 telemetry instants, ~275 flow records and ~25 log lines, so the fastest
  lane owned the clock: one flow detection covering sixty seconds was charged **sixty telemetry
  false positives**. Point precision on the compromise case read 0.041, and the corroboration
  A/B — that corpus's entire reason to exist — showed headline ptF1 falling 0.83 → 0.11 when
  lanes were added.

Three further findings were the same disease in the *aggregate* rather than in the answer key.
Every corpus-level rate was a mean of per-case rates; a rate is undefined on a clean recording;
`_mean` skips undefined values. So a spec that stayed silent on the clean recordings and one
that fired four false alarms on them reported **identical** event and point F1. Every shipped
corpus includes clean recordings specifically to measure that.

And `--fit-protocol prefix` cut training data at each asset's own first incident — which for an
asset with *no* incidents is no cut at all. Measured on `fleet_scan/scan-one`: 1515 of 1580
scored events were also training events, and all fourteen asserted-clean assets were trained on
100% of what they were judged against. The `none` vs `prefix` experiment CLAUDE.md documents
was comparing a clean measurement against a leaked one.

The rest were surfaces disagreeing with each other, or with themselves: an option no bundle
format could satisfy, a default pairing the documentation says cannot work, a typo that exited
0, a diagnostic class no caller caught, a manifest path that could read any file on the host,
and a dashboard composing a lookalike of a committed spec.

## Decision

### D1 — A label is made of the instants that changed

`Observed.expressed` carries `{fault_id: frozenset[datetime]}` — the instants a behaviour
actually perturbed — instead of a set of fault ids. `build._incident` derives the interval from
those instants; `Clock.span` is deleted, because there is no second computation left.

Every behaviour already had the answer at the line where it discarded it: `SensorRig` knows the
span it damaged, `NetworkHost` and `LogSource` hold the exact steps they injected records at.
The telemetry rig compares the **rounded** series before and after, because `_DECIMALS` is
applied before rendering — a perturbation below the sixth decimal never reaches the file, so
the answer key must not claim it did.

This makes `build.py`'s docstring true rather than aspirational: the key cannot describe
something absent from the bytes, because it is made of the bytes. `_validate`'s `unexpressed`
rule stops being decorative and starts refusing `intensity=0.0` at generation time.

Rejected: a fourth validation rule ("check the bytes differ"). It is another guess that a
future fault shape slips past, and it leaves the interval still derived from intent.

*Observable consequence.* The synthetic corpus's readings are byte-identical; only the labels
moved. `spike` is a half-sine, `sin(linspace(0, pi, 4))` = `[0, .87, .87, 0]`, so a spike
declared at step 5 for 4 steps is now labelled **6..7**: the first and last samples add exactly
nothing. Two rows per spike were previously labelled anomalous with nothing in them.

### D2 — The row clock is per `(asset, lane)`

`AssetTruth.timelines` maps lane to rows. Point metrics score a lane's detections against that
lane's rows and no others, and a row is anomalous when a fault **that lane expressed** covers
it (`Incident.affects`). An incident declaring no lanes affects every lane — the honest reading
for a converted corpus like SKAB, which records that a fault happened and never which modality
showed it, and indistinguishable from the alternative on a single-lane corpus.

This is ADR-0026 D1's move ("one clock per asset, not one global clock") carried one dimension
further. It needs no new concept.

`AssetTruth.clock` remains, as the sorted union, for the genuinely lane-agnostic questions:
where an asset's history starts (D3) and what a bundle stores.

Rejected: per-lane *intervals* as well. With the clock per lane, a lane is already only scored
on its own rows, and the union differs from each lane's own extent by at most the sampling gap
of the sparser lane — not worth a schema dimension.

*Cost.* Labels schema v3 (`AssetLabel.clocks`, keyed by lane). An older file is rejected with
the regenerate command rather than upgraded, for the reason v2 rejected v1: an upgrade would
have to invent the missing dimension, and the only place to find it is the readings, which is
precisely what the format exists to avoid needing.

### D3 — `prefix` reserves a head, and the head is not scored

`fit.Reservation` holds the cut and the answer key that survives it, as one object, because the
two are the same decision seen from either end: what a protocol took for training is exactly
what scoring must not look at. Splitting them is how the leak got in.

`prefix` reserves the first `TRAIN_FRACTION` (0.2) of each asset's pooled clock, capped at that
asset's first incident, and removes those rows from the truth. Findings whose window ends
before an asset's cutoff are dropped, in both directions — the same rule the harness already
applies to an asset outside the roster: *not measured* is not *measured and correct*.

Detection still runs over the whole recording, because a windowed detector needs its warm-up. A
finding that straddles the boundary is kept and judged on the part that counts.

An asset with no usable reservation (a very short clock, a recording opening mid-fault)
contributes **nothing** to training and is scored whole. Both halves matter: training on it
while leaving it out of the cutoffs would restore the leak.

Rejected: cutting at the corpus-wide earliest incident. Cheaper, and still leaks whenever an
asset's fault starts later than the earliest one. Disjointness should hold by construction, not
by where a fault happened to land.

### D4 — Corpus-level rates are pooled from counts

`SpecReport.summary()` divides summed counts instead of averaging per-case rates, so
`CaseReport` now carries the counts every rate was divided from (`scoreable_detections`,
`true_detections`, `incidents_caught`, the three point counts, `scoreable_alerts`,
`incidents_covered`).

The rates themselves are defined **once**, on `score.Counts`, which both `score_detections`
(one case's counts) and `SpecReport.summary` (the sum of them) ask. Transcribing the
compositions into `report.py` would have recreated the failure this decision fixes one level
along: this very ADR changes `detections_per_incident`'s denominator, and with two copies the
per-case column and the headline would silently disagree. `Counts` adds, so pooling is
addition and nothing else.

A false alarm on a clean recording has no recall to spoil but does land in the *denominator* of
precision — which is why pooling makes it visible where averaging could not. It also removes an
unstated macro-average: a 3-case recording used to weigh as much as a 300-case one. It is the
same rule `score` already applies within a case, applied one level up rather than approximated.

The per-case table is unchanged and still prints `-` where a rate does not exist (ADR-0021 D2).
`det` and `alerts` stay means, because those columns report "per case" and always did.

### D5 — Width is measured, not refused

`mean_true_detection_overlap` (`ovl`): of a true detection's window, the fraction that sits
inside a real fault on its own asset. Truth at rows 20–59 of a 100-row recording, judged by one
window covering 0–99, scores event precision 1.00, recall 1.00, one detection per incident and
0s latency — the best possible row, from a detector carrying no information. `ovl` reads 0.39.

Reported rather than refused, matching the harness's stance elsewhere: a wide window is
sometimes the honest output of a detector that cannot localize better. Measured over true
detections only, since a false one has zero overlap by definition and would merely restate
precision in a second column.

### D6 — Every rate uses scoreable findings

`detections_per_incident` and `alerts_per_incident` now divide by the *scoreable* count, like
every other rate. Previously they counted findings on unlabelled assets — 24 detections on an
unlabelled host turned one correct catch into "25 detections per incident", printed directly
above the line promising those findings were excluded from every rate.

### D7 — `fitted` counts what the protocol asked for

`FitOutcome.attempted` distinguishes "the protocol tried" from "the detector could". A run is
fitted when every *attempted* detector fitted. One rule-based control beside a learning
detector used to report `0/11 fitted` beside a score only eleven successful fits could produce
— and adding such a control is the shape `sweep._applied`'s own docstring recommends.

### D8 — Ingest warnings reach the report

`normalize_sources` already computes "this normalizer rejected 100% of input"; `evaluate_case`
discarded it. Now merged into `CaseReport.notes` beside the compatibility warnings. This is the
gap `_check_alignment` structurally cannot cover: it compares declared *format* strings, so a
normalizer misconfigured by **params** (a `home_networks` that does not cover the corpus)
passes alignment and then silently ingests nothing.

### D9 — `holdout` is removed, not left broken

`Case.holdout` had no `CaseManifest` field, so no bundle could ever populate it — while the
CLI, the API and the dashboard all offered `--fit-protocol holdout` as a working choice, and it
could only ever print a table of zeros with one trailing line of explanation.

An option that cannot succeed is worse than an absent one. Reinstating it means a manifest
field plus a generator that emits a clean companion recording, which is a labels-schema change
and belongs in an ADR rather than in a dropdown.

### D10 — The legal corpus/spec pairings are derived and published

`eval/bench.py` discovers `config/bench/*.yaml` and joins each spec's source shape against each
corpus's, which is exactly `runner._check_alignment`'s rule. Six specs, six corpora, roughly
seven legal cells, and that map previously lived only in YAML comments — the structural form of
"the two do not talk the same language".

Consequences: `--corpus` and `--spec` lose their defaults and print the table instead (the old
default benchmarked `synthetic` with team3, the one pairing CLAUDE.md says cannot work, and
printed eleven rows of 0.00 with no note); `eval list` prints the table; `GET /eval/specs`
publishes each spec **with its `PipelineSpec`**, so the dashboard runs the committed file
verbatim rather than a lookalike.

Derived rather than declared as a `corpus:` field on each spec: a hand-maintained field says
the same thing while being free to drift from what it describes.

### D11 — Three error classes that were unreachable

- `UnknownCorpus(CorpusUnavailable)`. Both used to be `CorpusUnavailable`, which the CLI treats
  as a skip, so `--corpus sythetic` exited **0** with empty output — a CI benchmark job passing
  green having measured nothing. The information to distinguish them was already computed at
  the raise site; only the type was missing. The API maps both to 404, unchanged.
- `BundleError(ValueError)`, was `RuntimeError`, caught by nothing. Listings now surface a
  broken bundle *with its reason* instead of `continue`-ing past it, and asking for one by name
  raises rather than reporting "unknown corpus" without mentioning the directory on disk.
- `bundle.contained` refuses a manifest path that escapes its bundle. A bundle is untrusted
  drop-in data, `data/eval` is a bind mount in compose, and `"../SECRET.txt"` returned the
  file's contents inside a benchmark report. Enforced at the single construction site so every
  entry point inherits it.

### D12 — The round-trip law compares whole events

`test_generated_text_round_trips_through_the_real_normalizer` compared the *sets* of lane,
asset id and timestamp, leaving an event's entire content unpinned: a renderer writing the
wrong metric value, dropping a channel, swapping `src_bytes`/`dst_bytes` or rounding a float
passed cleanly, and the resulting recall drop would have been blamed on the detector. It now
compares every field except the normalizer's own bookkeeping (`raw`, `event_id`, `origin`) and
`source_format`, which the syslog reader legitimately refines to a dialect name.

A companion meta-test asserts every normalizer-consumable, bundle-storable format has a
renderer. It immediately flagged `zeek-conn`: an accepted bundle format, exercised by the
flagship demo, that no generated corpus could ever be written in. `ZeekConnRenderer` closes it,
sharing the orientation inverse (`_Endpoints`) with the CSV dialect.

## Consequences

**Every published benchmark number moves.** Nothing that reads them survives unexamined: the
answer key changed (D1, D2), what is scored changed (D3), and how the aggregate is computed
changed (D4, D6). Numbers in older ADRs and design documents describe a different measurement.

**Bundles must be regenerated and SKAB re-fetched.** v2 answer keys are refused with the
command. The committed corpora's *readings* are byte-identical, which is itself the evidence
that D1 changed only the labels.

**The corroboration A/B reads correctly for the first time.** On `device_trimodal`,
`trimodal_all` scores evF1 0.98 / evP 0.97 against `trimodal_telemetry`'s 0.80 / 1.00 — and the
0.97 is the mirror decoy's false alarm on the clean recording, which ADR-0024 predicted and no
headline could previously show.

**`none` vs `prefix` is now a real experiment.** On `skab_valve1`, team3 scores evF1 0.30 with
its shipped artifact and 0.55 fitted per case, with `ovl` 0.74 against 0.97 — the fitted windows
sit far tighter on the fault. Under the old protocol the second number was partly memorised.

**`Observed` is a published extension point and its shape changed.** Three implementations
exist, all ours, each needing one line. A `Behaviour` written against v1 will fail loudly at the
type level rather than silently mislabelling.

**Deferred, deliberately.**

- `holdout`, as D9 describes: a `CaseManifest` field plus generator support.
- Per-lane incident intervals (D2), if a detector ever needs sub-lane resolution.
- `TRAIN_FRACTION` is a constant, not a flag. It becomes a flag the first time someone has a
  reason to vary it, not before.
- `generate` still never deletes, so a renamed case leaves an orphaned payload committed; the
  freshness test walks only the fresh tree and cannot see it.
- The vocabulary still collides: `entry` means four different things across `corpus.py`,
  `sweep.py`, `__main__.py` and the dashboard.
- Asset ids are still hand-copied from `scenarios.py` into three bench specs' `assets:` blocks
  with no check, and those blocks contradict ADR-0021 D7's own rule.

**Raised by the cleanup pass and deliberately left.** Each is a real gap; none is a change this
record's decisions require, and each would move numbers or add schema.

- **`_incident` collapses the perturbed instants to their convex hull.** So D1's guarantee holds
  up to that hull rather than absolutely: a fault whose injected steps straddle a quiet stretch
  labels the gap too. Audited across all 17 expressed faults in the shipped corpora — zero rows
  are over-labelled today, but that is a parameter coincidence, not a property. The deeper form
  needs no schema change: `merge_runs` already turns an instant set into contiguous runs, and
  the bundle format already stores a *list* of intervals, so `_incident` could emit one incident
  per run. It changes incident counts and therefore every rate again.
- **No `unlabelled_lane_detections`.** A detection on a lane the corpus has no clock for is
  invisible to point metrics and still counted in event precision, with nothing disclosing how
  much was unmeasurable. The asset dimension got exactly this counter (`unlabelled_detections`)
  and the argument for it; the lane dimension got the loop.
- **`summary()` is computed, not serialized.** `reports/evaluation.json` carries the per-case
  counts but no pooled corpus-level number, so a CI gate wanting the D4 headline must re-pool
  from `cases[].*` — which is a third place that would know how a rate is formed.
- **`_attribution`'s per-lane recall still macro-averages** across cases, immediately below a
  decision that says mean-of-rates is wrong. `CaseReport` carries the counts behind every rate
  except this one, so fixing it is a schema addition.
- **The dashboard's shipped-spec mode hardcodes `sweep: {}`.** True of a spec naming an
  artifact, false of `config/bench/team3_scratch.yaml`, which names none precisely so it can be
  swept — so the page is less capable than the CLI on the one experiment ADR-0022 exists for.
  `/eval/specs` publishes the detector params a sweep menu would need.
