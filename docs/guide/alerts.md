# Alert prioritisation

An alert engine turns detections into a ranked queue. It is the last stage that makes a
decision, and the only stage whose output an analyst reads directly.

Read `docs/guide/architecture.md` first, and `docs/guide/detectors.md` if you want to know
where detections come from. This guide assumes you know what a port and a contract are.

## What you get and what you owe

The port is one method:

```python
def build_alerts(
    self,
    detections: Sequence[DetectionResult],
    assets: Mapping[str, Asset],
) -> list[PrioritizedAlert]
```

Two inputs, because an alert is a judgment about a *device*, not only about a signal.
The detections say what was seen; the asset map says how much the device matters.

Four laws, enforced by `tests/ports/test_alert_engine_port.py`:

1. **Every detection is covered by exactly one alert.** Ranking filters, not a hidden gate.
   Nothing a detector found is allowed to disappear here.
2. **An alert's window spans its detections' windows, and its asset lineage is consistent.**
   Every detection in an alert belongs to that alert's asset, and lies inside its window.
3. **Corroboration is monotone.** Adding a corroborating detection to a bucket never lowers
   its priority.
4. **An unknown asset degrades gracefully.** A device nobody registered still gets an alert.

Law 1 is the product's central promise and the reason this layer scores rather than
filters. A detector that fires is never overruled; it is placed in a queue.

Law 3 is the one that constrains how you may write a formula, and it is easy to violate
by accident. See "Keeping monotonicity" below.

## The two that ship

| Name | Scoring | Reaches | Rationale |
|---|---|---|---|
| `stub` | additive, four terms | LOW to EMERGENCY | one sentence of context |
| `weighted` | multiplicative, four factors | LOW to CRITICAL | the arithmetic, factor by factor |

Both share correlation, ranking, severity labelling and threat classification. Only the
per-bucket number differs, which is roughly fifteen lines in each adapter.

Swapping one is a single field, in a config or in the Compose page's alert-engine section:

```yaml
alert_engine:
  name: stub          # ...or weighted
```

Run the same input through both. This is the demo walkthrough's three-lane run, which
"Reading a queue" below takes apart factor by factor:

```
weighted   1. [CRITICAL]  10.0.0.24 priority=80.0    2. [MEDIUM] 10.0.0.11 priority=39.6
stub       1. [EMERGENCY] 10.0.0.24 priority=100.0   2. [MEDIUM] 10.0.0.11 priority=38.0
```

Identical events, identical detections. The order agrees here and the *severity* does not,
which is the half an analyst acts on: `stub` adds its terms and clamps, so three lanes
agreeing carry the camera past 100 into a band `weighted` cannot reach at all. That
difference, on fixed detections, is the whole reason the port exists, and it changes no other
stage.

## The shape of the work

Every engine does the same three things in the same order, and `alert/_correlate.py` owns
all three:

```
detections --> bucket by (asset, proximity) --> one alert per bucket --> rank
                  bucket_by_asset_window          build_alert (yours)     correlate_and_rank
```

`correlate_and_rank` is the alert-side analog of `normalize._linewise`: it takes your
per-bucket scoring as a callback and owns everything else. An adapter's `build_alerts` is
three lines:

```python
def build_alerts(self, detections, assets):
    return correlate_and_rank(
        detections,
        bucket=self._bucket,
        build_alert=lambda group: self._build_alert(group, assets.get(group[0].asset_id)),
    )
```

Every group handed to `build_alert` is non-empty and single-asset, so `group[0].asset_id`
is safe and you never check for an empty bucket.

Ranking is `(-priority, asset_id, window_start)`. The tie-breaks are there so two runs over
the same data emit the same order; a bare sort on priority alone leaves equal-scoring
alerts in dict order.

## Correlation is proximity, not position

`bucket` is the largest gap that still counts as the same episode. Walking an asset's
detections in time order, a new alert opens when the next detection starts more than
`bucket` after the furthest point the current alert has reached.

Two details are load-bearing.

**The comparison is against the running end, not the previous start.** A detection lying
*inside* a long collective span yields a negative gap and joins it. Comparing starts
instead let one incident become two alerts with overlapping time ranges.

**Sorting comes first.** The walk is order-dependent and the port has no explicit
order-independence law, but a published priority that depends on the order rows arrived in
is not defensible. So `bucket_by_asset_window` sorts, and a test pins it.

### Why not a grid

This used to floor `window_start` onto fixed multiples of `bucket` since the epoch, which
is the obvious implementation and is wrong. A grid makes grouping a question of *where* an
incident fell rather than how close its detections were: two detections seconds apart
straddling a grid line never corroborated, while two nearly `bucket` apart did.

