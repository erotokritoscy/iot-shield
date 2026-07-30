# ADR-0037: A detector nothing can measure

- **Status:** accepted
- **Date:** 2026-07-30
- **Amends:** ADR-0035 D2 (which removed a bad *default* by removing the whole choice), ADR-0021
  D5 (a renderer is one line per event), ADR-0027 D1 / ADR-0028 D6 (what a label is made of, and
  what a fault magnitude is quoted against)
- **Related:** ADR-0030 (the meter vocabulary, one model per meter), ADR-0034 (a baseline
  describes one device), ADR-0022 (hyperparameters vs fitted state), ADR-0026 amendment /
  ADR-0027 D8 ("reported, not refused")
- **Scope:** the Evaluate page's detector list, the corpus generator, and one new corpus. No
  contract, no capability, no adapter, no API field changes.

## Context

Asked while looking for `meter_iforest` on the Evaluate page: *why can I not select it?* It is
registered, shipped, demoed and documented. It appeared in no corpus's detector list.

Not a UI bug. The page filters detectors to those a corpus can feed — `lanes.some(...) &&
payloads.every(...)`, the predicate ADR-0035 D2 introduced. `meter_iforest` requires
`payload:meter`, and **no corpus in this product carried one**: the three shipped bundles are
`stub-csv`, `syslog` and Zeek, and the fetchable SKAB is `stub-csv`. So the filter removed it
from every corpus at once.

That is worth naming precisely, because it is a category error rather than an off-by-one. A
filter that removes an option in *every* case has stopped being a filter and become a deletion.
The page reported five detectors where the registry has six, and the reason — that there is no
metering corpus — was hidden along with the thing it explained. Two years of an intern's
successors would read that page as the truth about what the product can be benchmarked on.

The CLI was never fooled: `evaluate` runs the pairing happily, exits 0, and prints a row of
zeros with **two** explanations, one from the guard reasoning about declarations and one from the
detector's own `Diagnosable` answer. So the harness already implemented "reported, not refused"
for exactly this case. Only the page disagreed with its own engine.

Behind the UI question sat the real one: **`meter_iforest` was the one shipped detector the
evaluation plane could not measure at all.** Nothing said so. The meta-test written to catch
precisely this (`test_every_benchmarkable_format_can_be_generated`, added when `zeek-conn` sat in
the same hole) intersects the consumable formats with `_EXTENSIONS`, and both meter dialects were
absent from that table — so they were invisible to the guard rather than reported by it. A guard
that polices an intersection cannot see something missing from either side of it, which is the
same shape of hole as the filter that hid its own subject.

## Decision

### D1 — Mark, do not hide

Every registered detector stays in the list. The ones a corpus can feed sort first and are the
only ones eligible to be **defaulted** to; the rest are shown with the reason inline (*needs
payload `meter`, this corpus carries none*) and remain selectable.

This keeps ADR-0035 D2's bug fixed and drops its remedy. The bug was that `meter_iforest` sorted
alphabetically first and became entry 1's *default*, so a meaningless benchmark ran without
anyone choosing it. Defaulting from the eligible subset is the whole fix. Removing selectability
was a second, larger step that cost three things:

- **A measurement.** "What does this detector do on data it cannot read?" is a legitimate
  benchmark question — the control condition — and the engine answers it with two sentences.
- **The gap.** Nobody noticed there was no meter corpus for the whole life of ADR-0035.
- **The registry's shape.** An unmarked absence reads as "this does not exist". The tell is the
  empty-state notice ("No registered detector can read this corpus") that could never fire.

The rule is now the guard's own two severities, applied one layer up: **refuse what is
structurally broken, mark what is merely futile.** A corpus/spec *shape* mismatch stays refused;
an idle detector is marked. The page stops having a third opinion.

### D2 — `BEHAVIOURS` is keyed on `(lane, payload)`

The generator was written when a lane *was* the whole shape, and encoded that in its behaviour
registry (`dict[Lane, Behaviour]`) and in `build._resolve`. Metering is where that stops being
harmless: a meter behaviour is a **second telemetry-lane behaviour**, so keyed by lane alone it
collides with `SensorRig`, and a meter renderer would be handed plain numeric readings carrying
no `MeterReading`. This is ADR-0035's insight reaching the one place that ADR did not — not new
architecture, the same conflation removed twice.

