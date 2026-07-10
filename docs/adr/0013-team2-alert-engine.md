# ADR 0013: WeightedAlertEngine — team2's prioritization, by rewrite not wrapper

Date: 2026-07-09 · Status: accepted (approved by Andreas)

## Context
Second module integration (parallel with team3): give the AlertEngine port a real
prioritizer beyond the additive stub. We chose team2-alert-prioritization
("AlertIQ"). Reading its actual code (`backend/scoring.py`; its README is stale)
shows a SIEM-alert prioritizer: a multiplicative product of interpretable factors —
`severity x device_weight x category_weight x likelihood x anomaly_factor /
(1 + log1p(frequency))` — with a decomposed human-readable rationale, ranked by
batch-relative min-max normalization.

## Decision
**Rewrite team2's ideas against our contract; do not wrap it.** This applies the
standing wrap-vs-rewrite rule (ADR-0011), and *refines* it. team4 failed the wrap
test because it **destroyed information in its output**. team2 fails it *upstream of
its output*, on two counts:

- **Different input domain.** team2's formula consumes SIEM fields (`severity`,
  `category`, `risk_score`, `confidence`, `entropy`, `signature_id`). Our
  `DetectionResult` carries none of them — only `asset_id, lane, method, score,
  threshold, window`. Wrapping would mean *fabricating* team2's inputs, producing a
  meaningless score.
- **Missing the port's core behavior.** Our `AlertEngine` owns *correlation* —
  grouping detections into (asset, time-window) buckets so cross-lane
  corroboration boosts priority. team2 has no correlation (one record = one alert),
  and its one multi-record notion (`frequency`) is a *penalty* — the inverse of
  corroboration. Its batch-relative min-max ranking also violates the port's
  monotonicity law (a stronger alert elsewhere would rescale everyone down).

So the wrap test needs **all three**: the module does the job our port describes,
on inputs we can supply, with output that preserves what our contract needs. team4
failed the third; team2 fails the first two. Rewriting also sheds team2's pandas
and scipy dependencies (the math is pure `log1p`).

**Borrowed from team2** (cited in the module docstring): the multiplicative,
factor-decomposed score shape; the explainable per-factor rationale; absolute
thresholds → severity label; `anomaly_factor` as the corroboration hook; and
`device_weight` → our `Asset.criticality`.

**`WeightedAlertEngine`** (`alert/weighted.py`), a *separate* adapter beside the
stub (D3 — proof-of-concept; the stub stays the permanent fallback). It keeps our
correlation (shared with the stub via `_correlate`) and re-expresses team2's
formula over the three signals our detections carry: `severity` ← strongest
score/threshold margin, `corroboration` ← distinct lanes in the bucket, `volume` ←
detection count, `asset_weight` ← criticality.

- **D1 (forced by the port):** map the product to [0, 100] with an absolute,
  saturating transform `100 · (raw−1)/(raw−1+K)` — **not** team2's batch-relative
  min-max — and **drop the frequency penalty**. Both are required for
  corroboration-monotonicity: every factor is ≥ 1 and non-decreasing as detections
  join a bucket, so priority can only rise.
- **D2:** score the three signals we have, in team2's multiplicative/explainable
  style; `threat_class` stays `"unknown"` (we have no attack taxonomy from
  detections yet).
- **D4:** named by function (`weighted`), team2 credited in the docstring/here.

## Consequences
- A second AlertEngine is selectable by config: `config/demo_weighted.yaml` runs
  the same detections through richer, explainable scoring. It joins the port suite
  as the `weighted` case — the swap-safety net now guards two engines.
- **The prioritizer is bottlenecked by detector richness.** team2's formula is rich
  because SIEM alerts are rich; our detections are thin, so this v1 weights only
  margin/corroboration/criticality. As detectors (team3) populate
  `DetectionResult.evidence`/`metadata`, the *same* adapter can consume more — and
  richer weighting may later motivate a `DetectionResult` enrichment (its own ADR).
- Correlation + the margin measure were extracted to `alert/_correlate.py`, shared
  by both engines (mirrors `normalize/_linewise`).
