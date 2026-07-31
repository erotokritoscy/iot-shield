# Detectors

A detector reads `NormalizedEvent`s and returns `DetectionResult`s. It is the layer that
decides something is worth looking at, and it is the only layer that holds a model.

Read `docs/guide/architecture.md` first. This guide assumes you know what a port, a lane, a
payload and a capability are.

## What you get and what you owe

The port is two properties and one method:

```python
class Detector(Protocol):
    @property
    def lane(self) -> Lane: ...
    @property
    def method(self) -> str: ...
    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]: ...
```

Four laws, enforced for every registered adapter by `tests/ports/test_detector_port.py`:

1. **Every detection carries this detector's `lane` and `method`.**
2. **Detection windows lie inside the time range of the input events.** You may not report a
   window you were not shown.
3. **`score > threshold`, strictly.** "Nothing was wrong" is a successful judgment with zero
   detections, never a failure and never an exception.
4. **Same input, same output**, apart from generated ids. Including under a shuffle: the
   suite reorders the input and demands the same set of judgments.

Plus one rule the suite cannot test, which matters more than it looks:

**A `DetectionResult` is one judgment over a window, not a row label.** If your model scores
rows, merge consecutive flagged rows into a span before reporting. The alert engine's
`volume` factor counts detections in a bucket as independent corroboration, so restating one
finding as forty detections feeds the same evidence in twice, once through `score` and again
through the count, and measurably inflates the alert's priority. There is no general test for
"distinct", so the evaluation harness measures detections-per-incident instead and lets you
see the cost.

Two optional capabilities exist, both opt-in, both checked with `isinstance`:

- **`Fittable`** gives you `fit(events) -> Detector`. The pipeline never calls it. It exists
  so an evaluation can learn an asset's normal from that asset's own clean history.
- **`Diagnosable`** gives you `why_silent(events) -> str | None`, for a detector that
  declines whole assets by design.

Not implementing either is not a defect. `log_burst` implements neither and is a first-class
detector.

## The six that ship

| Registry name | Lane | Payload | `method` | Learns? | Knobs |
|---|---|---|---|---|---|
| `stub_threshold` | telemetry or flow | | `stub_threshold` | no | `threshold`, `lane` |
| `team3_zscore` | telemetry | | `team3_zscore` | yes | 7 + `artifact` |
| `log_burst` | log | | `log_burst:<preset>` | no | `preset`, `window_minutes`, `min_count` |
| `flow_burst` | flow | `flow` | `flow_burst:<preset>` | no | `preset`, `window_minutes`, `threshold` |
| `meter_iforest` | telemetry | `meter` | `meter_iforest` | yes | 7 + `artifact` |
| `rules` | any (config) | follows lane | `rules:<lane>` | no | `lane`, `ruleset`, `merge_gap_minutes` |

Try them. Two of the six run on the product path, in the demo walkthrough:

```
uv run python scripts/make_demo_data.py
```

then upload what it writes on the dashboard's **Operate -> Compose** page, or name the files
in a config of your own. Its two runs, so you can tell whether a change broke something:

| run | events | malformed | detections | alerts | strongest |
|---|---|---|---|---|---|
| `camera_flow.log`; `rules` on flow | 12 | 0 | 3 | 2 | `rules:flow` score 2.55 vs threshold 1 |
| all three files; `rules` on flow and on log, plus `stub_threshold` | 110 | 2 | 8 | 2 | `rules:flow` score 2.55 vs threshold 1 |

`tests/pipeline/test_runner.py::TestWalkthroughDemo` pins both rows by exact count, which is
what stops the demo rotting quietly between rehearsals.

The other four ship no demo input, and that is deliberate rather than a gap: what makes a
statistical detector convincing is *labelled* data, not a five-minute story. Each has a
committed bench spec instead, run through `uv run python -m iotshield.eval`:

| detector | arguments |
|---|---|
| `flow_burst` | `--corpus fleet_scan --spec config/bench/flow.yaml` |
| `log_burst`, beside the other two | `--corpus device_trimodal --spec config/bench/trimodal_all.yaml` |
| `meter_iforest` | `--corpus meter_fleet --fit-protocol prefix --spec config/bench/meter.yaml` |
| `team3_zscore` | `--corpus skab_valve1 --fit-protocol prefix --spec config/bench/team3_scratch.yaml` |

The SKAB corpora are GPLv3 and therefore not committed: `eval fetch skab` converts a local
checkout, and an absent corpus is a skip rather than a failure. `docs/guide/evaluation.md`
reads the tables these print.

## Two families, and why both

Five of the six answer *is this unusual?* One of them, `rules`, answers *is this a
known-bad thing?*

They fail in opposite directions. Statistics catch what nobody anticipated but cannot say
what it is. Signatures say exactly what something is but only catch what somebody already
wrote down. A product with only the first tells an analyst "this device's traffic is 8 sigma
above normal, good luck"; a product with only the second is blind to anything new.

The clearest single example is in `library/ssh-possible-break-in.yml`: sshd logs its
break-in warning at INFO severity, once. Unusual-severity is blind to it and unusual-rate is
blind to it. Only a signature written by someone who knew the line mattered will ever catch
it.

## The shared skeleton

