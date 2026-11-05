# ADR-0048: The rest of meterdetect, and what real labels said about it

- **Status:** accepted
- **Date:** 2026-11-04
- **Related:** ADR-0030 (the first vendoring), ADR-0037 (a shelf is named, not pathed), ADR-0038
  (the synthetic meter corpus), ADR-0022 (hyperparameters vs fitted state), ADR-0021 D7 (a number
  we typed in), ADR-0028 (truth), ADR-0043 (`meter_quality`)
- **Scope:** no contract changes.
  - detectors: `meter_baseline`, `meter_lof`; alert engine: `fused`
  - `convert/` (rates as counters, WBN workbooks); `eval fetch lead`; `--fit-protocol clean-prefix`
  - the meter model shelf, `IOTSHIELD_METER_MODELS_PATH`, and `train --name/--preset/--until`
  - `evidence["signature"]` on per-meter detections; `PerMeterDetector`/`PerMeterParams` refactor

## Context

ADR-0030 vendored the Isolation Forest path of Panayiotis Cyprus's `meterdetect` at commit
`d3c61a6`. The project moved on: a rule baseline, LOF, event fusion, the Water Board of Nicosia
(WBN) SCADA source, and LEAD, a public dataset with human-labelled anomalies.

The copy this ADR read is an August 2026 working directory **with no git history**, so nothing
here can cite a commit. Ask the author for one before the next re-sync.

Two gaps mattered more than any one module. Every meter number we had came from our own generator,
which ADR-0030 had warned measures whether a port preserved behaviour rather than whether a
detector is good. And a run never fits while `demo_meter` was the only shipped fit, so **a real
meter got no anomaly judgment at all** beyond `meter_quality`'s written rules.

## Decision

### D1 — A rate becomes a counter in a converter, not a second path through the normalizer

WBN and LEAD report per-hour amounts; every meter dialect reads a running total. `convert/_rates`
writes the total in the existing `sensipro-hourly-csv` format, so differencing, gap and duplicate
handling stay in one place and no renderer or round-trip law changes.

Upstream's `cumsum` loses the reading after any gap. An opener row one hour earlier, stamped inside
the gap, recovers it, and the gap stays an absence. **A gap of exactly one hour cannot be
expressed**: the opener would abut the previous reading and the normalizer would emit a zero for an
hour nobody observed, which a stuck check would act on. Our first version did exactly that and its
tests caught it. So `representable` drops the reading after a one-hour gap and the caller counts it:
0.145% of LEAD's readings, 211 of WBN's 818,361. Rejected: a rate-reading dialect, whose derived
counter could not round-trip a generated event's.

### D2 — WBN arrives through a converter

`python -m iotshield.convert.wbn` turns the daily workbooks into CSV, behind the optional `wbn`
extra (`xlrd`, `python-calamine`) because only this step needs them and it runs ahead of the
pipeline. A negative flow, a dead channel, a corrupt file and an unplaceable daylight-saving stamp
are reported rather than dropped silently. Pressure, level and chlorine are not converted:
`MeterReading` has no place for them and no detector would read them.

### D3 — Fits made at deployment live on a named shelf

`train --name <fleet>` writes to `IOTSHIELD_METER_MODELS_PATH` (default `config/meter-models`), one
subdirectory per detector. It is ADR-0037's shape for ADR-0037's reason: a spec is the same object
from YAML or `POST /runs`, so an id is resolved by **membership** of a listing, never joined into a
path. A packaged id wins a collision and `train` refuses to create one. Files are cached by
modification time, so a re-fit needs no restart. They hold operator readings (ADR-0030 D6), so they
are gitignored.

`--preset` carries upstream's calibrated settings for the sources whose winning configuration our
four features can express. SensiPro's and EOA's are absent: they need a 12-feature set or an MSTL
residual we do not carry, and SensiPro's did not survive their own cross-validation (pAUC 0.76,
cross-validated 0.35 ± 0.43). `--until` keeps a fit out of what it will judge. Nothing checks that
a history is clean.

Not built: fitting directly from a data-source pull (export a CSV first), and scheduled re-fits.

### D4 — `meter_baseline`: checks that need no fit

