# ADR 0004: Ports, batch semantics, and the MalformedRecord contract

Date: 2026-07-08 · Status: accepted (discussed and approved by Andreas)

## Context
Modules must be swappable without touching their neighbors. Ports (typing.Protocol)
fix the vocabulary between the pipeline runner and each stage; adapters implement
them. Ports never call each other — the runner mediates every hop, which is what
keeps stages independently swappable and testable.

## Decision
Three ports, all batch-semantics (sequences in, lists out; a streaming variant
would be an *additional* port later, not a change):

- **Normalizer** — `normalize(text, *, source_format, origin) -> NormalizeResult`.
  Two outputs, always: events + malformed records. `NormalizeResult` is a
  port-local return shape, NOT a contract (never persisted, lives one hop).
  Unsupported `source_format` raises (config error ≠ data error).
- **Detector** — declares `lane` and `method`; `detect(events) -> [DetectionResult]`.
  The pipeline routes by lane; implementations defensively ignore foreign-lane
  events. Zero detections is a successful judgment.
- **AlertEngine** — `build_alerts(detections, assets) -> [PrioritizedAlert]`.
  Owns correlation AND scoring: (asset, time-window) bucketing where corroboration
  boosts priority but never gates detection; every detection is covered by exactly
  one alert (D3). Rejected: a separate Correlator port — it would force a contract
  for an intermediate "bucket" object nothing else consumes.

**MalformedRecord becomes the fifth contract.** Normalization is the trust
boundary; only there is failure an expected, informative outcome. Unparseable
input is disproportionately suspicious in security, so failure is modeled as
persisted data (queryable, testable, dashboard-visible), never as logs or drops.

**Port test suites are mandatory.** Each port has a parametrized suite in
tests/ports/; every adapter (stub or real) joins it by adding a case. The suites
pin behavior (conservation of input, lane discipline, corroboration monotonicity),
complementing pyright's structural check of the signature.

Stub adapters (StubCsvNormalizer, StubThresholdDetector, StubAlertEngine) are
permanent: reference implementations for the suites and the pre-committed
fallbacks for the integration timeboxes.

## Consequences
- Integrating a student module = writing an adapter + adding a suite case. If an
  adapter needs a port change, that is a design smell to escalate, not patch.
- The runner (next step) composes ports; it can be tested with stubs alone.
- Storage gains a malformed-records table when the storage layer lands.
