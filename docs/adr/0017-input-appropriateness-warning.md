# ADR 0017: Warn when a run's input is mostly malformed

Date: 2026-07-13 · Status: accepted (approved by Andreas)

## Context
The compatibility guard (ADR-0014) validates a pipeline's *structure* — that the
normalizer's output feeds the detectors, that the declared `source_format` is one the
normalizer accepts — entirely without looking at the data. That is deliberate: it is
what keeps the guard cheap and deterministic (capabilities resolve from the registry
without constructing adapters or reading input).

But there is a second, orthogonal axis of correctness the guard cannot see: **content
appropriateness** — does the *actual input* suit the chosen normalizer? Today the
answer only emerges line-by-line at runtime, as input that doesn't parse becomes a
`MalformedRecord` ("capture, never drop" — ADR-0011). That is exactly right for a
healthy feed with a few corrupt lines. It is *wrong* as the only signal for a total
misconfiguration: point the syslog normalizer at a CSV and every line becomes
malformed, yet the run reports "success" with a large malformed pile buried in the
Data-health view. Partial corruption and a wrong-normalizer misconfig look identical.

## Decision
After normalize, the runner computes the malformed fraction and, when it exceeds a
threshold (`_MALFORMED_WARN_RATE`, default `0.5`), appends a **WARNING** to the run
report: *"the 'syslog' normalizer rejected 87% of input (174/200 records); the input
may not match the chosen normalizer."*

- **The normalizer is the single authority.** The signal is the normalizer's *own*
  reject count — not a separate format-sniffer — so there is no second, rival source
  of truth about "is this syslog" that could disagree with the real parser.
- **WARNING, not ERROR.** It rides the same warnings rail as the guard's WARNINGs
  (`RunReport.warnings` → the Pipeline API's `RunResponse.warnings` → the compose
  receipt), so there is no new plumbing. It stays non-blocking on purpose: "capture,
  never drop" still holds (the malformed records remain the audit trail), and a
  genuinely high reject rate can be legitimate. This matches the two-tier severity
  philosophy (ERROR = would crash; WARNING = suspicious-but-legal).
- **It lives in the runner, not the guard.** Appropriateness is data-dependent and
  runtime; the guard stays content-blind and static. The two kinds of correctness
  stay in separate layers.

## Consequences
- A wrong-normalizer run is now loud instead of silent — in the CLI report, the API
  response, and the dashboard receipt — at essentially zero extra plumbing.
- The threshold is a module constant, not yet operator-configurable. Existing demos
  are unaffected: the syslog demo's ~20% reject rate stays below it (asserted in
  `tests/pipeline/test_runner.py`).
- **Deferred, pending a stakeholder conversation** on how strict input handling should
  be: a **pre-flight sample-parse** (warn or refuse *before* writing a junk-only run
  to the database) and a **UI "detect normalizer"** convenience (dry-run each
  normalizer on a sample and rank by reject rate to suggest the best fit). Both are
  additive on top of this signal; neither is built here.