Stuck, z-score and profile, ported from `baseline.py`, because the forest is blind to a stuck meter
(upstream's synthetic recall 0.02 against 0.88) and because it is the one anomaly judgment a meter
gets with no artifact. Consecutive hours merge into one detection, and the profile and floor come
from the batch judged, so under two weeks of history is a named skip.

Two things did not survive contact with data:

- **The leak rule is off by default.** Flow above 10 litres through both night hours is a household
  rule, and produced 30,782 of WBN's 31,521 baseline events. Upstream also read those hours in UTC;
  `leak_timezone` reads them locally.
- **The noise floor assumes a meter that idles.** The 95th percentile of non-zero readings sits near
  peak demand on a building with a base load, and the largest z on a LEAD sample was 1.73 against
  3.5. Every percentile has this problem, so `floor_percentile: 0` removes the floor, and both
  checks then share the rolling MAD as their scale. The default stays upstream's 0.95, which suits
  meters that idle.

### D5 — `meter_lof`: an option, with its threshold corrected

Same features and per-meter shape, sharing `PerMeterDetector`. `novelty=True` replaces upstream's
`fit_predict` on the batch judged. The threshold comes from `negative_outlier_factor_`, because in
novelty mode a training row counts itself as a neighbour and scores low, so a threshold from
`score_samples(training)` would overspend the budget. A test pins the difference. A demo artifact
is committed so it is ready to run like the forest.

### D6 — `fused`: registered, measured, not the default

Noisy-OR over `W[detector][signature]`. Departures, each forced by the port: a low score is a
low-priority alert rather than a dropped one, and each detector contributes the best weight among
its own claims, since upstream's single incident type can lower the score when a detection joins.
The weights are synthetic recall on 4-12 meters per source, used as though they were precision, and
device criticality is not a factor.

### D7 — `--fit-protocol clean-prefix`

`prefix` stops at an asset's first incident, and LEAD's median building has one 453 hours in, under
the 883 a default fit needs: **46 of 200 buildings could be fitted**, and the rest read as a bad
detector. `clean-prefix` reserves the same head uncapped and withholds its labelled hours, fitting
198. It is optimistic by construction, since a deployment has no labels to withhold faults with.

## Measurements

### LEAD: 200 buildings, 3,625 labelled incidents, `clean-prefix`

| detector | evF1 | evP | evR | detections | per meter-month |
|---|---|---|---|---|---|
| `team3_zscore` | **0.17** | 0.10 | 0.53 | 14,068 | 7.3 |
| `meter_lof` | 0.15 | 0.09 | 0.57 | 13,287 | 6.9 |
| `meter_baseline`, `floor_percentile: 0` | 0.13 | 0.08 | 0.40 | 17,994 | 9.4 |
| `meter_iforest` | 0.11 | 0.07 | 0.37 | 14,556 | 7.6 |
| `meter_baseline`, default | 0.00 | 1.00 | 0.00 | 4 | 0.0 |

Same detections from all three meter detectors, two engines, mean across buildings:

| engine | P@1 | P@5 | P@10 | mean rank of a true alert |
|---|---|---|---|---|
| `weighted` | **0.77** | **0.55** | **0.39** | **39.6** |
| `fused` | 0.35 | 0.25 | 0.21 | 58.6 |

### `meter_fleet`: 4 recordings, 3 incidents, `prefix`

| detector | evF1 | evP | evR | detections |
|---|---|---|---|---|
| `meter_baseline` | **0.80** | 1.00 | 0.67 | 3 |
| `meter_iforest` | 0.23 | 0.14 | 0.67 | 79 |
| `meter_lof` | 0.17 | 0.09 | 1.00 | 88 |
| `team3_zscore` | 0.16 | 0.09 | 1.00 | 319 |

Three incidents, from a generator whose fault mix descends from the same upstream author's. Read it
as the baseline being quiet and right here, not as the baseline winning: LEAD says otherwise.

### WBN: 101 meters, 818,150 readings, one real run

Fitted on January to March (52 s, 28 MB artifact), then a full run: 2 min 27 s and 3.3 GB peak on
SQLite. Against upstream's own emitted events, the port reproduces **150 stuck detections (150
upstream) and 4,792 heartbeats (4,792 transmission gaps)**, and leak noise goes from 30,782 to 0.
There are no labels, so this shows the path works, not that the alerts are right.

## What this says

1. **The default budget does not hold on real data.** A fit budgeted at about one alarm per
   meter-month produced 6.9 to 7.6 on LEAD, and 8,933 forest detections on WBN over a year. Both fit
   on the first months and judge the rest, so the likeliest cause is seasonal drift, though this
   was not isolated. Upstream sidesteps it by recomputing its threshold per run, which is the
   behaviour ADR-0030 D5 removed because it cannot report "nothing happened". This is the largest
   open problem for metering. The likely fix is periodic re-fitting, so the shelf is a prerequisite.
2. **Upstream's ranking did not reproduce here.** It found Isolation Forest best on LEAD by ROC-AUC
   (0.78), and LOF weakest. At a fixed operating point, fitted first and scored on unseen months, the
   order differs and the general z-score leads. These are different questions, and neither result
   refutes the other, but it is why a threshold-free number is not a deployment recommendation.
3. **Fusion hurts.** `fused` puts the true alert on top less than half as often as `weighted`. Its
   priorities also barely separate true alerts from false (88 against 81, where `weighted` gives 25
   against 10). `weighted` stays the default, and the same evidence gives no reason to adopt `fused`.
4. **A purpose-built detector has not earned its place on electricity.** On LEAD every meter-specific
   detector is at or below the general one. Metering's specific value so far is the stuck check and
   the data-quality rules, which no statistical detector replaces.

## Consequences

- Detectors registered: nine. `PerMeterParams` reorders `MeterParams`' dump, so regenerating
  `demo_meter.json` produces a key-order diff with identical values. It was left as committed.
- The Compose wizard gains a **Stuck water meter** scenario (`scripts/make_sample_data.py`): the demo
  meter's newest four weeks with a 48-hour freeze, under all three meter detectors. It pins 8
  detections from `meter_baseline` and `meter_lof`; `meter_iforest` stays silent on this input.
- A compose deployment mounts `config/meter-models` read-only into the Pipeline API; the runner
  writes through its existing `./config` mount.
- `eval fetch lead` takes `--source <dir holding train.csv>`. LEAD's terms were not reviewed, so it
  is treated like SKAB: converted locally, gitignored, not redistributed.
- Open: which id preselects is still a filename accident (ADR-0036), and now one a deployment can
  change by adding a fit. Per-source reliability weights and `combiners.py` were not ported, since
  the pooled matrix already failed its test. Neither was the watermark, which only matters to a
  scheduled incremental run, which this project does not have.
