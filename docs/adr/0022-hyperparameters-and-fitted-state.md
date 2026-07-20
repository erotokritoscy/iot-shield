# 22. Hyperparameters vs fitted state

Date: 2026-07-20

## Status

Accepted. Amends the `Fittable` port (ADR-0021 D3) and the team3 artifact layout (ADR-0012 D2).

## Context

ADR-0021 gave the harness the ability to **retrain** a detector per case (`--fit-protocol
prefix|holdout`). The other half of an evaluation harness — **choose your own
hyperparameters** — turned out not to exist, and measuring the registry showed why:

```
stub_threshold   ['threshold', 'lane']                       <- every knob, config-settable
log_burst        ['preset', 'window_minutes', 'min_count']   <- every knob, config-settable
team3_zscore     ['artifact', 'min_segment_length']          <- window/lags/fft/percentile MISSING
```

team3's real hyperparameters were inside `artifacts/skab_zscore.json`, next to the fitted
baseline:

```
window_size, lag_steps, fft, percentile   <- HYPERPARAMETERS (a human chose these)
channels, feature_columns                 <- FITTED SCHEMA   (learned from the data's shape)
median[], mad[], threshold                <- LEARNED PARAMS  (the "weights")
```

Two consequences, one visible and one not:

1. **No config or benchmark could set them.** A sweep over `window_size` was impossible to
   express, so the harness could compare *detectors* but never *configurations of one*.

2. **`fit()` silently inherited them from a stale file.** `detector.py` read
   `art.window_size`, `art.lag_steps`, `art.fft`, `art.percentile` and `art.channels` off
   the artifact it had been loaded from. Five of six hyperparameters came from the old
   artifact; the sixth, `min_segment_length`, was correct — *and the only reason it was
   correct is that it happened to be a constructor argument.* The distinction was never a
   modelling judgment. It was plumbing.

The general hazard is not team3's. **Every adapter that persists a trained model will mix
the two**, because every serialization format does: a pickled sklearn estimator, a torch
checkpoint, an ONNX graph all carry architecture *and* weights in one file. Left unstated,
the next adapter repeats this exactly.

## Decision

### D1 — Hyperparameters are exactly the adapter's construction parameters

Stated once on the `Fittable` port, binding on every adapter that learns anything:

1. **Hyperparameters are exactly the registry factory's parameters** — declared as a frozen
   pydantic model on `AdapterSpec.params_type`, so config sets them, `/modules` reports
   them, and the harness sweeps them. Nothing configurable may hide inside a model file.
2. **`fit(events)` recomputes learned parameters and learned schema from `events`, using
   the hyperparameters the instance was CONSTRUCTED with.** It never reads a hyperparameter
   out of its own previous fitted state.
3. **A persisted artifact is an atomic cached fit**, not a source of configuration.

This is sklearn's `get_params()` / `fit()` split in our vocabulary, and it makes
`build_detector(name, params).fit(X)` fully determined by `params` and `X` — which is
precisely what a sweep needs to mean what it says.

### D2 — An artifact is atomic; a contradicting override is a construction error

`{artifact: skab_zscore, window_size: 20}` is refused. The artifact's `median`/`mad` are 45
numbers **positionally aligned** to the feature columns that `window_size=60` produced;
keeping them while changing the window scores one feature set against another's baseline —
silently, producing numbers, all meaningless. This is the failure the rule exists to
prevent, and it cannot be detected downstream.

The refusal prints the artifact's full hyperparameter block as copy-pasteable YAML, so
"start from these but change one" is an edit rather than a hunt through JSON. Note the test
is *given or not given*, *not* value equality: inheriting is what omission means, so the
rule stays one sentence and never depends on what a particular file happens to hold.

Rejected alternative: silently letting the override win. It produces a working-looking
detector whose scores are noise — the worst outcome available.

### D3 — No artifact means an untrained detector, which refuses to judge

Omitting `artifact:` is how a spec says "fit this from scratch with my hyperparameters" —
the direct analogue of `IsolationForest(n_estimators=300)` before `.fit()`. Such a detector
reports `is_fitted = False`, and `detect()` raises `NotFitted` rather than returning `[]`:
an empty list is a *successful* "no anomalies here" verdict, the most dangerous wrong
answer a detector can give.

