# 28. The meter detector, and scikit-learn in the runtime

Date: 2026-07-28

## Status

Accepted. Settles the runtime-dependency question ADR-0012 deferred ("numpy-only;
sklearn/torch deferred to future per-model ADRs") and ADR-0024 D1 restated. Adds a
normalizer with two dialects, a learning detector, and one lane vocabulary module. Amends
nothing; changes no contract.

## Context

Panayiotis Cyprus's unsupervised meter anomaly detector (*ML-Based Anomaly and Attack
Detection for Smart Meter Telemetry*, upstream commit `d3c61a6`) is the third teammate
module to be integrated, and the strongest: genuinely unsupervised ML over real utility
data, with model-agnostic feature attribution and an honest evaluation. The integration was
specified in `docs/integration/ml-detector-and-eoa-metering-plan.md`; he made the four
changes it asked for (installable package, timezone-aware UTC, raw score exposed, static
profile as the default residual) and they were verified end-to-end before this work began.

Two sources are in scope, because the promise being tested is *one detector, many sources*:
**EOA** (a Nicosia water platform, per-meter CSV history) and **SensiPro** (a second
organisation's PostgreSQL platform, carrying water *and* electricity).

Metering differs from every modality already in the pipeline in one way that drives most of
what follows: **a meter reports a cumulative counter, not a reading.** The meaningful
observation — hourly consumption — does not exist in the input; it is a difference between
two records.

## Decision

### D1 — scikit-learn joins the runtime

`scikit-learn>=1.4` becomes a runtime dependency. It is the first model library in the
image, and it arrives with scipy, joblib and threadpoolctl behind it.

*Why now, and why not sooner:* ADR-0012 froze the surface at numpy-only deliberately,
because team3's robust z-score genuinely needed nothing more and a dependency admitted "for
later" is a dependency admitted for nothing. This detector is Isolation Forest — an
ensemble of randomized trees — and reimplementing it would be a worse version of a
well-tested one, in a repo whose golden rule is to reuse the module a student already
validated.

*The cost, stated:* sklearn + scipy is roughly 100 MB unpacked, the largest single addition
to the image so far. That makes image hardening (already unblocked but not done) more
valuable, not less. `statsmodels` stays **out**: it is needed only for the MSTL residual,
which upstream now keeps behind `residual="mstl"` and whose benefit over the static
hour-of-week profile is still an open question there.

*Second user:* `weeks-3-4.md` §5.3 plans a SKAB telemetry Isolation Forest for the
z-score head-to-head. It is now unblocked and needs no further dependency ADR.

### D2 — one meter normalizer, one dialect per source

`normalize/meter.py` serves both sources, with `eoa-meter-csv` and `sensipro-hourly-csv`
as dialects — the shape `flow.py` established (ADR-0023 D4).

The alternative was one adapter per source, which is how upstream is organized and how the
integration letter described it. Rejected because the *shared* part is the interesting part:
differencing, gap safety, rollback handling, hourly gridding. Duplicating that per client is
the actual risk; the per-source part is a column mapping, a timezone and a utility. This is
also faithful to upstream's own architecture, where only loading is source-specific and
`cleaning.py` is shared.

The port suite parametrizes **per declared format**, so both dialects are independently
covered; adding a client is one `DIALECTS` entry plus one port case.

SensiPro is a database. Reading it directly is not a normalizer's job — the port takes text
and our ingestion is files and streams — so it arrives as an export of the one table we
read (`ascendinghourlyreading`). That was always the plan for it (letter §6); a database
*reader* would be a new port, not a new adapter.

### D3 — consumption is the event's value; the counter rides in metadata

A `NormalizedEvent` carries **hourly consumption** as its `value`, stamped at the
interval's **end**, with the raw cumulative counter, the interval width and the utility in
`metadata` behind the typed `MeterReading` (`src/iotshield/meter.py`).

Three options were considered:

| | value | invertible? | verdict |
|---|---|---|---|
| A | the raw counter | yes | rejected — a monotonically rising `value` is meaningless to any generic consumer; `stub_threshold` would fire on every event forever |
| B | consumption, counter in metadata | **yes** | **chosen** |
| C | consumption, counter discarded | **no** | rejected — kills the renderer, and with it any generated meter corpus |

C is what the letter literally described, and it is the one option that cannot work: a
renderer is the inverse of a normalizer, and consumption alone reconstructs the counter only
up to an unknown constant (and not at all across a reset). B costs one metadata key and
keeps the boundary exactly invertible, which is also what lets a later data-quality detector
see a rollback without a second ingest path.

The end-of-interval stamp is upstream's convention and what an hourly export implies. Note
it is the **opposite** of the FLOW lane, which stamps a flow at its start; neither is
inferable from a timestamp, so `interval_hours` travels with every event and the convention
is documented per lane rather than assumed globally.

### D4 — parsing in the normalizer, judgment in the detector

Upstream's `clean_meter` does six things. Three are invertible parsing and live in the
normalizer (hourly gridding, gap-safe differencing, rollback handling); three are modelling
judgments and move into the detector (the commissioning trim, the service-start trim, the
usability verdict).

The dividing line is the renderer. A normalizer that silently discarded a meter's first
three months could not be inverted, and the meta-test that composes renderer and normalizer
is what catches that class of error.

### D5 — the threshold is fitted state; the false-alarm budget is the hyperparameter

Upstream's `run_isolation_forest` fits and scores the same window and reports a per-batch
`contamination` quantile. We keep the model and the features and rewrite that plumbing
(`_iforest.py`): fit on known-clean history, then judge new data against an **absolute**
score threshold.

A per-batch quantile cannot report "nothing happened" — it flags 0.2% of any input,
including input with no anomalies (measured: exactly `0.002 x n` hours on an entirely clean
synthetic meter). Under ADR-0022 the split is clean: `fp_budget` is what a human knows (how
many alarms per meter-month are tolerable) and the threshold is what only the data knows.

This also resolves, rather than overrides, the upstream author's position. He declined to
expose a threshold input on the grounds that it is calibrated, not free — which is correct,
and is why there is no threshold parameter here either. His
`evaluation.threshold_for_fp_budget` computes exactly this number; the step we drop is his
converting it back into a contamination fraction.

Measured on a synthetic year, fitting on a clean 20% prefix and scoring the unseen
remainder: **4/5 injected events caught at 0.71 false alarms per meter-month**, versus 2/5
for the per-batch path.

One guard came out of that measurement and is enforced: a budget the history cannot support
is **refused**. If `fp_budget x n` is below one row, the quantile lies past the largest
observed score and the threshold is an interpolation artifact — a plausible number derived
from nothing. Calibrating ~1 alarm/meter-month needs on the order of 715 clean hours;
observed drift across 827 → 5,207 rows was ~19% and non-monotone, so `fp_budget` is a knob
to sweep, not a guarantee.

### D6 — the artifact stores the clean history, not the fitted weights

`artifacts/<id>.json` is `{schema_version, params, meters: [...]}`, where each meter carries
its hour-of-week profile and **the feature matrix it was fitted on**. Loading re-fits, and
re-derives the threshold from those rows and `fp_budget`.

team3's artifact stores weights as JSON because a median/MAD baseline is a handful of
readable numbers. A 200-tree forest is not: as JSON it is megabytes of unreviewable arrays,
and as a pickle it is a blob valid for one sklearn version. Storing the training data
instead is *smaller* than the model it produces, is exactly what a reviewer wants to see,
and makes model and threshold impossible to get out of step — a future sklearn that builds
slightly different trees derives a matching threshold rather than a mismatched one.

The cost is real and bounds where this format is appropriate: an artifact carries data, so
committing one is only acceptable for data we may commit. The demo meter is synthetic.

### D7 — one model per meter, and no shippable default

Fitted state is a map from `asset_id` to that meter's model; an asset with no model is
**skipped, never scored**. Upstream fits per meter because meters range from 25% to 94%
idle and a pooled model is dominated by the idle majority.

The consequence differs from every other detector here and is stated rather than hidden:
**there is no universal meter model.** A fleet of 1,656 SensiPro meters needs 1,656 fits,
produced at deployment from each meter's own clean history. The committed artifact exists so
the demo and the tests have something fitted to run; it is not a product default.

Note the contrast with team3, whose `fit` *refuses* more than one asset. Both enforce the
same rule — never interleave two assets' histories — from opposite sides.

### D8 — three upstream defects fixed in the vendored copy

Vendored code we own is code whose bugs we own — but only the code we actually vendor. Each
fix below is documented at its definition and pinned by a test, so a re-sync cannot quietly
reintroduce it; the defects in code we do not copy are reported upstream instead.

1. **A collapsed MAD produced z = 55,000.** `deviation` is exactly zero through every idle
   hour, so its median absolute deviation collapses — but to ~1e-14 of floating-point
   residue, not to 0. Upstream tests `mad > 0`, so the standard-deviation fallback never
   fires, and its floor (proportional to the *median*, i.e. 1e-6 for a zero-median feature)
   turned a 0.055 deviation into z = 55,000 on the demo meter. A MAD is now treated as
   collapsed when it is negligible relative to the column's own **spread**, and reported z
   is clamped at 999 — past which the number is not information.
2. **The holiday calendar expires.** The moving Orthodox holidays are tabulated for
   2024–2026 only, so from 2027 every one of them silently vanishes (measured: 14 holiday
   days in 2026, 10 in 2027, no error). **Reported upstream, not fixed here** — and the
   calendar is not vendored at all. Only the lean-four path is copied, and no model column
   uses a holiday, so carrying the table would mean maintaining a dated Cyprus calendar for
   a feature that never reaches the forest. (It was briefly vendored *and* fixed, by
   deriving the dates from Orthodox Easter; the fix was correct and the code was still dead,
   so it went out with the rest of the calendar. If a holiday feature is ever wanted, it
   should land with the feature.)
3. **`inject_events` mislabels a multi-block spike.** With `count > 1` the blocks are
   recorded as one event spanning first→last, so `labels_from_events` marks everything
   between them: 3 changed hours, 2,732 labelled. Latent upstream because its own
   calibration always passes `count=1`. **Not fixed here** — that module is corpus
   machinery and belongs in `eval/`, which is out of scope for this change. Recorded so the
   fix lands with the port.

## Consequences

- The image grows by ~100 MB. Hardening matters more.
- `meter_iforest` is the first registered detector that is untrained by default, which
  exposed a real regression: the compose page preselected it (it sorts before
  `stub_threshold`) and would have opened on a chain a run refuses. `ModuleInfo` now
  publishes `artifact_param`, so the page prefers a detector that is *ready to run* — a
  fact previously visible only on the Evaluate side.
- Every TELEMETRY detector is now offered for a telemetry corpus, including ones that will
  find nothing (`meter_iforest` needs meter metadata; `team3_zscore` needs SKAB's channels).
  Both report as silent with a reason, which is the ADR-0026 amendment working as intended.
- **No meter corpus yet, so the detector is not benchmarked.** It is demonstrated, not
  measured. Closing that is the next step and it is deliberately separate: upstream's
  `synthetic_normal.py` + `injection.py` become a `Behaviour` and a `Renderer` under
  `eval/generate/`, giving a committed, licence-clean labelled meter corpus. Two findings
  are already waiting for it: defect 3 above must be fixed as part of the port, and the
  answer key must treat the **168-hour rolling shadow** after an event as out of scope
  rather than asserted-clean — measured, the difference is 8.70 versus 0.71 alarms per
  meter-month at one operating point, a 12x distortion of the false-alarm rate.
- LEAD (1,413 labelled hourly meters) is a later, *independent* check. A corpus generated by
  upstream's injector and scored by upstream's detector measures whether the port preserved
  behaviour, not whether the detector is good — his features were tuned on that injection
  mix. The distinction is worth keeping explicit.
- Demo inputs are **synthetic**, generated in the real wire formats and calibrated to the
  real per-meter statistics upstream publishes (mean ~0.008 m³/h, max ~0.135, ~50% idle).
  No real meter data is committed, and the device ids are deliberately not real meters. Real
  exports would confirm the column mappings against reality; both schemas are documented
  upstream (`data/raw/EXPORT_LOG.MD`, `docs/sensipro_data.md`), which is what made building
  without them possible.
- Re-syncing upstream is a diff against the `_`-prefixed modules, whose docstrings name the
  upstream file and every adaptation. They also name the upstream **commit**, which team3's
  do not need (its reference tree is frozen) and this one does.
