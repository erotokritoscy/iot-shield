# 24. FLOW-lane behavioural detector

Date: 2026-07-20

## Status

Accepted. Normalizer side: ADR-0023.

## Context

ADR-0023 gives the FLOW lane a normalizer. Without a detector it would be decorative — the
exact position the LOG lane sat in between ADR-0011 and ADR-0020.

There is also a specific prize. The weighted engine's `corroboration` factor is
`1 + 0.5 x (lanes - 1)`. It was pinned at 1.0 for the life of the product until ADR-0020
produced a second judged lane (1.5). A third judged lane makes 2.0 reachable — the product's
headline claim, *anomalies correlated across modalities on one asset*, at full strength.

The reference set contains one flow module, so the first question is whether to port it.

## Decision

### D1 — Do not port team5's classifier; it cannot satisfy our contract

team5's CICIDS2017 notebook reports 99.8% accuracy on binary benign/attack over 2.83M
flows. It should not be our first flow detector, for reasons that compound:

- **It is architecturally incompatible.** Its first preprocessing step drops `Flow ID`,
  `Source IP`, `Source Port`, `Destination IP`, `Protocol` and `Timestamp` — and the Kaggle
  mirror it uses does not ship most of them. The model consumes 77 purely statistical
  features with **no identity and no time**. Our entire product correlates on
  `(asset, timestamp)`; a detector that cannot say *which device* or *when* cannot produce
  a `DetectionResult` at all. The information our contract requires is destroyed upstream
  of any seam an adapter could sit on — the same situation ADR-0011 met with team4, and the
  same resolution.
- **The 99.8% is not a security result.** Its top features (`Init_Win_bytes_forward`,
  `Average Packet Size`, `Packet Length Variance`) fingerprint the traffic *generator* used
  to synthesize CICIDS2017 on a 2017 enterprise LAN. Pointed at a customer's IoT fleet it
  measures domain shift — what ADR-0012 D3 identified and ADR-0021 then measured.
- **Nothing is persisted**: no model, no scaler, no feature order, no `outputs/`. Adopting
  it means downloading 2.83M rows from Kaggle and retraining, against a corpus-licensing
  constraint that already says datasets are fetched, never committed.
- **It would cost scikit-learn**, which ADR-0012 deliberately deferred to its own future
  ADR. That decision deserves to be made on its merits, not as a side effect.
- **It fragments**: one detection per flow row is the anti-pattern ADR-0020 D4 measured.
- A binary probability gives an analyst nothing, while `PrioritizedAlert.rationale` is
  contractually mandatory.

Not discarded — *sequenced*. It becomes the natural second flow detector once `FlowRecord`
gives it a typed input (ADR-0023 D5), sklearn earns its ADR, and a labelled flow corpus
exists to prove it on.

### D2 — Behavioural aggregation over a host's recent network activity

The unit of anomaly in flow data is not a row (ADR-0023 Context, property 4). The signal
exists only in the aggregate over a host's recent behaviour, so that is what we compute.

This is the same conclusion ADR-0020 reached for logs, by the same route, and it earns the
same benefits: correct on run #1 (no labels, no corpus, no artifact), no new runtime
dependency, deterministic by construction, and explainable — *"cam-07 contacted 43 distinct
destinations in a minute (boundary 20)"* is something an analyst acts on.

The IoT setting makes it unusually strong: an IoT device has a tiny, rigid communication
profile (one broker, one NTP server, one update endpoint, on a fixed cadence), so deviation
is glaring. This is why flow-based detection works better on IoT fleets than on general
enterprise networks.

### D3 — One parameterized mechanism with presets

Fan-out, port scanning and exfiltration look like three detectors and are one: *per asset,
over a sliding window, does some aggregate exceed a threshold?* Only the aggregate varies.

Three presets ship:

| preset | aggregate | catches |
|---|---|---|
| `fan_out` (default) | distinct peer hosts | worm/botnet propagation, sweeps |
| `port_scan` | distinct peer ports | reconnaissance against few hosts |
| `exfiltration` | sum of `bytes_out` | bulk egress |

`fan_out` is the default for the same reason `severity_spike` is `log_burst`'s: it needs
only the peer address, which every dialect carries, and it is the highest-value flow signal
for an IoT fleet.

Two design points with real content:

- **Each preset carries its own window and threshold.** Scanning is a seconds-to-a-minute
  phenomenon; exfiltration is minutes-to-hours. One shared default fits neither, so the
  preset owns them and the config params default to "whatever this preset considers
  normal". This is what makes one adapter viable across phenomena with different timescales.
- **The scanning presets count only asset-initiated flows**; exfiltration is
  direction-agnostic. Fan-out and port scanning are claims about what a device is *reaching
  out to*, so counting inbound connections would make every popular server look like a
  scanner. Exfiltration cares only which way the bytes went — an attacker who dials in and
  pulls data out is still exfiltration, and the counters are already asset-relative
  (ADR-0023 D2).

### D4 — The sliding-window skeleton is extracted and shared

`log_burst` already implemented a two-pointer sliding window with an overlap-merge, and
ADR-0020 D4 established with measurements that the merge is load-bearing: unmerged, one
incident is restated as many detections, and the alert engine's `volume` factor reads the
repeats as independent corroboration, inflating priority for evidence gathered once.

