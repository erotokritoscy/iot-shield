# ADR 0012: Second module integration — team3 telemetry detector, hybrid (borrow the math, rewrite the plumbing)

Date: 2026-07-09 · Status: accepted (approved by Andreas)

## Context
ADR-0011 integrated the first module (team4 syslog) and set the rule for every
integration after it: **wrap** when the module's output preserves what our
contract needs; **rewrite with cited provenance** when it does not. It also named
the next milestone explicitly — "detector activation is what the next integration
(team3) proves." This is that integration: the first *real detector*, and the
moment CLAUDE.md reserved for settling the runtime dependency surface ("the image
is intentionally unhardened until the first real detector (team3) settles the
dependency surface").

The module is `../reference/team3-telemetry-ad` (public clone, IoT time-series
anomaly detection; see `../reference/INDEX.md`). It ships four models (robust
z-score, IsolationForest, LSTM-AE, CNN-AE), engineered + raw feature paths, and
the SKAB/NAB datasets. It has **no tests**.

Reading the code surfaced a different problem than team4's. team4 was
*destructive* (it dropped and fabricated data our contract needs). team3's math
is sound and worth keeping — its problem is that it is **structurally
un-adaptable**:

- its three entry points in `orchestration.py` (1,880 lines) take an argparse
  `Namespace`, read config YAML from the CWD, read datasets from relative paths,
  and emit results as **file side-effects** — the streaming "output" is
  `streaming/alerting.py:append_alert` appending a CSV row; there is no return
  value, no callback, no seam an adapter can sit on;
- it **`import torch` unconditionally** at module top (and in `utils/seed.py`), so
  even the numpy-only z-score path cannot be imported without a full torch stack;
- it runs **global side-effects on import** (`matplotlib.use("Agg")`, global RNG
  seeding);
- its benchmark threshold comes from a **label-aware search over the test set**
  (`configs/skab.yaml` `search:`), which is measurement, not detection.

An adapter cannot wrap any of that. But the *models* and *feature builders* are
tiny, pure (numpy/pandas), and genuinely reusable. So the verdict is the ADR-0011
mechanism with a larger preserved core.

## Decision
**Hybrid: copy team3's pure model + feature math into our tree with cited
provenance; rewrite the orchestration / windowing / artifact / IO layer against
our contract.** Golden rule 3 forbids importing from `../reference/` at runtime,
so "borrow" here means *vendor with provenance*, as syslog did.

Lives in `src/iotshield/detect/team3/`:

**Borrowed from team3** (cited in each module docstring):
- `_features.py` — `fft_window_energy`, lag-step normalization, and the
  multivariate rolling/lag/FFT feature builder (from `preprocessing/features.py`),
  **adapted to preserve the timestamp index** (team3's `reset_index(drop=True)`
  discarded it, which would break mapping a detection back to a real time).
- `_zscore.py` — robust median/MAD baseline and per-row max score (verbatim from
  `models/zscore.py`).

**Rewritten to our laws:** `detector.py` implements the Detector port. It filters
to its lane, groups events by asset, **pivots univariate events back into a
`(time × channel)` matrix** (ADR-0003 D1 — "detector adapters pivot back"),
builds features, scores, thresholds, and merges anomalous runs into episodes.

### D1 — model: robust z-score only; the dependency surface is numpy
We ship **one** model, robust z-score, which needs **numpy only**. IsolationForest
(sklearn) and the autoencoders (torch ≥ 2.3) are deferred to their own future
ADRs that will each deliberately accept that heavier dependency. This settles the
surface CLAUDE.md was waiting on — **numpy, no torch, no sklearn** — which is what
unblocks image hardening (a separate task). Rationale mirrors ADR-0011's "prove
the lifecycle on the simplest case first": z-score exercises the entire hard seam
(pivot, windowing, feature engineering, artifact load, thresholding, episode
merging, evidence) at the lowest possible dependency and determinism risk.

We also **drop team3's `StandardScaler`** from the classic path: robust z-score
already centers and scales by median/MAD, so the scaler was redundant. Dropping it
is what keeps the artifact a **pure-JSON** object (no pickle) — see D2.

### D2 — artifacts: trained offline, shipped as pure JSON by id
Detection is inference-only; the model is trained once, offline, and its
parameters are committed. Because the model is median/MAD + a threshold, the
artifact is **pure JSON** (`src/iotshield/detect/team3/artifacts/skab_zscore.json`):
window/lag/fft config, the raw `channels` order, the engineered `feature_columns`
order, `median`, `mad`, `threshold`, `percentile`. This is human-readable,
git-diffable, and free of pickle version-fragility and pickle-deserialization
concerns — the same ethos as our JSON-serializable contracts. The config selects
an artifact **by id** (`params: {artifact: skab_zscore}`), resolved through
`importlib.resources`, so runtime is CWD-independent (closing team3's relative-path
landmine) and never touches `../reference/`.

### D3 — unsupervised threshold; train on the asset's own normal baseline
The threshold is the 99th percentile of the model's scores on **normal** training
data, frozen into the artifact. We **reject** team3's `search:` block (it selects
percentiles from test-set ground truth) and its streaming warmup calibration
(poisonable, irrelevant to a batch port). Two findings from evaluating against the
SKAB labels shaped the training recipe:

