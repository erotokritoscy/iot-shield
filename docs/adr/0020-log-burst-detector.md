# 20. LOG-lane burst detector

Date: 2026-07-17

## Status

Accepted.

## Context

The LOG lane has had a normalizer since ADR-0011 and no detector since. Log events are
ingested, stored, and shown — and judged by nothing. Two consequences:

1. Half our ingest is decorative. The syslog demo exists to showcase normalization and
   explicitly produces zero detections.
2. **Cross-lane correlation has never happened.** Both real detectors (`stub_threshold`,
   `team3_zscore`) are TELEMETRY. The weighted engine's `corroboration` factor is
   `1 + 0.5·(len(lanes) − 1)`, so with only one lane ever detecting, it has been pinned at
   exactly 1.0 for the life of the product. Our headline claim — anomalies correlated
   across modalities on one asset — was structurally unreachable, not merely undemonstrated.

Multi-source ingestion (ADR-0019) removed the last blocker by letting one run carry a
telemetry CSV and a syslog stream for the same asset. What was missing was something to
judge the log lane.

We measured the student modules for a detector to port and found none:

- **team1** `/analytics/anomalies` is a hardcoded scan: `severity in {error, critical}` or
  `abs(value) > 80`, capped at 200 rows. `80` is a literal. No state, no baseline.
- **team4** stops at parsing plus static per-metric severity constants
  (`temperature {WARNING: 80, CRITICAL: 90}`). Its own README scopes detection out.

Neither keeps the per-asset, per-window state a log detector needs, so there was nothing
to wrap and nothing to rewrite. This is the first detector we design ourselves.

## Decision

### D1 — Deterministic counting, not ML

A log event carries no numeric channel: its signal is `(timestamp, asset, severity,
message)`. Unlike telemetry — a continuous stream where "6σ from baseline" is meaningful —
a log is a stream of discrete semantic events, so "anomalous" can only mean a known-bad
*content* match, *too many* of something, or something *never seen*. All three are matching
and counting; none needs a learned representation.

We pick the volumetric family, for reasons that compound:

- **It catches the highest-value log threats directly.** Brute force, credential stuffing,
  scanning, enumeration and error storms are all literally "N of X within W".
- **Explainability is contractual.** Every alert carries a `rationale` an analyst acts on.
  "12 failed-auth events on gateway-01 in 60s (boundary 4)" survives contact with a human;
  a reconstruction error does not. In security, an explainable false positive is triageable
  and a black-box one corrodes trust.
- **No cold start.** It is correct on run #1 — no labels, no training corpus, no artifact.
  Supervised classification is impossible (we have no labeled log-attack data, and none to
  port); sequence models (DeepLog-style) need a large clean per-source corpus, cost a torch
  runtime, and break on log-format drift.
- **Determinism is a port law.** "Same input, same output" is trivially true here, and the
  detector needs no artifact — sidestepping the pickle/checkpoint version-fragility that
  our team3 JSON artifact exists to avoid.

Rejected: **rare-template/novelty** detection. It is also Tier-0, but "rare ≠ malicious"
gives it a structurally worse false-positive rate, it needs a learned baseline of normal
templates (cold start), and its headline threats are volumetric anyway — which counting
catches more directly. It is a good *second* log detector, not the first.

### D2 — One parameterized mechanism with presets, not a family of detectors

`LogBurstDetector` is a single rule — count what a **preset** predicate matches, per asset,
in a sliding window — and the preset is a param. Two ship:

- `severity_spike` (severity ≥ error): the default. Format-agnostic, needs no patterns, so
  it says something useful about any log source a normalizer can parse.
- `auth_failure` (curated authentication-failure phrasings): the credential-attack story.

This is the same altitude discipline as `stub_threshold` being one detector parameterized
by threshold and lane. A new category is a `PRESETS` entry, not a new adapter. The preset
rides in `method` (`log_burst:auth_failure`) so two configured bursts stay distinguishable
in a detection and in the alert rationale.

Note the presets are genuinely complementary, not redundant: sshd logs "Failed password" at
**INFO**, so `severity_spike` is blind to exactly the burst `auth_failure` exists to catch.

### D3 — Curated presets only; no operator-supplied regex