Three consumers, three different correct responses to the same state:

| Consumer | Response |
|---|---|
| `compute_run` (the product) | refuses the run with a named config error, before judging and before any sink |
| `eval.prepare_detector` | reports a note; `evaluate_case` drops it so the case scores zero **with a reason** |
| the dashboard | disables **Run benchmark** and says which detector needs a fit protocol |

The runner's check sits in `compute_run`, not in the compatibility guard: the guard's
defining property (ADR-0014) is that it reasons about a spec *without constructing*
adapters, and this is the first check that needs the built object. It is also deliberately
not in `compute_result`, which the harness composes itself — the product refuses, the
benchmark reports.

`is_fitted` is read through `_fitted.is_fitted()`, where **absent means fitted**, rather
than being added to the `Fittable` protocol. `Fittable` is `runtime_checkable` and matches
on attribute *presence*, so adding a member would silently reclassify any existing adapter
that implements only `fit` as "not fittable" — a wrong answer delivered quietly. The
default is also the right one: a rule-based detector has no state to learn.

### D4 — The registry declares the split; the artifact file shows it

`AdapterSpec` gains two optional fields, both defaulting to "not a learning adapter" so
`stub_threshold` and `log_burst` say nothing:

- `params_type` — the hyperparameter model. Supplies the **real** defaults to `/modules`,
  since a learning adapter's factory params must all default to `None` (the "not given"
  sentinel D2 needs), which would otherwise leave a UI with nothing to prefill.
- `artifact_param` — which param names a cached fit. Declared rather than name-sniffed, so
  a UI can offer "trained weights" and "fit from scratch" as two **exclusive modes** and
  never construct the request D2 refuses.

The artifact file is reshaped to schema v2 so the split is visible in the data:

```json
{"schema_version": 2, "params": {...}, "state": {...}}
```

The committed `skab_zscore.json` was migrated by moving keys only — `median`, `mad` and
`threshold` are bit-identical, verified against the v1 values, which are now pinned by a
test. A v1 file is rejected with "regenerate it with…", version-checked *before* shape
validation so it fails on the one actionable fact rather than on ten unexpected keys.

### D5 — The law is enforced behaviourally, in the port suite

> Build the adapter twice with **one hyperparameter differing**, fit both on **identical
> data**, and assert the two fitted detectors behave differently.

No introspection and no new port surface. An adapter whose `fit()` read its hyperparameters
from a stale artifact — the exact bug above — produces two identical detectors and fails.
Adapters declaring no `distinguishing_params` skip, exactly like the existing fit test.

Chosen over adding `get_params()` to the port: that would enlarge the contract every
adapter must satisfy in order to test one that already declares its params to the registry.

### D6 — Sweeps are spec rewriting, and live in one place

`eval/sweep.py` cross-products a spec over hyperparameter values into the same
`list[tuple[label, PipelineSpec]]` that `evaluate` already takes. So a sweep is **N ordinary
rows** in the head-to-head table — the console renderer, the JSON report, the CI gate and
the dashboard all handle one with no knowledge that a sweep happened.

- Labels read `team3_scratch[window_size=20]`, carried into the report as the run's identity.
- An assignment reaches **every detector in the spec that declares that parameter**; the
  rest are untouched, so a rule-based baseline in the same spec stays the control.
- Validation is over the **union of all specs**, so `--spec team3 --spec stub --sweep
  window_size=…` works (the stub rides along once, as the control) while a genuine typo is
  still refused and names the real alternatives.
- `MAX_EXPANSION = 24`, because the caller is often a button: a stray comma must not launch
  three hundred benchmarks on a shared server.

Shared by the CLI (`--sweep NAME=V1,V2`) and the API (`LabelledSpec.sweep`) so both produce
identical labels and obey one cap. Expansion happens **server-side** for the same reason.