`faults.lanes_of` deliberately still answers with a *lane*: a label is indexed by `(asset, lane,
instant)` and has no payload axis (ADR-0028 D1), so two shapes on one lane correlate, which is
the same argument ADR-0035 made for refusing `Lane.METER`.

### D3 — A renderer is not one line per event

It cannot be, because a normalizer need not be a per-record function. The meter normalizer
*differences* a cumulative counter, so N consumption events are the differences of N+1 counter
readings and the file legitimately holds one row more than there are events — one opening row per
contiguous run. There is no way to write N rows and read N events back.

The ordering law survives; its *test* had to change. `reversed(render(reversed(events))) ==
render(events)` is "one line per event" in disguise: with k lines per event, reversing the output
also reverses the lines within each event, so the mirror can only hold when k is 1. What was
actually being protected is that the output **depends** on input order (a renderer that sorted
internally would be order-blind) and **loses nothing** in either direction. Both hold for any k,
and an internal sort still fails the first.

### D4 — A label is the instants whose *observation* changed, not whose bytes changed

Perturb one hour of a cumulative counter and every later row differs, permanently. The label law
compared rendered lines, so on this lane it would have demanded a label over the entire remainder
of every faulted recording — marking as faulty thousands of hours at which nothing was observed
differently, and charging a detector for missing them. That is the exact failure the law exists
to prevent, arrived at by applying it literally.

A running total is a statement about the past, not an observation at the instant carrying it.
What a detector reads at instant T is the consumption for T, so that is what both the behaviour's
`expressed` and the label-law test compare. For the four non-differencing lanes the line *is* the
observation and this is the previous comparison unchanged.

### D5 — A boundary may name no registry param, if the detector learns its line

`Calibration.param` becomes optional. `intensity` is multiples of the detector boundary a
behaviour declares (ADR-0028 D6), and a meta-test pins each boundary to the registry's default
for that param — the right law while a threshold is something a human typed. `meter_iforest` has
none: it fits an isolation-forest score to a false-alarm budget on each meter's own history.
Pointing at `fp_budget` would be worse than nothing, a rate standing in for a magnitude.

So the metering boundary is quoted in units the behaviour owns — **3x what this hour of the week
normally draws** — and the escape hatch is narrowed two ways. `param=None` is allowed only for a
**`Fittable`** detector, which is checkable and is exactly the class that learns its own line.
And because a number in the behaviour's own units can only be validated by measurement, the
registry check is replaced by a *behavioural* one: the corpus asserts that the floor shape is
caught at the declared intensity and that the discriminating shape is caught at all.

### D6 — Formats are partitioned, not intersected

`NOT_STORED` names the formats a normalizer consumes that a bundle deliberately does not hold,
with a reason at each entry: `eoa-meter-csv` (a second meter dialect — the shape is already
storable through SensiPro, so a renderer would buy coverage of a parsing quirk, not of a
modality) and `normalized-jsonl` (a bundle of already-normalized events would skip the
normalization a benchmark exists to measure). The meta-test now requires every consumable format
to be in exactly one of the two sets, so an omission is reported instead of invisible.

## Consequences

**`meter_fleet` ships.** Four recordings, two meters (water and electricity), one fault shape
each plus a clean recording, 420 KB, three seconds to generate. Its length is **computed**, not
typed: `meter_iforest` refuses a meter with less than `min_train_hours` of clean history — itself
derived from `fp_budget` — and `prefix` reserves only a fifth of the clock, so the recordings are
~5,250 hourly readings. Sizing it by hand is how a corpus ends up one row short of fittable and
reports a table of skips that reads like a broken detector. The first attempt was exactly that,
off by the opening reading that closes no interval.

**Measured, at the product's real operating point** (`--fit-protocol prefix`, every
hyperparameter default, fitted 4/4):