Corroboration is the headline factor, so that made a published severity a fact about the
wall clock. The measured case: the same three detections scored CRITICAL at 12:00:00 and
split into HIGH plus MEDIUM at 12:04:40. `tests/adapters/test_correlation.py` now runs the
same evidence at three positions relative to a grid line and requires one CRITICAL alert
from each.

Flooring also went through `datetime.fromtimestamp`, which raises `OSError` on Windows for
anything before 1969-12-31T12:00Z. One device with an unsynchronised clock took down the
whole run.

### The span cap

Proximity grouping is single-link, so it is transitive: A joins B and B joins C puts all
three in one alert even when A and C are far apart. Without a ceiling a slow drizzle of
detections chains indefinitely and an operator is handed one alert spanning hours, which is
not an incident anybody can act on.

`MAX_BUCKET_SPAN` is ten minutes. It is a product judgment about what reads as one thing,
not a derived quantity, and it is deliberately larger than the default five-minute `bucket`
so it only bites on genuine chains rather than on ordinary corroboration.

## What `volume` counts

A bucket's `volume` factor counts **distinct judgments**, keyed on
`(lane, window_start, window_end)`. That triple is the whole of what a detector asserts
about the world, so two rows describing the same lane over the same window are one claim
however many detectors or rules produced them.

`method` is deliberately not in the key. Including it counts *producers*, which was the
defect this replaced: two rule instances matching one flow record, or two presets of one
adapter judging one reading, bought corroboration that no additional evidence supported.
Measured at the time on a two-rule chain over one camera, that pushed volume from 1.15 to
1.30 and carried an alert over the HIGH floor by 0.037 points.

When rows and judgments differ the rationale says so, and only then. Ingest the demo's flow
capture twice through two identical rule instances and the camera's alert reads:

```
4 detection(s) (2 distinct) across 1 lane(s) on 10.0.0.24 ...
```

Naming the distinct count only when it differs keeps the common case terse and makes the
interesting one, several detectors agreeing about one window, legible rather than looking
like an arithmetic error.

`distinct_judgments` lives in `_correlate` and both engines call it, so they cannot disagree
about what "more of this" means.

## Scoring

Both engines start from the same measure of strength:

```python
def detection_margin(detection):
    return detection.score / detection.threshold   # 2.0 if threshold <= 0
```

A ratio rather than a difference, because scores from different detectors are on different
scales and only "how far past its own line" is comparable across them. The port guarantees
`score > threshold`, so a margin is always above 1. The 2.0 fallback is defensive: our
detectors refuse a non-positive threshold at construction, but a third-party adapter might
not, and a degenerate detector should still read as anomalous rather than dividing by zero.

### `weighted`

Four factors, each at least 1.0, multiplied:

| Factor | From | Range |
|---|---|---|
| `severity` | the strongest detection's margin, capped | 1.0 to 4.0 |
| `corroboration` | 1.0 + 0.5 per extra lane in the bucket | 1.0 to 2.0 |
| `volume` | 1.0 + 0.15 per extra distinct judgment, capped at 4 | 1.0 to 1.6 |
| `asset_weight` | `Asset.criticality` | 1.0 to 1.3 |

Then an absolute, saturating transform to a bounded score:

```python
raw = severity * corroboration * volume * asset_weight
priority = 100.0 * (raw - 1.0) / (raw - 1.0 + 2.0)
```

Subtracting 1 first is what makes a barely-over-threshold detection score near zero rather
than inheriting a floor from the fact that every margin exceeds 1. The saturation constant
puts `raw = 3` at 50 and `raw = 5` at 66.7, and no product of the four caps can reach 100.

`corroboration` caps at 2.0 because there are three lanes and no more. That is structural,
not a tuning choice.

### `stub`

The same signals, added instead of multiplied, then clamped at 100:

```python
base        = 30.0 * max(0.0, min(margin - 1.0, 2.0))    #  0 to 60
corroboration = 20.0 * (len(lanes) - 1)                  #  0 to 40
volume      =  5.0 * min(judgments - 1, 3)               #  0 to 15
criticality =  0.0 / 5.0 / 10.0 / 15.0                   #  0 to 15
```

It exists as the pre-committed fallback: a bounded, monotone toy that keeps the port honest
about being a port. It is not a lesser engine so much as a different opinion, and its terms
sum past 100 when several lanes agree, so it is the only engine that can reach EMERGENCY.

### Reading a queue