Because swept specs differ *only* in detector params, they ingest identically — so
`evaluate` memoizes the normalize phase across the specs of one call, keyed by case plus
source chain. That is the payoff of ADR-0021's normalize/compute split: a 3-value sweep of
SKAB's 16 recordings went from 10.3 s to 7.4 s (**-28%**) by parsing 163k rows once instead
of three times, for byte-identical scores. The memo is scoped to the call, not the process,
so a corpus's events never outlive the benchmark that asked for them.

### D7 — The dashboard makes the split the page's central choice

Per selected detector, an expander showing either its plain knobs (no `artifact_param`) or
the two exclusive modes. Choosing "fit from scratch" makes the fit protocol load-bearing,
so the page blocks the run and says why — `none` plus an untrained model would score a row
of zeros that reads as "this detector is bad" when the truth is that it never ran.

A single-parameter numeric sweep renders as a **curve** (event F1 against the swept value)
rather than bars. Three bars labelled `[window_size=20]`, `[=60]`, `[=120]` make the reader
decode the ordering out of the labels; a quantitative axis answers the question the sweep
was run for — does this parameter matter, and where does it peak? A flat line is a real
result and a cheap one.

## Consequences

**Measured on `skab_valve1`, 16 recordings, `--fit-protocol prefix`:**

| spec | evF1 | evP | evR | ptF1 | det/inc | alt/inc |
|---|---|---|---|---|---|---|
| `team3_scratch[window_size=20]` | 0.43 | 0.32 | 0.75 | 0.41 | 9.19 | 2.38 |
| `team3_scratch[window_size=60]` | 0.47 | 0.37 | 0.81 | 0.46 | 8.69 | 2.31 |
| `team3_scratch[window_size=120]` | 0.48 | 0.38 | 0.75 | 0.51 | 8.69 | 2.44 |
| `stub` (control) | 0.52 | 0.35 | 1.00 | 0.52 | 1135.12 | 5.00 |

Two findings worth recording. **`window_size` barely matters** on this corpus — 0.43 to
0.48 across a 6× range, so the shipped 60 is not a lucky choice and tuning it is not where
the gain is. And **fitting from scratch at `window_size=60` reproduces the artifact-fitted
run exactly** (0.47/0.37/0.81/0.46/8.69/2.31), which is the strongest available evidence
that the v1→v2 migration changed nothing.

**Backward compatible at the config surface.** Every demo and both original bench specs name
only `artifact:`, inherit its hyperparameters, and produce identical results; all seven
demos re-run to their pinned counts.

**What this costs.** A learning adapter is now three files (params, state, detector) rather
than one, and its factory needs one `None`-defaulted parameter per hyperparameter mirroring
its params model. That mirror is the residual duplication this design accepts: coercion and
validation live on the model (a `BeforeValidator` handles the UI's `"1, 5, 10"` text), but
the *names* are still written twice, and a field added to the model without a matching
factory argument is silently unreachable from config. Removing it means making
`params_type` load-bearing in `build_detector` — worth doing when a second learning adapter
arrives and the mirror has to be written a second time.

**What it buys the next adapter.** A LightGBM or LSTM-AE detector declares its
hyperparameter model and its artifact param, ships whatever serialization its weights
actually need (a booster dump, a `.pt` file — there is deliberately no shared artifact base
class), and gets config, `/modules`, sweeps, fit protocols, the port suite and the dashboard
for free. Nothing in `eval/` names a detector; the coupling surface is the factory signature
and `fit()`, and this ADR is what makes those two mean the same thing for every adapter.

**Deferred.** `Case.holdout` is still unreachable (no `CaseManifest` field), so the
`holdout` protocol remains inert. Multi-parameter sweeps render as bars rather than a
surface, and `sweep_curve` recovers the swept parameter by parsing the report's spec label
rather than reading structured data — cheap and safely degrading, but the label format is
an undeclared contract between `eval/sweep.py` and the dashboard; a `SpecReport.sweep`
field is the deeper fix. `_bench_spec` still hardcodes the `stub` normalizer (the wall a
log corpus will hit), and the dashboard prefills a per-detector default artifact id from a
small table because `/eval/detectors` does not yet enumerate each adapter's shipped
artifacts — the same class of gap, and both are UI convenience rather than contract.