| case | incidents | detections | event recall |
|---|---|---|---|
| `spike-04` (surge, 1.3x boundary) | 1 | 26 | 1.00, first alert within the hour |
| `stuck-04` (frozen register) | 1 | 17 | 1.00, first alert within 8 hours |
| `drift-11` (leak ending at the boundary) | 1 | 1 | 0.00 |
| `clean` | 0 | 35 | — |

Pooled: evF1 0.23, evP 0.14, evR 0.67, `ovl` 0.95. Two findings, neither tuned away:

- **The frozen register is caught**, and it is the shape that justifies this detector existing.
  Consumption reads exactly zero, which is an ordinary level for 3am and impossible at 7pm; no
  magnitude rule can separate those and a model of normal can.
- **Precision is ~3x the declared `fp_budget`** (35 alarms over ~8,400 clean meter-hours against
  ~1 per 730). The likely cause is that the reserved prefix is ~1,050 hours, a short sample from
  which to estimate a 99.86th percentile, so the fitted threshold is optimistic. Recorded rather
  than fixed by lengthening the corpus until the number looked better, which is ADR-0021 D7's
  circularity wearing a different hat. It is a real question about the operating point.
- **The drift is missed** at intensity 1.0. A 168-hour rolling window adapts to a 72-hour ramp,
  so this is the shape where latency separates detectors — and 1.0 means *exactly at the alarm
  line*, so a marginal result is the definition of the boundary rather than a surprise. Left
  unpinned by any test for that reason.

**`meter_iforest` is selectable on 1 of 4 corpora and explained on the other 3**, where before it
appeared on none. The two changes are worth keeping distinct in memory: D1 alone would have made
the gap *visible*, and D2-D6 are what closed it.

**Open**, all four found by review rather than by the work, and all deliberately not fixed here:

- **`build._check_lanes` is still lane-only.** It refuses two formats on one *lane* because a run
  pools its sources and events would double — true of two dialects of one shape, false of two
  shapes on one lane. As written it forbids exactly the corpus D2's own rationale calls
  desirable: a consumption anomaly and a temperature spike on one device, corroborating. It
  cannot simply be relaxed, which is the deeper point — `_observe_lane` runs a behaviour over
  every device in the recording, so a camera in a metering recording would silently take the
  default meter profile. Letting a *device* declare which shapes it is on is the real fix, and it
  is a design change rather than a tidy-up. Today only `_check_lanes` and `MeterFleet`'s clock
  precondition stand between us and that nonsense: two accidental guards for a missing concept.
- **The hourly cadence is a precondition inside one `observe`, not a declaration.** So the test
  fixture identity-checks the meter payload to know which clock to build. `Behaviour.step` on the
  port would let `_observe_lane` check it once for every behaviour, and is the right altitude —
  but it is protocol machinery for a single implementor, which is the same reason
  `meter.CUMULATIVE_FIELDS` is one frozenset rather than a per-payload table. A second
  payload-bearing shape is what earns both.
- **`NOT_STORED` hangs a policy on a cosmetics table.** `_EXTENSIONS` is documented as what a
  human sees in a file listing, with unknown formats explicitly fine; membership in it now also
  means "a bundle may hold this", which is one table carrying two facts and is why the meta-test
  has to assert the two sets are disjoint. Nothing enforces it either — writing a bundle whose
  input is `normalized-jsonl` still succeeds, the case `NOT_STORED` says cannot happen.
- **`NOT_STORED`'s `normalized-jsonl` entry is an argument, not a proof.** If a passthrough
  corpus ever turns out to be worth having, the reasoning there is what has to be revisited.
- **`meter_fleet` has no baseline arm.** Every other corpus is read against `bench/stub.yaml`,
  the floor that makes "has this detector earned its complexity" answerable. That detector
  compares an absolute value in the channel's own units, and its 80.0 was chosen to straddle a
  rig's pressure — three orders of magnitude above a meter's hourly draw, so paired here it
  scores zero for a units reason rather than a detection one. Measured, to be sure it was the
  units: zero detections on all four cases, including the surge at 3.9x normal. A meter-scaled
  threshold would be a number we chose, which ADR-0021 D7 is about. So this corpus reports
  `meter_iforest` against nothing, and what a floor should be on a lane whose units are
  site-specific is genuinely unresolved.