The flow detector needs identical episode logic with a different aggregate, so
`detect/_windows.py` now owns the walk, the merge and the peak, parameterized by an
incremental `Aggregate` (`add`/`remove`/`value`). `log_burst`'s aggregate is simply "count",
and its tests passed unchanged through the refactor — which is the evidence the extraction
is behaviour-preserving.

This follows the precedent the codebase set twice: `_linewise.py` was extracted when the
second normalizer arrived, `_group.py` when the second detector arrived. The third member
of a family is where a skeleton is proven rather than speculative.

Incremental rather than recomputed is not a micro-optimisation: recomputing "distinct
peers" from scratch at every window position would make a long capture quadratic.

### D5 — The configured threshold *is* the exceedance boundary

`find_episodes` compares strictly (`>`), matching the port's law that an emitted detection
has `score > threshold`. So the number an operator configures is the number an analyst
reads on the detection: "43 distinct destinations vs boundary 20".

A deliberate, small divergence from `log_burst`, whose `min_count` is a "how many are
needed to trigger" count and therefore reports `min_count - 1`. Both end up reporting the
exceedance boundary; they just start from different natural phrasings.

### D6 — `event_kind` follows the episode's span

Unlike a log burst (which needs >= 2 events by construction, so is always collective), a
single very large transfer can trip `exfiltration` on its own. That is a genuine point
anomaly and the contract already has a shape for it, so a one-flow episode is `POINT` and a
multi-flow episode is `COLLECTIVE`.

### D7 — `Fittable` is not implemented, and the reason is not "fitting is risky"

Learning a per-asset baseline is *more* attractive here than it was for team3: a
thermostat's normal fan-out is 2 and a NAT gateway's is 2000, so a single fleet-wide
threshold is guaranteed wrong for most devices. It is still deferred, for three structural
reasons:

1. **There is no caller.** The port is explicit that the pipeline never calls `fit`; only an
   evaluation does, on a corpus's clean prefix or holdout. There is no labelled flow corpus,
   so `fit` would have no production caller by design and no benchmark caller in fact. The
   only thing exercising it would be the port suite's "a fitted detector is still a
   detector" test — which checks that fitting does not break the port, not that it learns
   anything correct.
2. **Contaminated baselines fail sharply here, and we could not detect it.** IoT devices get
   compromised and stay compromised for months. A threshold learned from counts moves
   monotonically with the worst thing in the training window: one scan episode in the
   "normal" history pins the threshold above scan level, and the detector goes permanently
   blind on exactly the device that needed it. A robust design (trimmed quantile rather than
   max, a sanity ceiling) has free parameters that can only be chosen by measuring.
3. **It would foreclose the better mechanism.** `fit(events) -> Detector` learns *a number*.
   The strong IoT signal is not "contacted 43 hosts" but "contacted 43 hosts, 40 of which it
   has **never** contacted before" — the learnable object is a *set*, and using it needs
   cross-run persistent state (the deferred asset-enrichment work), not a per-case `fit`.

Trigger to revisit: a flow corpus with clean/faulty separation, at which point the quantile
and the contamination guard become measurable rather than assumed.

## Consequences

- The FLOW lane is judged. `demo_flow.yaml`: a compromised camera sweeps 41 hosts for
  telnet; the sweep merges into ONE collective detection (score 43 vs boundary 20 — the
  camera's own broker and NTP server are legitimately distinct destinations in the same
  minute), giving priority 44.1 / MEDIUM on a high-criticality asset.
- **Corroboration reaches 2.0 for the first time.** `demo_trimodal.yaml` runs telemetry, log
  and flow detectors on one camera in one five-minute window: 4 detections across 3 lanes.
  The identical flow sweep scores **44.1 / MEDIUM alone and 76.4 / CRITICAL corroborated**.
  Same evidence, different confidence, because three independent modalities agree.
- The `volume` factor is exercised honestly: 4 distinct judgments, not one restated.
- No new runtime dependency and no contract change. Pure Python; the numpy-only surface
  (ADR-0012) is untouched.
- A third detector in the volumetric family made the sliding-window skeleton worth sharing
  (D4). `detect/` now has three shared pieces: `_group.py` (routing laws), `_runs.py`
  (row-mask -> spans) and `_windows.py` (time-window -> episodes). A new volumetric detector
  supplies an aggregate and inherits the rest.
- **Not benchmarked.** There is no labelled flow corpus, so this ships in the same position
  `log_burst` is in: the port suite says it *fits*, nothing yet says it is *good*. The
  candidates (CICIDS2017, UNSW-NB15, TON_IoT) all carry licensing questions, and CICIDS2017
  in particular has no usable timestamps for interval labels. The realistic next step is a
  synthetic flow corpus in `eval/generate.py`, which already does exactly this for
  telemetry.
- Thresholds are fleet-wide constants until D7's trigger is met, which will produce false
  positives on genuinely chatty devices (a NAT gateway, an update mirror). The
  `home_networks` and per-preset threshold params are the operator's only lever today.
- Deferred: `Fittable` (D7), the peer/port novelty profile it points at, a supervised
  classifier in team5's shape (D1), and a labelled flow corpus.