Three modules hold the parts every detector would otherwise re-derive. Read these before
writing an adapter, because most of a new detector is choosing which of them applies.

### `_group.py` owns the routing laws

```python
for asset_id, asset_events in group_by_asset(events, lane=self.lane, keep=self._judgeable):
```

That one call gives you foreign-lane isolation, a deterministic asset order, and each
asset's events in time order. Only `keep` is yours: which of your own lane's events are
judgeable at all.

The time sort is the load-bearing part. A windowed detector fed unordered events does not
crash, it lies: the trailing edge of the window never advances when a timestamp goes
backwards, so the window grows without bound and every position qualifies. That is a
detector that fires on everything while passing every other law. Every adapter used to
carry its own defensive sort, and the one that forgot would have shipped exactly that.

### `_windows.py` owns "too much of something, too fast"

One question, several aggregates:

| aggregate | measures | preset |
|---|---|---|
| `CountAggregate` | how many events are in the window | `log_burst` |
| `DistinctAggregate(key)` | how many distinct keys | `fan_out`, `port_scan` |
| `SumAggregate(quantity)` | a running total | `exfiltration` |

`find_episodes` walks two pointers over time-ordered items, keeps the aggregate describing
the widest window ending at the right edge, and merges every qualifying window that overlaps
the previous episode into that episode. An aggregate is incremental (`add`/`remove`) so the
walk stays linear; recomputing "distinct peers" at every position would make a long capture
quadratic.

The merge is what makes one incident one judgment. Ninety auth failures inside a five-minute
window qualify at almost every position and describe one event.

### `_runs.py` owns spans and shapes

- `merge_runs(mask)` collapses a per-row boolean verdict into inclusive index spans.
  `[F,T,T,F,T]` yields `(1,2)` then `(4,4)`.
- `group_by_gap(items, gap, timestamp)` splits already-interesting items wherever more than
  `gap` separates two neighbours.
- `classify_run(length)` turns a span length into an `EventKind`: 1 is `POINT`, 2 is
  `CONTEXTUAL`, 3 or more is `COLLECTIVE`.
- `sample_messages(events)` takes the non-empty messages among the first three, for evidence —
  it caps first and filters second, so an episode opening with a message-less event yields two.

### Three ways to form an episode, and they are not interchangeable

This is the decision people get wrong.

| helper | starts from | adjacency unit | used by |
|---|---|---|---|
| `find_episodes` | raw items | a sliding time window over an aggregate | `log_burst`, `flow_burst` |
| `merge_runs` | a per-row verdict already computed | index adjacency | `team3_zscore`, `meter_iforest` |
| `group_by_gap` | items that are all already interesting | elapsed time between neighbours | `rules` |

Picking index adjacency where you needed a time gap is a real bug, not a style choice. A
device's flows interleave (mqtt, smb, ssl, smb), so matches of one rule are almost never
neighbours in the sequence. An adjacency merge over them emits one detection per match,
which is the exact double-counting the merge exists to prevent.

## Score, threshold and the exceedance boundary

The port's law is a strict `>`, so the threshold you report is the **exceedance boundary**:
the largest value still considered normal. The point is that the number an analyst reads on
the detection is the same number that decided it.

The two burst detectors differ here, deliberately:

- `log_burst`'s `min_count` is a trigger, "how many are needed". It reports
  `min_count - 1`, so `min_count: 5` reads as "12 events vs threshold 4".
- `flow_burst`'s `threshold` already *is* a boundary, so it reports it unchanged:
  "43 peers vs threshold 20".

A non-positive threshold is refused at construction by every adapter that takes one. Two
reasons: it would make every window qualify, and it sends the alert engine's
`detection_margin` down its degenerate branch where it scores an arbitrary 2.0.

`rules` has no natural score at all, because a rule match is a boolean. It synthesises one
from the two judgments the rule's author already made: `severity_hint` sets a base (1.5 to
4.0) and `confidence` scales it (0.7 to 1.0), against a fixed `threshold` of 1.0. The table
is calibrated so the weakest combination sits just above the floor and the strongest lands
exactly on the alert engine's cap of 4.0.

## Evidence

`evidence` is a free-form JSON object with exactly one required key.

**`explanation` is a law.** One analyst-readable sentence, from every adapter, enforced by
`test_every_detection_explains_itself`. It is the only field every consumer can render, and
the dashboard names findings by it. The supporting detail stays per-detector, because a
z-score, a feature attribution and a peer count are genuinely different things.

Two formatting traps that both shipped and both had to be fixed:

- `:g` gives six significant digits and switches to exponent form. `exfiltration`'s boundary
  is 50,000,000, so every detection that preset could emit rendered as "5e+07 bytes",
  discarding the real figure in the one sentence anybody reads. Use `,.0f` for quantities.
- Six significant digits also round two nearby numbers into equality. A reading of 80.00001
  against a threshold of 80.0 read "80 is above the configured threshold of 80", which is
  self-contradictory. `stub.py` uses `:.12g`.