Content matching is inherently source-format-specific, so the patterns are a curated library
we ship and maintain. An operator selects a preset by name and never writes a regex in YAML.
Accepting arbitrary regex from config would let a typo become a pathologically slow or
wildly over-matching pattern, with no test coverage and no review. User-supplied patterns
can be added later behind a deliberate decision.

### D4 — Sliding window, and overlapping windows merge into one episode

A fixed tumbling bucket would split a burst that straddles a boundary. We use a sliding
window (≥ `min_count` matches within `window_minutes`), the classic brute-force rule shape.

Two pointers walk the time-ordered matches; qualifying windows that *overlap* describe one
incident and merge, while a window whose left edge has passed the previous episode's end
means a real quiet gap and opens a new one. **One incident produces one detection.**

*Corrected 2026-07-19.* This decision originally justified itself as "a 90-attempt brute
force is one alert, not 90". That claim is false, and measuring it produced a better reason.
Unmerged, a 90-attempt burst emits 86 detections — but they all land in one 5-minute
correlation bucket, so the alert engine already collapses them to **one alert either way**.
Bucketing absorbs fragmentation *within* a bucket; it is only fragments spread across bucket
boundaries that multiply alerts (91 fragments over 20 minutes → 4 alerts).

The real justification is what the merge does to the alert's *score*. The burst's intensity
already rides in `score` as the peak count in any window, which the engine reads as its
severity factor. The engine's `volume` factor separately counts *detections* in the bucket —
a corroboration signal that is only meaningful if each detection is a distinct judgment. An
unmerged detector therefore reports the same intensity twice, through two channels that mean
different things: measured, the identical burst scores 63.0/HIGH merged and 75.1/CRITICAL
unmerged. Merging is what makes the detection **one judgment**, which is the precondition
`volume` depends on.

That precondition is a general property of the Detector port, not a quirk of this adapter
(team3's detector merges consecutive anomalous rows for the same reason), so it is written
down in `detect/port.py` and the shared span-merging helper lives in `detect/_runs.py`.
ADR-0021's evaluation harness measures compliance (detections-per-incident) rather than
enforcing it.

### D5 — Fixed threshold now; learned per-asset baselines deferred

`min_count` is operator-configured. A learned per-asset baseline ("this host normally sees
≤1 failure/hour") needs cross-run state and belongs with the deferred persistence/enrichment
work, not here.

### D6 — The category rides in evidence, not in `threat_class`

`evidence["category"]` carries the preset name (`auth_failure`). Promoting it to the alert's
`threat_class` — still defaulting to `"unknown"` — is an alert-layer change and a separate
follow-on. No contract change is needed for this detector.

## Consequences

- The LOG lane is judged. `demo_log.yaml` detects an sshd brute force (12 failures in ~60s,
  ending in a successful login) as one collective detection → one HIGH alert.
- **Corroboration fires for the first time.** `demo_multimodal.yaml` now runs a log detector
  alongside the telemetry one on pump-02: the same pressure spike that scored ~27 (LOW) on
  telemetry alone scores ~56 (HIGH) once the log burst corroborates it, with
  `corroboration = 1.5`. The compatibility guard's uncovered-log-lane warning is gone.
- No new runtime dependency: pure Python, no numpy, no artifact. The numpy-only surface
  (ADR-0012) is untouched.
- The second detector in the LOG-vs-telemetry pair made the Detector port's *routing* laws
  worth sharing: foreign-lane isolation and deterministic per-asset ordering now live once
  in `detect/_group.py` (`group_by_asset`), used by both this detector and team3's. It is
  the detect-side analog of the existing `normalize/_linewise.py` and `alert/_correlate.py`
  — a new adapter inherits the port's routing laws instead of re-deriving them, and only
  supplies the predicate for which of its lane's events are judgeable.
- No contract change: a burst is a `DetectionResult` like any other. Because the port's law
  is a **strict** `score > threshold`, the reported `threshold` is the *exceedance boundary*
  — the largest count still normal, i.e. `min_count - 1`. "12 vs 4" reads correctly to an
  analyst and gives the engine a real margin.
- The curated pattern library is a maintenance surface: new log dialects may need new
  phrasings. `severity_spike` is the safety net that needs no patterns at all.
- Deferred: learned baselines (D5), `threat_class` promotion (D6), rare-template detection,
  and any sequence model.