The demo walkthrough makes each factor visible by running the *same* devices twice and
changing only how many lanes are judged. `scripts/make_demo_data.py` writes its three inputs;
the two runs are the flow capture alone, then all three files, with every device left to
auto-discovery. The walkthrough registers nothing, because the Compose page has no field for
it and two demos of one scenario must not print different numbers.

Flow lane only, one `rules` instance:

```
1. [HIGH]   10.0.0.24  52.7 = f(severity 2.55 x corroboration 1.00 x volume 1.15 x asset_weight 1.10 -> raw 3.23)
2. [MEDIUM] 10.0.0.11  39.6 = f(severity 2.10 x corroboration 1.00 x volume 1.00 x asset_weight 1.10 -> raw 2.31)
```

The same capture, plus the camera's syslog and telemetry and the detectors that read them:

```
1. [CRITICAL] 10.0.0.24  80.0 = f(severity 2.55 x corroboration 2.00 x volume 1.60 x asset_weight 1.10 -> raw 8.98)
2. [MEDIUM]   10.0.0.11  39.6 = f(severity 2.10 x corroboration 1.00 x volume 1.00 x asset_weight 1.10 -> raw 2.31)
```

Read the columns rather than the totals. `severity` is **identical** across the two runs,
because the strongest single finding is the same telnet rule at a margin of 2.55: nothing got
worse, more of it was seen. What moved is `corroboration`, one lane to three, which is
1 + 0.5 per extra lane and therefore 2.00, its ceiling with today's three lanes; and `volume`,
two distinct judgments to seven, which caps at four extra and therefore 1.60. Multiplied,
3.23 becomes 8.98 raw, and the queue reads CRITICAL where it read HIGH.

The gateway is the control, and it is what makes the pair prove anything. Its inbound SSH
session is a real finding on one lane, it has no corroboration to gain, and it scores 39.6 in
both runs. A priority that rose everywhere would only show that scoring had changed.

`asset_weight` is the one factor an operator sets directly, and the one this walkthrough
deliberately leaves alone: both devices carry 1.10, the auto-discovered MEDIUM default.
Register the camera HIGH from a config file and it weighs 1.20 and scores 55.7 and 81.5
instead — the same two severity bands, which is the honest size of what registration buys.

That is what "prioritisation" means here. It is not a better detector; it is an ordering over
the same findings that spends an analyst's attention on the device several independent
sources agree about.

### Keeping monotonicity

Law 3 is a real design constraint, and the two obvious ways to build a scorer both break it:

- **Batch-relative normalisation.** Min-max scaling over the batch makes an alert's score
  depend on the other alerts in the run, so a stronger finding elsewhere silently rescales
  everyone down. This is what the upstream module this engine borrows from did, and it is
  the reason it could not be wrapped (ADR-0013).
- **A repetition penalty.** Treating repeated detections as noise-to-suppress is the exact
  inverse of corroboration, and inverts the law rather than bending it.

The safe recipe: every factor at least 1 and non-decreasing as detections join a bucket
(multiplicative), or every term non-negative and non-decreasing (additive). Then
monotonicity and the [0, 100] range both hold by construction rather than by test, and the
port suite confirms rather than discovers.

## Severity labels

The priority-to-label mapping is shared, absolute, and lives in `_correlate` so that a 70
reads as CRITICAL regardless of which engine produced it:

| Priority | Label |
|---|---|
| 90 and above | EMERGENCY |
| 70 to 90 | CRITICAL |
| 50 to 70 | HIGH |
| 30 to 50 | MEDIUM |
| below 30 | LOW |

Bands are inclusive on the left.

What is shared is the *mapping*, not the reachable range. `weighted`'s factor caps multiply
to 16.64, which the saturating transform maps to 88.66, so EMERGENCY is out of reach for
that engine by construction. `stub` reaches 100. A severity is therefore fully comparable
between two alerts of one engine and only loosely between engines.

If you want to widen a range, that is a scoring change: raise a factor cap or the
saturation constant in the engine. Never move a floor here, or the two engines stop
agreeing about what a 70 means.

## What the alert says

Three fields go beyond the number: `threat_class`, `rationale` and `recommended_action`.
They are decided together, by the same rule, and `narrate` in `_correlate` owns all three
for both engines. What an alert *says* is a product convention, not a property of whichever
formula produced its score.

A `DetectionResult` says "this asset, this lane, this window, this far past the threshold"
and deliberately says nothing about what kind of thing that is, because for half the shipped
detectors there is nothing to say. A robust z-score knows a reading is far from normal and
knows nothing else. Two kinds of producer do know: an authored **rule** carries the
classification its author wrote down, and a named **preset** like `flow_burst:fan_out` names
a phenomenon we chose deliberately. So `rules`, `log_burst` and `flow_burst` always carry a
claim; `stub_threshold`, `team3_zscore` and `meter_iforest` never do.