If your detector knows what it detected, put a typed `Enrichment` in evidence under
`EVIDENCE_KEY` rather than a bare string. That is what carries a threat class and a MITRE
mapping through to the alert, and it is why `log_burst`'s and `flow_burst`'s presets each
carry a required `enrichment` field: making it required puts the invariant in the type, so
the next preset's author has to answer the question instead of leaving the alert queue split
into "explained" and "unknown".

## Hyperparameters and fitted state

This is the one rule the project calls THE LAW, and it applies to every adapter that learns
anything. It exists because a serialized model carries a human's choices and a fitting
procedure's output in one file, and every format encourages mixing them.

1. **Hyperparameters are exactly the adapter's construction parameters**, declared as a
   frozen pydantic model on `AdapterSpec.params_type`. So config sets them, `/modules`
   reports them, and the evaluation harness can sweep them.
2. **`fit(events)` recomputes learned state from `events`, using the hyperparameters the
   instance was CONSTRUCTED with.** It must never read a hyperparameter out of its own
   previous fitted state.
3. **A persisted artifact is atomic.** It records its hyperparameters, and an explicit
   override that contradicts them is a construction error.

Rule 2 is enforced behaviourally, which is what makes it general: build the adapter twice
through the registry differing in one hyperparameter, fit both on identical data, and the
results must differ. An adapter that read its hyperparameters from a stale artifact would
produce two identical detectors and fail.

`fit` returns a **new detector** rather than mutating `self`, so the determinism law
survives and one evaluation case's fit can never leak into another's.

Whether the law applies is derived from the registry, not from the test case. A learning
adapter that supplies no `distinguishing_params` fails rather than skipping, because
otherwise an adapter could exempt itself from THE LAW by leaving a field unset.

## Artifacts

An artifact is a **cached fit**: one file holding the hyperparameters a fit used and the
state that fit produced. Two ship, and their ids are a closed vocabulary the registry knows.

Both are **pure JSON, never a pickle.** Human-readable, git-diffable, and free of sklearn
version fragility. Both load through `importlib.resources`, so the path is package-relative
rather than working-directory-relative. Both are `@cache`d, because package resources are
read-only at runtime and a benchmark builds its detectors once per case.

### The file's shape is the ADR-0022 split, made visible

```json
{
  "schema_version": 3,
  "params":  { "window_size": 60, "percentile": 99.0, "...": "what a human chose" },
  "models": [ { "asset_id": "skab-testbed", "...": "what fitting computed" } ]
}
```

You can open the file and tell the two halves apart. That is the whole point. Schema v1
interleaved them, and `fit()` consequently inherited stale hyperparameters from whatever
file happened to be loaded.

`schema_version` is checked **before** the shape, and this ordering is deliberate: an older
layout would otherwise fail on `extra="forbid"` with a list of ten unexpected keys, burying
the one actionable fact. The error names the regeneration command:

```
unsupported team3 artifact schema_version 2 in 'skab_zscore' (expected 3);
regenerate it with `python -m iotshield.detect.team3.train`
```

An old file is **refused rather than upgraded**, even when the mechanics look trivial. The
v2 to v3 upgrade would have to invent the `asset_id` its single state belonged to, and
inventing it wrong is the quietest possible failure: the detector would skip every real
asset and report no detections, which reads as "nothing was wrong".

### One model per asset

Both artifacts hold a **list** of per-asset states, not one state for everything.

A baseline is a statement about one device. Rolling, lag and FFT features read the row
before, so they mean nothing across two devices; meters range from 25% to 94% idle, so a
pooled meter model is dominated by the idle majority. `detect` looks up the asset's own
state and **skips an asset it has no model for** rather than judging it against another
device's normal, which measures the difference between two machines and calls it an anomaly.

`asset_id` lives *inside* each state rather than being a key the state is filed under, so a
model read out of the JSON says which device it describes without reference to its
container. That is also what lets `channels` differ per asset, which is how one artifact can
cover a heterogeneous fleet.

### The two designs differ in what they persist

This is the most interesting decision in the module, and the two adapters landed on opposite
answers for good reasons.

| | `team3_zscore` | `meter_iforest` |
|---|---|---|
| id | `skab_zscore` | `demo_meter` |
| schema | v3 | v1 |
| size | 4.7 KB | 228 KB |
| persists | the **fitted weights** (median, MAD, threshold) | the **clean training rows** |
| loading | reads the numbers | **re-fits the forest** |
| cost at load | nothing | about 120 ms per meter |
| threshold | stored | **re-derived on load** |

team3's model *is* a handful of numbers, so storing them is both smaller and reviewable.

An Isolation Forest is 200 trees of opaque arrays. As JSON that is megabytes of
unreviewable numbers; as a pickle it is a binary blob whose validity is tied to one sklearn
version. Neither is something a reviewer can read, and readable was the whole point of the
format. So the meter artifact stores the training matrix instead: it is smaller than the
model it produces, it is exactly what a reviewer would want to see, and re-fitting from it
is deterministic given `random_state`.

The part that makes this safe rather than clever is that the **threshold is re-derived on
load** from those same rows plus `fp_budget`. Model and threshold are recomputed together,
so a future sklearn that builds slightly different trees produces a matching threshold
rather than a silently mismatched one. Nothing to migrate, nothing to pin.

