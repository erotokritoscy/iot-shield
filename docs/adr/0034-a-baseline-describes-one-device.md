# ADR-0034: A baseline describes one device

- **Status:** accepted
- **Date:** 2026-07-29
- **Amends:** ADR-0012 (D2 the artifact, D4 the channel-coverage guard), ADR-0022 (the shape
  of team3's fitted state, not the law)
- **Related:** ADR-0021 (the evaluation plane and fit protocols), ADR-0030 D7 (one model per
  meter — the same decision, taken for the other learning detector first), ADR-0033 D3
  (`Diagnosable`, the capability this change reports through)
- **Artifact schema:** team3 v2 -> **v3**. `skab_zscore.json` regenerated; every stored
  number is unchanged.

## Context

Found by clicking, not by a test: benchmarking `device_trimodal` with `team3_zscore` under
`--fit-protocol prefix` reported

```
fit refused (fit expects one asset's history; got 3 (10.0.0.11, 10.0.0.24, 10.0.0.53))
```

and a full row of zeros. Every layer was behaving correctly, which is what made it worth an
ADR rather than a fix.

**What a fitted `Team3State` physically is.** Five fields: `channels`, `feature_columns`,
`median`, `mad`, `threshold`. There is no asset dimension anywhere in it. So the object
already *was* a statement about one device — the type said so before any check did.

**Why it can only be about one device.** The features are `rolling(window).mean()`,
`.std()`, `.diff()` and `.shift(lag)`. Every one reads the row before it. That presupposes
an arrangement where a row is one device at one instant and consecutive rows are consecutive
instants *of that same device*; `.diff()` across a row boundary between two devices computes
"camera temperature minus gateway temperature", a number with no referent.

Measured on `device_trimodal`'s real telemetry, with the guard bypassed, pooling three
devices did something worse than interleave them. `wide_frame` pivots on
`(timestamp x metric)` with `aggfunc="mean"` and **no asset in the index**, so:

- shared channels were **averaged into a device that does not exist** — at
  `00:01:30Z`, gateway 57.337, camera 61.771, mirror 54.106 became a single `Temperature` of
  57.738, a fleet mean no sensor measures and no fault expresses;
- unshared channels were **manufactured** — the camera samples at 5 s and the other two at
  30 s, so `CpuLoad` was 83.3% NaN against the camera's index, and `ffill` turned it into a
  staircase whose `.diff()` is four zeros and a jump forever.

Three real devices (30x2, 180x2, 30x2) became one fictional 180x3 device. Refusing that was
right.

**The two halves disagreed.** `fit` demanded exactly one asset; `detect` accepted any number
and scored them all against *the same single baseline*. So the adapter had no coherent story
about a fleet in either direction: it refused to learn from one, and it silently mis-judged
one. The refusal was the loud half of a problem whose quiet half — a pump rig's median/MAD
judging a camera, no error, no warning — is the more dangerous.

**Why it stayed invisible.** Not hidden; never exercisable. The only fittable telemetry
detector was team3, and the only corpora it was paired with — `skab_valve1`, `skab_valve2`,
`skab_clean`, `synthetic` — have **exactly one asset each**, so "the case's events" and "one
asset's history" were the same set of events, and the harness's assumption was true by
accident. `device_trimodal` is the first multi-asset corpus on the telemetry lane.

`meter_iforest` had already met this and answered it the other way (ADR-0030 D7: fitted state
is a map from `asset_id` to that meter's model). That ADR names the contrast explicitly —
"both enforce the same rule, never interleave two assets' histories, from opposite sides".
Two adapters, one rule, two shapes, is one shape too many.

## Decisions

### D1 — Fitted state is a list of per-asset baselines

`Team3State` gains `asset_id`; `Team3Artifact` carries `models: list[Team3State]` with a
uniqueness validator and a `fitted` map keyed by asset. This is `MeterArtifact.meters`
exactly, so a reader who has understood one has understood both.

`asset_id` lives *in* the state rather than as a key it is filed under, so a model read out
of the JSON says which device it describes without reference to its container — and so
`channels` can differ per asset, which is what D3 needs.

### D2 — `detect` looks up per asset; no model means skipped, never scored

An asset absent from the map is skipped rather than judged against another device's normal,
which would measure the difference between two machines and call it an anomaly. The
channel-coverage guard (ADR-0012 D4) then checks each asset against **its own** model's
channels rather than one global schema.

### D3 — Channels are learned per asset, which is what makes a heterogeneous fleet work

This is the constraint that decided the whole design, and it rules out the cheap fix.

Building features per asset and then stacking the *feature rows* into one shared baseline
would have fixed the averaging and the staircase with no schema change. It does not survive
a fleet whose devices report different channels. Unpinned, a pooled fit learns the **union**
— `{CpuLoad, FrameRate, Temperature}` — and D2's per-asset coverage guard then finds that
the camera (`FrameRate`, `Temperature`) and the gateway (`CpuLoad`, `Temperature`) are each
missing a channel they never had, so **every asset is skipped and the table is zeros again**,
for a third distinct reason.

Generalised: one `Team3State` can serve only devices that all carry the same channels. A
mixed IT/OT estate is heterogeneous by definition. Per-asset state is therefore forced, not
preferred.

### D4 — A fleet fit is not all-or-nothing

An asset that cannot be fitted (too short a history, a missing pinned channel) is skipped and
named; only a fleet where *nothing* could be fitted raises, and that error lists each
refusal. One device with 40 rows against a 60-row window must not cost the other twenty-nine
their fits. Mirrors `meter.fit`'s `too_short` handling.

### D5 — Both silences are reported, and neither claims a cause it cannot see

`why_silent` (ADR-0033 D3) now distinguishes **no model for this asset** from **model
present, channels not covered**, because they call for different actions: fit this device,
versus this model is for a different rig.

It deliberately does **not** say *why* an asset has no model. The detector genuinely does not
know — it may never have been trained on that asset, or `fit` may have refused it under D4 —
and a fitted-from-artifact detector could not know either, since an artifact has nowhere to
carry a refusal. Naming a cause it cannot see would be this capability's own failure mode,
one level in. The refusals are named at fit time, in the exception, when they are total.

### D6 — No device-class models, for now

Thirty identical pumps need thirty fits of near-identical data. That is the same trade
ADR-0030 D7 accepted for meters ("there is no universal meter model"), and the map can grow
an `applies_to` later without breaking any stored file, so the cheap option stays open. What
is *not* reversible is shipping a shared-baseline shape and discovering which devices it
silently mis-judged.

### D7 — v2 is refused, not upgraded

Mechanically the upgrade is trivial: wrap the one `state` in a list. It is refused because
the upgrade must **invent** the `asset_id` that state belongs to, and inventing it wrong is
the quietest failure available — the detector skips every real asset and reports no
detections, which reads as "nothing was wrong". Regenerating names the asset deliberately.
`train.py` gains `--asset`, defaulting to `skab-testbed`: the id carried by
`data/demo/demo_team3.csv` and by every `skab_*` bundle, which is what keeps
`config/demo_team3.yaml` and the committed SKAB benchmarks running unchanged.

## Measured

**The shipped model is bit-for-bit the same model.** Regenerating `skab_zscore.json` changed
`schema_version`, moved `state` into `models[0]`, and added `asset_id: skab-testbed`.
`params`, `provenance`, `channels`, `feature_columns`, `median`, `mad` and `threshold`
(24.539536363633378) are all identical. The change is about *whom* the model is for, not
what it learned.

**`skab_valve1` under `prefix` is byte-identical to before** — `evF1 0.55, evP 0.38,
evR 1.00, ovl 0.97, ptF1 0.69, 272 detections, 26 alerts, 16/16 fitted`, and every one of the
sixteen per-case rows unchanged. A single-asset corpus produces a one-entry map, so the
regression gate is that nothing moved, and nothing did.

**`device_trimodal` under `prefix` now measures something**, with `team3_scratch`'s shape
(hyperparameters, no artifact, so channels are unpinned):

| | before | after |
|---|---|---|
| cases fitted | 0/3 | **3/3** |
| detections | 0 | 21 |
| alerts | 0 | 6 |
| telemetry event recall | 0.00 | **1.00** |
| telemetry point precision | - | 0.06 |
| evF1 | 0.00 | 0.14 |

**And the number is bad, which is the honest result.** Recall is perfect and precision is
0.06: the fault is caught and so is a great deal else, including alerts on an asserted-clean
asset. That is the high-variance failure ADR-0021's shakedown already measured on SKAB (recall
1.00 -> 0.81 under per-recording fitting, from an unstable normal prefix inflating the learned
threshold), reproduced on a fleet. **This change did not make team3 better; it made team3 on a
fleet *measurable*.** Deciding whether per-asset fitting is a good idea is now an experiment
someone can run, which it previously was not.

**D4 is load-bearing on real data.** Only the camera gets a model on this corpus: it samples
at 5 s (180 readings), while the gateway and mirror report twice a minute (30 readings), so
their 20% training prefix is six readings — too few for any window. Before D4 that would have
been a total refusal; now two thirds of the fleet is skipped, the run still measures the third
that fitted, and `why_silent` names the two that did not. Lowering the window to 10 does not
rescue them, which is a fact about the recording rather than the model.

## Consequences

- **`skab_zscore` no longer judges assets other than `skab-testbed`.** Previously it judged
  anything whose channels matched; now a foreign rig is skipped. This is a product-visible
  behaviour change and it is the point: the old behaviour applied a pump's baseline to
  whatever it was pointed at. It is only tolerable because D5 guarantees the skip is
  reported — silence with a reason beats confident nonsense, but silence alone does not.
- **The evaluation harness needed no change.** `prepare_detector` already hands `fit` the
  whole pooled training slice; once `fit` splits by asset, the harness works unmodified. The
  constraint is fixed in the adapter that owns it, and the eval plane learns nothing new about
  assets. (The alternative — fitting per asset inside `eval/` — would have fixed the
  benchmark while leaving `detect`'s silent half in place.)
- **`fitted N/M` counts cases, not devices.** A case whose fleet was partially fitted counts
  as fitted, and `why_silent` carries the partiality. Splitting that into a per-device count
  would need a report field; deferred rather than guessed.
- **`Team3ZScoreDetector` gains `fitted_assets` and `model_for`**, matching
  `MeterIForestDetector`. Two learning adapters now present the same surface — but it is a
  **convention, not a declared capability**, and that gap is the honest weak point of this
  change. Nothing type-checks the pair, and the port suite still cannot express "fit two
  assets, judge a third" (every `fit_events` fixture is single-asset). So the class of bug
  this ADR exists to fix — one rule enforced on one side and violated on the other — remains
  catchable only by clicking, which is how it was caught. The fix is cheap and deliberately
  deferred to its own change, because it adds a port capability and that is an architecture
  decision rather than a consequence of this one: a one-member `PerAsset` protocol beside
  `Stateful` (the precedent for a protocol existing purely so a shape is type-checked rather
  than discovered by `getattr`), plus a capability-gated law relabelling `case.fit_events`
  under a second asset id. Precedent for legislating at N=2 is explicit — ADR-0032 D7 made
  `explanation` a law because "four of six adapters honoured it while both docs claimed all
  of them did".
- **`meter_iforest`'s `why_silent` under-reports, and this change makes that visible.** It
  has the same second silence team3 now names — a model present whose feature schema this
  data cannot produce (`except KeyError: return []`) — and reports only unmodelled meters.
  Left alone here because it is a different adapter and a completeness gap rather than a
  consequence of this change; it is exactly what the port law above would force closed.
- **A fit-note now names its detector** (`eval/fit.py`). `CaseReport.notes` is a set, so
  three unfittable detectors used to deduplicate into one anonymous "not fittable" line, from
  which a reader could not tell whether one control opted out or the whole spec did. Leading
  with `method` rather than the registry name also distinguishes two presets of one adapter.
- **Still open:** a fitted detector cannot say why `fit` refused a particular asset (D5), and
  there is no device-class model (D6). Both are additive; neither blocks anything today.