That knowledge crosses the detect-to-alert seam as a typed record under one `evidence` key,
which is the same solution the FLOW and METER payloads use at the previous seam. The alert
layer reads it through `Enrichment.from_detection` and names no evidence keys itself.

### Precedence, and the union

`promote_enrichment` picks one claim and merges the advice:

1. **`threat_class` is single, so one claim must win.** Highest tier first (`RULE` beats
   `DETECTOR` beats `HEURISTIC`), then the strongest detection within that tier, then
   position in the bucket. Deterministic all the way down.
2. **`suggested_actions` are unioned** across every contributing detection. An analyst
   looking at a device showing both failed logins and a bulk outbound transfer wants both
   sets of next steps.

The third tier, `HEURISTIC`, is a label inferred from an anomaly's shape. It is **not
implemented**. The tier exists so the ladder is decided rather than discovered later, and
`is_heuristic` marks such a claim `(inferred)` in the rationale, because a tool that
presents a guess in the same voice as a fact loses the analyst's trust in the whole column
the first time it is wrong.

Absent any authored claim the threat class stays `"unknown"`, which is an honest "nothing
here knows what this is" rather than a gap. The constant is named once in `_correlate` and
is deliberately equal to `PrioritizedAlert.threat_class`'s own default.

Live. Both tiers show up in one queue if you run `rules` and `flow_burst:fan_out` together
over `fleet_scan`'s scanning recording - `gzip -dc data/eval/fleet_scan/cases/scan-one.csv.gz`
into a file, then a two-detector config over it:

```
1. [HIGH] 10.0.0.24 priority=52.7 ... Threat class: Remote Services (T1021),
                                      per rule:telnet-outbound-propagation.
3. [LOW]  10.0.0.53 priority=24.4 ... Threat class: Remote System Discovery (T1018),
                                      per flow_burst:fan_out.
```

Rank 1 is the scanning camera, where both detectors fire into one bucket: the preset counts
38 distinct peers against a boundary of 20, and the rule reads the port they were dialled on.
The rule wins the naming because `RULE` outranks `DETECTOR`, and the preset's own claim stays
in the evidence rather than being discarded. Rank 3 is the deliberately chatty mirror in the
same capture, which no authored rule covers, so there the preset's claim is what reaches the
alert.

The demo walkthrough is the degenerate case of the same thing. Configure `flow_burst:fan_out`
beside the rules there and it finds nothing - the camera probes six hosts against that
boundary of 20 - so the rule names the alert unopposed.

`origin` is what makes a claim attributable: `rule:telnet-outbound-propagation` rather than
an anonymous label. It exists partly because the Detector port gives a detector one
`method`, so a rule detector holding six rules cannot name the firing rule there.

### Enrichment never moves the number

Worth stating plainly, because it is the property that keeps the queue trustworthy: adding
enrichment changes what an alert says and never what it scores. A test pins it. Otherwise
writing a rule with a longer action list would be a way to buy priority.

### The checklist that is one string

`recommended_action` is a single `str` in the contract, but the thing it holds is a
checklist of up to six steps unioned from every rule that fired. `Enrichment.action_text`
joins them; `action_steps` splits them back. They are each other's inverse and live in the
same module so they cannot drift.

That is a claim about our own prose, and prose is exactly what a contributor writes, so
`validate_action_step` refuses a step the pair could not round-trip: empty, containing a
newline, containing an interior `". "`, or missing terminal punctuation. The last is the
dangerous one, because it does not split a step, it merges two and loses an entire
instruction with nothing to show that it happened.

The cap discloses itself. On the walkthrough's three-lane run the camera alert gathers twelve
steps from its four rule findings, and the trim is carried as a real step so the dashboard
renders it with no UI change:

```
Isolate the device and inspect its firmware for compromise. Review which hosts it
contacted on port 23 and whether any answered. Block outbound telnet at the segment
boundary. Review the source IP for other SSH activity against this host. Check whether
the same source later attempts authentication. 7 further step(s) from other findings on
this alert were omitted; see the detections.
```

The winner's own steps are never the ones evicted, because precedence sorts the list before
the cap applies.

The split exists on the TypeScript side too (`web/src/lib/evidence.ts`), for the same
reason and with its own tests. If you change one, change both.

## Where assets come from

`resolve_assets` builds the map, in memory, with no database:

**Registration wins.** Start from the operator's seeded `assets:` block, then add an
auto-discovered stub for every event `asset_id` not already present. A discovered asset is
named after its own id, typed `"unknown"`, and given MEDIUM criticality as a stated neutral
default. `discovered_asset` is shared with the storage layer so the two cannot disagree
about what discovery produces.

This is what the walkthrough deliberately does *not* show: nothing there is registered, so
every device carries 1.10 for the MEDIUM default. Seeding an asset is
the cheapest way to change a queue's order without touching any detector. Note the Compose
page has no field for it, so a run driven entirely from the dashboard discovers every device
at MEDIUM; registration today means a config file or a `POST /runs` body.

Discovered ids are added in sorted order. A set's iteration order follows string hashing,
which Python randomises per process, so identical runs over identical input used to emit
discovered assets in different orders, and that reached the JSONL the file sink writes.

Cross-run enrichment from a persistent store is a deferred follow-on: today a run only knows
the assets its own config seeded plus the ones its own events mentioned.

## Adding an alert engine

Five parts.

1. **The adapter.** A module in `alert/`. Take `bucket: timedelta` in `__init__`, delegate
   to `correlate_and_rank`, and write only `_build_alert`. Use `detection_margin`,
   `distinct_judgments`, `severity_from_priority` and `narrate` from `_correlate` rather
   than re-deriving any of them; that is the whole reason they are there.

2. **The capability**, in the same module. Every alert engine has the same shape, so there
   is no thinking to do:

   ```python
   CAPABILITY = Capability.of(consumes={DETECTIONS}, produces={ALERTS})
   ```

3. **The registry entry** in `pipeline/registry.py`:

   ```python
   ALERT_ENGINES = {
       ...
       "mine": AdapterSpec(_my_alert_engine, static(_MY_ALERT_CAPABILITY)),
   }
   ```

   The factory converts config params to constructor arguments and validates them, which is
   where `bucket_minutes must be positive, got 0.0` comes from.

4. **Export it** from `alert/__init__.py`.

5. **Join the port suite.** Append an `AlertEngineCase` with your registry name. A
   meta-test asserts every registered engine has a case, so skipping this fails the suite by
   name rather than shipping an engine with no coverage.

Then `scripts/check.sh`.

Populate `score_components` with whatever your factors are. The contract does not fix the
keys, the two shipped engines use different ones, and the dashboard renders whatever it
finds, so a new engine's decomposition appears in the UI with no frontend change. Do not
hardcode factor names anywhere downstream.

## Config surface

```yaml
alert_engine:
  name: weighted
  params:
    bucket_minutes: 5
```

One engine per run, one parameter on both shipped engines. Omit the block and the default
applies.

| Mistake | What you get |
|---|---|
| `name: nope` | `unknown alert engine 'nope'; known: stub, weighted` |
| `bucket_minutes: 0` | `invalid params for alert engine 'weighted': bucket_minutes must be positive, got 0.0` |
| `window: 5` | `invalid params for alert engine 'stub': _stub_alert_engine() got an unexpected keyword argument 'window'` |

The unknown name is caught by the compatibility guard, which runs at the top of the ingest
phase before a single record is parsed. The other two are construction errors and land later:
the alert engine is built inside `compute_result`, after ingest, deliberately — the guard goes
first so a structurally broken chain is reported rather than pre-empted by a narrower failure.
All three still precede every sink write, which is the property that matters: an engine
misconfigured is wrong for every detection in the run, so nothing is published.

## Where to look

| | |
|---|---|
| The port and its four laws | `src/iotshield/alert/port.py` |
| Correlation, ranking, labels, narration | `src/iotshield/alert/_correlate.py` |
| The multiplicative engine | `src/iotshield/alert/weighted.py` |
| The additive fallback | `src/iotshield/alert/stub.py` |
| The threat-claim vocabulary | `src/iotshield/enrichment.py` |
| The port suite | `tests/ports/test_alert_engine_port.py` |
| Grouping and volume regressions | `tests/adapters/test_correlation.py` |
| Precedence and the action union | `tests/adapters/test_enrichment_promotion.py` |
| The checklist round trip | `tests/test_action_checklist.py` |
| A ranked queue to read: the walkthrough's inputs | `scripts/make_demo_data.py` |
| Its two runs, pinned by exact counts | `tests/pipeline/test_runner.py` |
| Ranking is not a gate | `docs/adr/0003-data-contracts-v1.md` |
| Why the engine is a rewrite, not a wrapper | `docs/adr/0013-team2-alert-engine.md` |
| Threat class, precedence, the action union | `docs/adr/0032-rule-detector-and-threat-enrichment.md` |
| Proximity grouping and distinct judgments | `docs/adr/0033-pre-handover-review-fixes.md` |