The cost is real and worth stating. `load_artifact` is cached per process, so a 1,656-meter
fleet is minutes of CPU on every cold start: each API worker, each container restart. And
the artifact carries data, which means a committed artifact is only appropriate for data we
may commit. Our demo meter is synthetic, so it is fine. A customer's meter would be fitted
at deployment, which is the deployment shape for metering anyway.

### How a detector declares its artifact

Three declarations, all in the registry entry:

```python
"team3_zscore": AdapterSpec(
    _team3_zscore,
    static(_TEAM3_CAPABILITY),
    params_type=Team3Params,                    # WHICH knobs are hyperparameters
    artifact_param="artifact",                  # WHICH factory param names a cached fit
    discovered={"artifact": _team3_artifacts},  # WHICH ids exist, asked when needed
),
```

Each answers a different question a caller has without constructing the adapter.

`params_type` exists because a learning adapter's factory params must all default to `None`
(the "not given" sentinel `resolve_params` needs), which would otherwise make `/modules`
report every knob as having no default and leave the UI nothing to prefill. Introspection
reads the true defaults from the pydantic model instead.

`artifact_param` is **declared rather than name-sniffed**, so a UI can offer the real choice
as two modes: "load trained weights" or "fit from scratch with my own hyperparameters".
Without it someone combines the two and hits the atomicity error.

`discovered` is the values, and it is one channel for every vocabulary the registry can
*enumerate* but a signature cannot express. Artifact ids are files in a package; rule sets are
directories on the deployment's shelf. Neither can be a `Literal[...]`, so both are published
as that parameter's `ParamInfo.choices` and any form that already renders a closed vocabulary
as a dropdown renders them identically. Before it existed, the two shipped artifact ids lived
nowhere a client could read them and the Evaluate page carried a hardcoded TypeScript table
listing one of the two.

It holds **callables**, not lists, and that is the difference between the two kinds of
vocabulary. Package resources cannot change while a process runs, so an artifact list cached on
first use stays true for that process; a shelf the operator is invited to drop directories into
cannot make that promise. Resolving per call means adding a rule set is creating a directory, visible on the
next page load with no restart (ADR-0037).

Live, across all six:

```
stub_threshold  params_type=None          artifact_param=None        discovered={}
team3_zscore    params_type=Team3Params   artifact_param='artifact'  discovered={'artifact': ['skab_zscore']}
log_burst       params_type=None          artifact_param=None        discovered={}
flow_burst      params_type=None          artifact_param=None        discovered={}
meter_iforest   params_type=MeterParams   artifact_param='artifact'  discovered={'artifact': ['demo_meter']}
rules           params_type=None          artifact_param=None        discovered={'ruleset': ['packaged']}
```

An empty result publishes **no** choices rather than an empty dropdown, so an adapter packaged
without its artifacts falls back to a free-text box instead of a `<select>` with nothing in it.

### The atomicity refusal, in practice

```
$ artifact: skab_zscore  +  window_size: 30

error: invalid params for detector 'team3_zscore': cannot override ['window_size'] while
loading a trained artifact - its weights are aligned to the features its own
hyperparameters produced. It was fitted with:
      window_size: 60
      lag_steps: [1, 5, 10]
      ...
    Drop the artifact to fit from scratch (and pick a fit protocol), or drop the overrides.
```

Quoting the artifact's own params turns the refusal into two seconds of editing rather than
a hunt through a JSON file, which is most of what makes an absolute rule tolerable. The
block goes through the same YAML writer the configs are read with, so it is paste-ready.

### Training: the three ways a detector gets fitted

Before the commands, be clear about which one you want, because only one of them writes a
file:

| Route | Who calls it | Writes a file | Use it for |
|---|---|---|---|
| `detector.fit(events)` | your own code, or the evaluation harness | no | experiments, hyperparameter sweeps, fitting at deployment |
| the training CLI | you, once, by hand | **yes**, one JSON | a model you want to commit, ship, review or reuse |
| `artifact: <id>` in config | the pipeline, at construction | no, it reads one | every normal run |

**A pipeline run never fits anything.** `fit()` is not part of the Detector port and the
runner never calls it. A run either loads an artifact or raises `NotFitted`. So "training" is
always something you did earlier, deliberately, by one of the first two routes.

The training CLI is just route 1 plus reading a file and writing a file. It exists so that
the fit is reproducible and reviewable rather than living in somebody's notebook.

### The two trainers

Each learning adapter ships one, as a module you run directly. With no arguments both
regenerate exactly the artifact that is committed today:

```
$ uv run python -m iotshield.detect.team3.train
wrote .../team3/artifacts/skab_zscore.json - 45 features for skab-testbed
  (from 570 normal rows), threshold=24.5395 (p99)

$ uv run python -m iotshield.detect.meter.train
  meter 202405109001: 1636 clean hour(s), threshold=0.74362 (p99.86)
wrote .../meter/artifacts/demo_meter.json - 1 meter(s) at fp_budget=0.0014
```

Byte-for-byte the same files, which is worth knowing and worth keeping true: it means anyone
can re-derive a shipped model from the shipped inputs and diff the result. The two thresholds
printed there are the ones every detection from either adapter reports (`threshold 24.54`,
`threshold 0.7436`), so a number on an alert traces back to a command you can run.