- **Train on the same asset's normal history, not a foreign clean recording.**
  Robust-z-max keys on loud transients; SKAB's dedicated `anomaly-free.csv` is a
  *different* recording whose transients set the threshold out of reach of a valve
  run's (quieter, sustained) fault — zero recall. Training on the leading normal
  prefix (labelled `anomaly == 0`) of the recording under watch — the realistic
  deployment pattern — recovers ~0.9 recall at ~0.03 false-alarm on abrupt faults.
- **Drop degenerate features.** A quantized/near-constant channel derivative has a
  MAD at the 1e-8 floor; robust-z then explodes on quantization noise and
  dominates the per-row max. Features whose training MAD is at/near the floor
  carry no robust signal and are dropped at train time. (FFT-energy features are
  disabled in the shipped artifact — heavy-tailed and unhelpful here — though the
  builder keeps the capability behind a flag.)

Known model limit (documented, not hidden): robust-z-max is a weak baseline that
flags *abrupt* excursions, not slow set-point drift — team3's autoencoders are its
strong detectors, deferred with their torch dependency.

### D4 — feature columns are sacred (a boundary contract, like timestamps)
team3's online path never checked that the runtime feature vector matched the
trained one positionally — a silent-corruption landmine. Our adapter validates at
the boundary: an asset whose channels do not cover the artifact's `channels` is
**skipped entirely** (emits nothing), never scored against a misaligned matrix.
Engineered features are reindexed to the trained `feature_columns` and asserted.

### D5 — one detection per episode (merge adjacent anomalies)
Stride-1 scoring flags every timestamp of a sustained anomaly; emitting one
detection per timestamp would (a) misrepresent one physical event as many point
anomalies, discarding the run-length that *is* `EventKind`, and (b) fool the alert
engine, whose corroboration/volume logic assumes one `DetectionResult` = one
independent flag. So we merge consecutive anomalous rows into **episodes**: one
`DetectionResult` spanning `window_start..window_end`, `score` = the episode's peak,
`event_kind` from run length (point/contextual/collective, thresholds borrowed
from `evaluation/postprocess.py:classify_event_type`), `evidence` = the peak
timestamp and top-contributing channels. A `min_segment_length` knob (default 1,
emit all — no hidden gate, ADR-0003 D3) is surfaced rather than baked in.

### D6 — no contract change
`DetectionResult` absorbs this detector unchanged: telemetry lane, `method =
"team3_zscore"`, raw `score`/`threshold`, the episode window, `event_kind` (finally
exercised), and `evidence`. That a completely different detector class fits the
port with **zero** contract change is the architecture validating itself; had it
not fit, golden rule 2 says stop and escalate, not patch.

## Consequences
- We now detect on telemetry with a real (if simple) statistical model, not just
  the `stub_threshold` reference. A third demo run exercises it end-to-end:
  `--config config/demo_team3.yaml --input data/demo/demo_team3.csv` (a small
  SKAB `valve1/13` slice — normal into a sustained valve fault — converted to our
  long CSV; its naive timestamps are attributed UTC, the same D2 assumption as
  ADR-0011's syslog). The run yields one collective detection and one alert.
- The detector joins the port suite as the `team3_zscore` case; the swap-safety
  net now guards two detectors.
- The runtime dependency surface is settled as numpy-only; sklearn/torch models
  are future per-model ADRs; image hardening is unblocked.
- Known limitations (scoped, deferred): one artifact / one channel-set (no
  per-asset-type routing); the adapter treats each asset's timestamp-ordered
  events as a uniformly-sampled series and does not resample; batch port only (no
  streaming); team3's `interpret_events`/`explanations` are not reused in the
  alert layer yet (that is team2's future job).
