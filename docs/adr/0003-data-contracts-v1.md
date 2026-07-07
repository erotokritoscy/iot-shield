# ADR 0003: Data contracts v1

Date: 2026-07-07 · Status: accepted (discussed and approved by Andreas)

## Context
The product unifies heterogeneous modalities (telemetry, logs, flows) not by merging data
but by merging judgments: shared *addressing* (who/when) at the event level, shared *judgment
format* at the detection level, shared *decision* at the alert level. The contracts are the
only components with fan-out to everything, so they freeze first (schema v1 per the plan).

## Decision
Four pydantic v2 contracts in `src/iotshield/contracts/`: `Asset`, `NormalizedEvent`,
`DetectionResult`, `PrioritizedAlert` (fields documented in the models themselves).
Cross-cutting rules, enforced by validators and tests:
- Timestamps are timezone-aware, normalized to UTC; naive datetimes are rejected.
- Unknown fields are rejected (`extra="forbid"`).
- Enum values are wire format; renames are breaking changes (tests freeze them).
- Detections/alerts describe intervals; point anomalies are zero-length windows.

Specific decisions:
- **D1 — one value per event.** Multivariate readings fan out into one event per metric
  (uniform storage/query; matches both normalizer modules). Detector adapters pivot back.
- **D2 — score persistence is pipeline policy, not contract.** Detections carry raw
  `score` + `threshold`. Whether sub-threshold scores are persisted stays open; lean:
  a skinny side table (asset, ts, method, score, threshold) when the dashboard needs
  score history — additive, no contract change. `DetectionResult` remains a *positive* judgment.
- **D3 — every detection bucket becomes an alert; ranking filters.** Rejected alternative:
  a pre-alert gate (second cutoff discarding low-value buckets) — it adds a hidden tunable
  whose miscalibration silently hides incidents; unnecessary at MVP scale.
- **D4 — no `source_event_ids` on detections.** Contributing events are recoverable by
  query on (asset_id, window, lane); `evidence` may carry sample references. Alerts keep
  explicit `detection_ids` because that short list *is* the corroboration lineage.

## Consequences
- Any contract change from here on requires a new ADR (golden rule 1).
- Storage tables and API schemas mirror these models; ports speak only these objects.
- The prioritizer derives cross-method comparability from the score/threshold margin —
  raw scores are never compared across methods directly.