Flags, and what each is for:

| `team3.train` | Default | |
|---|---|---|
| `--source` | SKAB `valve1/13.csv` in the reference tree | the recording to fit on |
| `--out` | `team3/artifacts/skab_zscore.json` | where to write |
| `--window` | 60 | `window_size`, the feature window |
| `--percentile` | 99.0 | which quantile of training scores becomes the threshold |
| `--asset` | `skab-testbed` | **which device this model describes** |

| `meter.train` | Default | |
|---|---|---|
| `--source` | `data/demo/demo_meter_history.csv` | a known-clean export |
| `--source-format` | `eoa-meter-csv` | which meter dialect the export is in |
| `--out` | `meter/artifacts/demo_meter.json` | where to write |
| `--fp-budget` | 0.0014 | the false-alarm budget the threshold is derived from |
| `--n-estimators` | 200 | trees in the forest |

`data/demo/demo_meter_history.csv` is the one hand-authored file left under `data/demo/`;
everything else there is written by `scripts/make_demo_data.py`. It survived the demo rebuild
for a specific reason: it is the *sole* input to the committed artifact, which stores the
clean history rather than the weights, so deleting it would have made a shipped, tested model
permanently unregenerable.

`--asset` on team3 is load-bearing rather than cosmetic. A model judges only the asset it is
filed under, so this value has to match the `asset_id` in the data you will later run
against. `skab-testbed` is the id every `skab_*` evaluation bundle carries, which is what
keeps the committed benchmarks running against the shipped artifact rather than skipping it.

The two trainers differ in how many models they produce. team3 fits **one** asset, the one
`--asset` names. The meter trainer fits **every meter it finds in the file**, one model each,
because a clean utility export is naturally a fleet. That asymmetry is not a design
inconsistency; it follows from what each input is.

### Fitting on your own data

For the meter, this is the intended path: point `--source` at your own clean export, name its
dialect with `--source-format`, and you get one model per meter in it. The trainer reads the
file **through the real normalizer**, so an artifact can only ever be fitted on events shaped
the way production events are shaped, and a change to the normalizer cannot silently
invalidate a model.

For team3, read the flags again before you try. Its CLI pins SKAB's eight channels
explicitly, because the committed artifact ships as a ready-to-run model for that specific
rig, so a different recording fails on the way in:

```
ValueError: notskab.csv is missing expected channels: ['Accelerometer1RMS',
'Accelerometer2RMS', 'Current', 'Thermocouple', 'Voltage', 'Volume Flow RateRMS']
```

