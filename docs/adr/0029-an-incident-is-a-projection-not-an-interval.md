# ADR-0029: An incident is a projection, not an interval

- **Status:** accepted
- **Date:** 2026-07-28
- **Supersedes in part:** ADR-0028 D3 (the shape it produced, not the rule it stated)
- **Related:** ADR-0021 (the evaluation plane), ADR-0026 (per-asset truth), ADR-0027 (per-lane
  clocks), ADR-0028 (the answer key's coordinate system)

## Context

ADR-0028 D1 fixed the answer key's coordinate system: truth is indexed by **(asset, lane,
instant)**, because that is the finest thing a `DetectionResult` can claim. Everything since has
been consequences of taking that seriously.

Two of those consequences are in tension, and this ADR is the resolution.

**D2** said a fault is one cause with several observable *projections* — a compromised camera
scans the network, fails an integrity check, and runs hot — so there is one incident per
`(fault, lane)`, all sharing a `fault_id`. The event level divides by projections because a
telemetry detector can only fairly be asked about the telemetry projection; the alert level
divides by distinct faults because an alert carries no lane and an analyst investigates a cause
once.

**D3** said a fault's label is made of the instants a behaviour actually perturbed, split into
contiguous runs of the lane's own clock — so a quiet stretch in the middle of a fault is not
labelled. This was right and remains right: labelling an unperturbed instant charges a detector
for correctly staying silent, which is the convex-hull defect D3 removed.

D3 was implemented as **one incident per run**. That silently made D2 false: after a split there
can be several incidents per `(fault, lane)`, and `incidents` is what event recall divides by.

### The fragment count is an arithmetic accident

On the **telemetry** lane a fault *modifies readings that already exist*. The sensor keeps
sampling; the fault changes the values. Nothing interleaves, so a fault's instants are contiguous
and the count is always one.

On the **log** and **flow** lanes a fault *adds records* while normal traffic continues
underneath. Whether a routine event lands *between* two injected records or *on top of* one then
decides how many pieces the label has — and a coincident event vanishes, because a clock is a
set of instants.

Measured on a purpose-built syslog corpus. A gateway logs a heartbeat every 60 s; an error storm
injects eight lines across 60 s:

| storm | injections at | heartbeat lands | incidents |
| --- | --- | --- | --- |
| from t=400 | 400, 407, 415, **422**, 430, … | t=420, *between* two | **2** |
| from t=600 | **600**, 606, 612, … | t=600, *on* an injection | **1** |

Identical fault shape, identical device, identical detector output (`log_burst` is a windowed
counter and merges either way into one episode). Over the corpus this moved reported event recall
from **0.50 to 0.60**. Worse, it moved it *asymmetrically between two specs being compared*: the
`auth_failure` and `severity_spike` presets each catch their own two storms and miss the other
two, which is a tie — and the table reported 0.57 against 0.75 because only one arm's fault
happened to fragment.

An author choosing `length_steps=60, intensity=1.6` over `intensity=2.0` has no way to see they
have changed the corpus's denominator.

### Why nobody noticed

All three committed corpora have zero fragmentation across all 19 cases. Their devices are fast
enough that injected records always outpace routine ones, so nothing ever lands in a gap. That is
by coincidence, not by construction — word for word the criticism `build._incidents` levels at
the hull design D3 replaced.

## Decision

**D1. An `Incident` carries `spans: tuple[tuple[datetime, datetime], ...]` instead of
`start`/`end`.** It stops being an interval with metadata and becomes a *projection with an
extent*.

The two levels then read what each needs from one object:

- **point** scoring flags the instants inside **any** span, so the gaps survive and D3's rule is
  unchanged in effect;
- **event** scoring counts **incidents**, so the denominator is one per `(fault, lane)` whatever
  the interleaving does.

`start` and `end` remain as derived bounds — earliest span start, latest span end — because
"when did this fault begin" is a real question that latency measures against and no individual
span answers.

**D2. `overlaps()` tests every span, not the bounds.** Testing `start..end` would credit a
detection landing squarely in a gap, which is exactly the hull.

**D3. Labels schema v5.** `IncidentLabel.spans` is a list of two-element arrays, replacing
`start`/`end`.

**D4. A v4 bundle is refused, not upgraded** — even though it *is* mechanically upgradable (a v4
incident is a v5 incident with one span). Upgrading would be lossless but wrong: v4 wrote one row
per run, so re-reading those rows as one-span projections preserves exactly the inflated
denominator this ADR removes, and silently. Regenerating re-derives the grouping from the
`fault_id`s the generator knew all along.

**D5. `Incident.over(start, end, ...)`** constructs the single-span case. A converted corpus
(`AssetTruth.from_timeline`) has one run per incident by construction — a per-row anomaly column
carries no way to say two runs share a cause — so only a generator that measured *which instants
it perturbed* can produce a gapped projection.

## Consequences

**Single-lane corpora are unchanged.** SKAB under team3 still scores evF1 0.55; every incident
there has one span, so every path is identical.

**Two specs on one corpus become comparable.** The log A/B above now reads 0.67 against 0.67,
which is the honest answer.

**The width column stays informative.** `ovl` measures against the union of spans, so the
fragmented storm scores 0.93 rather than 1.00 — the gap is still there, still costing width,
still visible. Only the *count* changed.

**The timeline draws gaps.** `CaseTimeline.truth` is one bar per span, so the chart no longer
paints over stretches the table beside it scores as clean.

**D2's sentence is now true by construction** rather than true whenever nothing interleaves,
which is the same move D2 itself made when `lanes: frozenset[Lane]` became a required singular
`lane`.

## Rejected options

**Group by `fault_id` when counting events, leave `Incident` alone.** No schema change, no
regeneration, a small localised edit in `score` — and per-lane recall is already a group-by, so
it would be idiomatic. Rejected because it fixes the number without fixing the model: the bundle
on disk still stores fragments, so the answer key still cannot *express* a projection, and
everything reading `incident.start`/`incident.end` (the timeline, a future dashboard view) still
sees pieces. The whole thrust of ADR-0028 was to write the system down and make it true by
construction rather than by a rule applied at each read site.

**Go back to the convex hull.** The hull gives a stable count, which is the property being
sought. It gets there by lying about the quiet instants, which is what D3 was written to stop.
The mirror-image error, not a fix.

**Make truth per-event rather than per-instant**, so a routine heartbeat and an injected error
line at the same instant could be distinguished. Rejected on the same grounds as ADR-0028's
per-metric option: a `DetectionResult` names a window, not a set of events, so a finer answer key
has nothing on the other side to be compared against. An event id would also have to survive
normalization, which fans one wide row into one event per channel — making the id a fact about an
encoding, the thing the coordinate system exists to avoid.

**Refuse a scenario whose fault fragments.** Tempting, since fragmentation is an authoring
accident. But it is *also* a real property of an additive lane — a burst really is interrupted by
routine traffic — and refusing it would forbid describing a true world to protect a metric.
Measure it honestly instead, which is the same stance ADR-0027 D8 takes on a silent detector.