That is the CLI's limitation, not the adapter's. `Team3ZScoreDetector.fit(events)` learns the
channel list from whatever telemetry it is handed, so fitting a different rig means calling
`fit()` (route 1) and then `model_for` / `Team3Artifact` if you want to persist it. (The meter
adapter's equivalent accessor is `state_for`; the names differ because team3's per-asset record
is a *model* and the meter's is a *state*.) The pure
function underneath, `train_state` (frame plus `Team3Params` in, `Team3State` out, no I/O),
is shared by the CLI and by `fit()`, which is what guarantees the two cannot disagree about
what fitting means.

The meter has no equivalent pure function because `fit()` already *is* the single definition
there, and its trainer is a thin driver: read, normalize, fit, `state_for`, write.

Neither trainer formats its failures. An unsuitable input gives you a Python traceback ending
in the `ValueError` above, not an `error:` line like the pipeline CLI produces. They are
developer tools run by hand a few times, and that is the level of finish they have.

### Where the file goes, and how its id appears

An artifact's **id is its filename** without the suffix, and the directory it must live in is
the adapter's own `artifacts/` package directory. That is the whole registration mechanism.
Dropping a file in is enough:

```
$ cp my_rig.json src/iotshield/detect/team3/artifacts/

>>> DETECTORS["team3_zscore"].discovered["artifact"]()
('my_rig', 'skab_zscore')
```

and `artifact: my_rig` now builds, appears in `/modules` as a `choices` entry on the
`artifact` parameter, and shows up in the dashboard's dropdown with no code change anywhere.
The listing is cached the first time it is asked, so a running process needs a restart to see
a new file.

Both trainers write with `newline="\n"` explicitly. `Path.write_text` translates to
`os.linesep`, so regenerating on Windows rewrote every line of a committed artifact as CRLF
and buried a real one-value change in a 3,000-line diff.

If you change the *shape* of a state, bump `schema_version` in that adapter's `artifact.py`
and retrain. Old files are refused, not upgraded, and the refusal names the trainer to run.
That is deliberate: silently reading a stale model is the failure mode this format exists to
prevent.

## Untrained is a state, not an empty answer

An adapter built from hyperparameters alone has nothing to judge with. `detect()` raises
`NotFitted` rather than returning `[]`:

```
NotFitted: team3_zscore was built from hyperparameters with no trained baseline;
name an 'artifact' in its params, or fit it first (--fit-protocol prefix)
```

An empty list is a *successful* judgment meaning "no anomalies here", which is the most
dangerous wrong answer a detector can give. A run configured with an untrained model is
refused before ingest; a benchmark reports it as a note.

Reporting which state you are in goes through a separate one-member protocol, `Stateful`,
rather than a member on `Fittable`. `Fittable` is `runtime_checkable` and matches on
attribute presence, so adding `is_fitted` to it would silently reclassify an adapter that
implements only `fit` as "not fittable". The helper `is_fitted(detector)` returns `True` for
anything that does not declare it, which is the right default: a rule-based detector has no
state to learn and is always ready.

## Declining to judge, out loud

Some detectors correctly judge nothing. `meter_iforest` skips a meter it has no model for;
`team3_zscore` skips an asset it was never fitted for, and an asset whose trained channels
the data does not carry. Both are the
right behaviour and both are invisible, because the run reports zero detections, which reads
exactly like "nothing was wrong". An operator cannot tell "these assets are fine" from
"these assets were never looked at".

`why_silent` is the answer. It is a **pure predicate over the same events `detect` would
receive**, not state recorded during a run, so the detector stays stateless per call, the
caller decides when to ask, and the answer cannot go stale.

Two layers catch this, and it is worth seeing both fire on one config. Feeding bare numeric
telemetry to `meter_iforest`:

```
! [detectors[0]] detector 'meter_iforest' judges only events carrying meter payload(s),
  which no configured source declares it produces (nothing); its lane matches, so it will
  be handed events and discard all of them
! meter_iforest judges consumption readings carrying a 'meter' payload; none of the 3
  telemetry asset(s) here carry one, so nothing was scored - this input is telemetry but
  not metering
```

The first is the static capability guard, before ingest, from declarations alone. The second
is `why_silent`, after the run, from the events actually seen. The guard catches the config;
`why_silent` catches everything that bypassed the guard, which includes a hand-written YAML
and an evaluation bundle.

A live example, from pointing the shipped team3 artifact at a rig it was never fitted for:

```
$ uv run python -m iotshield.eval --corpus synthetic --spec config/bench/team3.yaml
  ! team3_zscore judged nothing on some assets: it has no fitted model for synthetic-rig -
    a baseline is fitted per asset, so an asset absent from the fit is not judged
```

Eleven rows of `0.00` arrive with that line attached. Without it the table is
indistinguishable from a detector that ran and was useless, and the reader's next move would
be to tune a model that was never consulted.

One rule worth copying from `team3`: `why_silent` and `detect` read their skip condition
from **the same function** (`_uncovered`), because the port requires them to answer
identically. One rule with two encodings that disagreed is exactly the failure ADR-0034 was
written about.

And note what `why_silent` deliberately does not say. It reports that there is no model for
an asset without asserting why, because the detector genuinely does not know: it may never
have been trained on that asset, or `fit` may have refused it for a short history. Naming a
cause it cannot see would be the same failure one level in.

## Presets: the cheapest new detection

If your idea is "the same mechanism measuring something else", it is a preset, not an
adapter. A `PRESETS` entry in `log_burst.py` or `flow_burst.py` is about fifteen lines, plus
the key in that factory's `Literal[...]` in the registry — the two are mirrored by hand, and
`test_every_preset_adapter_declares_its_presets_as_choices` fails if they drift, because a
preset the `Literal` does not list is unreachable from the UI even though the adapter supports
it. Once listed, `/modules` publishes it and the compose page renders it as a dropdown option.

A `flow_burst` preset supplies an aggregate, a direction filter, its own window and
threshold, and a required enrichment:

```python
"dns_flood": Preset(
    describe="DNS queries sent",
    unit="queries",
    aggregate=lambda: CountAggregate(),
    window_minutes=1.0,
    threshold=200.0,
    keep=lambda record: record.peer_port == 53 and record.direction in _ASSET_INITIATED,
    enrichment=Enrichment(...),
),
```

Each preset carries its **own** window and threshold rather than sharing a default. Scanning
is a seconds-to-a-minute phenomenon and exfiltration a minutes-to-hours one; 20 distinct
peers per minute and 50 MB per half hour are both right and neither is right for the other.

The preset rides in `method` (`flow_burst:dns_flood`) so two configured instances stay
distinguishable in a detection and in the alert rationale. That is the port's rule about
`method`: it names *what judged*, so a param selecting which rule decides belongs in the
name, while a param that only tunes the same rule's sensitivity does not.

## Rules: a detection with no Python

This is the extension path to reach for first, and the one most likely to keep this repo
useful after handover. A rule is a YAML file. Drop it in a directory, name that directory in
config, done. No Python, no port suite, no rebuild.

See the vocabulary and the shipped library:

```
uv run python -m iotshield.detect.rules
```

A complete working rule (this one loads and scores 1.7 against a threshold of 1.0):

```yaml
id: external-dns-egress
title: Device Talking DNS To An Outside Resolver
lane: flow
description: >-
  The device sent DNS to a resolver outside the monitored networks. IoT devices should use
  the local resolver, and direct external DNS is a common tunnelling and beaconing channel.
one_line: An outbound flow to port 53 on a host outside the home networks.
severity_hint: medium
confidence: medium

detection:
  selection:
    flow.peer_port:
      operator: "=="
      value: 53
    flow.direction:
      operator: "=="
      value: outbound
  condition: selection

mitre:
  - tactic: Command and Control
    technique: Application Layer Protocol
    technique_id: T1071

suggested_actions:
  - Check whether the device is configured to use the local resolver.
  - Review the queried names for tunnelling patterns.
```

Required keys are `id`, `title`, `lane`, `description`, `severity_hint`, `confidence` and
`detection`. A selection is a set of field/operator/value triples ANDed together; `condition`
is flat, so one name, or `A OR B`, or `A AND B`. No parentheses, no NOT.

Put it on the **shelf** - one directory the deployment designates, holding one subdirectory per
rule set - and name that rule set:

```
config/rules/
  my-rules/
    suspicious-dns.yml
```

```yaml
detectors:
  - name: rules
    params:
      lane: flow
      ruleset: my-rules
```

`ruleset` is a **name, not a path** (ADR-0037). The server resolves it against its own shelf and
refuses anything not on it, because the same pipeline spec arrives from a YAML file *or* from a
`POST /runs` body - so a path honoured here would let any caller aim the rule loader at a
directory on the server. The names on the shelf are what `/modules` publishes as this param's
choices, which is what makes the dashboard offer a dropdown instead of an empty text box.

`ruleset: packaged` is the default: the library that ships inside the product. Where the shelf
is depends on the deployment - `config/rules` in a checkout, `IOTSHIELD_RULES_PATH` overrides,
and `docker-compose.yml` mounts it read-only into the Pipeline API. Adding a rule set to a
running stack is creating a directory; it appears on the next page load, no restart.

Resolution is membership, not sanitising, which is why a typo, a traversal and an absolute path
all fail identically and say what exists:

```
ruleset: nope        -> unknown rule set 'nope'; available: packaged. A rule set is named,
ruleset: ../../etc      not pathed: it is a directory on the shelf the deployment configures
ruleset: C:/Windows     (IOTSHIELD_RULES_PATH)
```

Sanitising a path would have to answer "is this one safe?" for every spelling of a traversal;
membership only has to answer "is this one of mine?".

Seven operators (`==`, `!=`, `>`, `<`, `>=`, `<=`, `contains`) and no regex, deliberately.
`contains` covers every signature actually needed, and an operator-authored regex in YAML is
one config typo away from being pathologically slow or wildly over-matching with nothing
between it and the run.

**The field vocabulary is closed.** There is no escape hatch for addressing arbitrary
metadata paths, because an open path would re-open the silent-failure hole for the
least-defended audience: someone editing YAML with no type checker, no tests and no error
message. A source needing a field we do not model is a request to add one reviewed entry to
`rules/fields.py`, and that table reads *through* `FlowRecord` rather than into `metadata`,
so if `FlowRecord` renames a field the table stops type-checking and CI catches it.

Everything is checked at load, and every check names the file:

```
invalid rule in my-rules/broken.yml: unknown field 'flow.bytes_sent' for lane 'flow'.
Did you mean 'flow.bytes_in', 'flow.bytes_out', 'flow.packets_in'? Known fields: ...

invalid rule in my-rules/broken.yml: field 'flow.service' holds a text, so operator '>'
does not apply to it; allowed: !=, ==, contains
```

Four behaviours here are stricter than the upstream engine this grammar came from, each
replacing a silent or late failure:

1. **A missing value never matches, whatever the operator.** Upstream, `!=` against an
   absent field returns True, so `flow.service != "smb"` fires on every event with no
   service at all. Absence is not a positive claim.
2. **Type mismatches are refused at load**, not raised as an uncaught `TypeError` mid-run.
3. **A condition naming a selection that does not exist is refused at load**, not as a
   `KeyError` on the first event in production.
4. **Files load in sorted order**, because "same input, same output" is a port law and a
   directory listing is not a guarantee of anything.

Three more loading rules worth knowing:

- **Every file is compiled, even ones for other lanes.** A broken log rule fails a flow-lane
  run too. A malformed file in a shipped rule set is a defect, and the alternative hides it
  until somebody happens to configure that lane.
- **Duplicate ids are refused**, because an alert attributes its threat class to one id.
- **A lane with no rules is refused at construction**, since the only symptom would
  otherwise be a detector that finds nothing.

The packaged library is memoized; a `ruleset` directory is deliberately not, so an operator
editing a rule file against the long-lived Pipeline API does not have to restart.

## Adding a detector

Six parts, and the last two are the ones people forget.

1. **The adapter.** A module (or package) in `detect/`. Use `group_by_asset` for routing and
   whichever of the three episode helpers fits. Refuse impossible configuration at
   construction, not at `detect()`: `stub_threshold` refuses `lane: log` because a LOG event
   has no `value`, and a guard that certifies a chain which can never fire is worse than no
   guard.

2. **The capability**, co-located in the same module:

   ```python
   CAPABILITY = Capability.of(
       consumes={LaneToken(Lane.FLOW), PayloadToken(METADATA_KEY)},
       produces={DETECTIONS},
   )
   ```

   Declare a `PayloadToken` if you read a typed vocabulary out of `metadata`. Declaring one
   is a promise the port suite enforces: `test_a_declared_payload_is_actually_required`
   strips the payload from the triggering events and requires you to judge nothing. The
   guard reports a payload mismatch as a WARNING rather than an ERROR precisely on the basis
   that nothing breaks, so an adapter that indexed `metadata["meter"]` directly would crash
   on a config the guard deliberately allows.

   If your lane depends on config, write `capability(params) -> Capability` instead. `rules`
   is the clearest case: `rules(lane: flow)` reads every field through `FlowRecord` and needs
   the payload, while `rules(lane: log)` reads the contract's own fields and needs none. One
   adapter, two different plug shapes.

3. **The registry entry** in `pipeline/registry.py`. Annotate closed vocabularies as
   `Literal[...]` so `/modules` publishes them and the compose page renders a dropdown. If
   the adapter learns, add `params_type`, `artifact_param` and a `discovered` entry for the
   artifact param — the same channel every found-rather-than-declared vocabulary travels down.

4. **Export it** from `detect/__init__.py`.

5. **Join the port suite.** Add a `DetectorCase` with `triggering_events` guaranteed to fire.
   A meta-test asserts every registered detector has a case, so skipping this fails by name.
   If you implement `Fittable`, also supply `fit_events` (usually different from the
   triggering ones, since events chosen to fire are rarely fit to train on) and
   `distinguishing_params`, two registry param sets differing in one hyperparameter.

6. **Emit `explanation`.** In evidence, on every detection. It is a law, and four of six
   adapters did not do it until the test existed.

Then `scripts/check.sh`.

## Config surface

```yaml
detectors:
  - name: flow_burst
    params:
      preset: fan_out
      window_minutes: 1.0
      threshold: 20.0
  - name: meter_iforest
    params:
      artifact: demo_meter
```

Several detectors on several lanes in one run is the normal case, and one adapter may appear
twice with different params. Every bad param value raises at construction, before any input
is read, and surfaces as `invalid params for detector 'x': ...`.

Some are worth knowing by sight:

| what you typed | what you get |
|---|---|
| `artifact: nope` | `unknown team3 artifact id: 'nope'` |
| `artifact` plus any knob | the atomicity refusal, quoting the artifact's params |
| `lane: log` on `stub_threshold` | `compares a numeric reading, which the 'log' lane never carries` |
| `lane: telemetry` on `rules` | `holds no 'telemetry' rules ... available for: flow, log` |
| `preset: typo` | `unknown log_burst preset 'typo'; known: auth_failure, severity_spike` |
| `min_count: 1` | `min_count must be >= 2 (a burst is 2+ events)` |
| `min_train_hours: 200` | `too short for fp_budget (0.0014) ... needs at least 883 hours` |

That last one is worth reading in full, because it is the shape a good config error takes:
it names both knobs, does the arithmetic, and gives two ways out ("raise `min_train_hours`
to 883, or loosen `fp_budget` to at least 0.03125"). It exists because two gates on "enough
history" were independent constants and disagreed, so every meter in the gap passed the
first and raised inside the second, which on a fleet threw away every meter already fitted
in that call.

## Where to look

| | |
|---|---|
| The port, its laws, the two optional capabilities | `src/iotshield/detect/port.py` |
| THE LAW and `resolve_params` | `src/iotshield/detect/_fitted.py` |
| Routing laws | `src/iotshield/detect/_group.py` |
| Sliding-window episodes and aggregates | `src/iotshield/detect/_windows.py` |
| Row masks, time gaps, episode shapes | `src/iotshield/detect/_runs.py` |
| Simplest adapter | `src/iotshield/detect/stub.py` |
| Preset pattern, richest evidence | `src/iotshield/detect/flow_burst.py` |
| Artifact holding weights | `src/iotshield/detect/team3/artifact.py` |
| Artifact holding training data | `src/iotshield/detect/meter/artifact.py` |
| Artifact id discovery | `src/iotshield/detect/_artifacts.py` |
| A trainer with a pure fit shared with `fit()` | `src/iotshield/detect/team3/train.py` |
| A trainer that is a driver over `fit()` | `src/iotshield/detect/meter/train.py` |
| Rule grammar | `src/iotshield/detect/rules/model.py` |
| Rule field vocabulary | `src/iotshield/detect/rules/fields.py` |
| The port suite | `tests/ports/test_detector_port.py` |
| The walkthrough's inputs, and the scenario they encode | `scripts/make_demo_data.py` |
| Its two runs, pinned by exact counts | `tests/pipeline/test_runner.py` |
| Vendoring team3, artifacts as JSON | `docs/adr/0012-team3-telemetry-detector-integration.md` |
| The log burst design | `docs/adr/0020-log-burst-detector.md` |
| Hyperparameters vs fitted state | `docs/adr/0022-hyperparameters-and-fitted-state.md` |
| The flow burst design | `docs/adr/0024-flow-burst-detector.md` |
| Meter detector and scikit-learn | `docs/adr/0030-meter-detector-and-scikit-learn.md` |
| Rules and threat enrichment | `docs/adr/0032-rule-detector-and-threat-enrichment.md` |
| `why_silent` and the two silences | `docs/adr/0033-pre-handover-review-fixes.md` |
| One baseline per asset | `docs/adr/0034-a-baseline-describes-one-device.md` |
| The payload axis and artifact ids | `docs/adr/0035-the-lane-is-not-the-whole-shape.md` |
