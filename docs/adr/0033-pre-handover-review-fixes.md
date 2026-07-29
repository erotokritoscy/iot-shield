# ADR-0033: What the pre-handover review changed

Status: accepted (2026-07-29)

## Context

Before handover, the whole product was reviewed by ten parallel readers, each scoped to one
plane, with every finding independently reproduced before it was accepted. The suite was
green throughout (1,123 passing tests), so nothing here is a test failure: these are the
defects a test suite structurally cannot catch — a number that is wrong rather than absent,
a guard that cannot fire, a silence that reads as an all-clear, a document that describes
behaviour the code stopped having.

They are collected in one ADR because they were decided together and share one theme, not
because they touch one module.

## The theme

**A system must not be able to say something it does not know.** Nearly every finding is an
instance: a factor that counted rows and called it corroboration; a grid that made a
published severity a property of the wall clock; a benchmark that annotated one spec with
another's warnings; a detector that skipped an asset and reported the same thing it reports
for a clean one. In each, the output was *confident* and *wrong*, which is worse than an
error, because nothing downstream — including a reader — has any way to notice.

## Decisions

### D1 — `volume` counts distinct judgments, not detection rows

`(lane, window_start, window_end)` is the whole of what a detector claims (ADR-0028 D1), so
two rows describing the same lane over the same window are **one** claim however many
detectors produced them. `method` is deliberately excluded from the key: including it counts
producers again.

Measured on the shipped `config/demo_rules.yaml`: the gateway's three flow detections
included two rules matching the *same* Zeek record, which put `volume` at 1.30 instead of
1.15 and the alert at **50.037 — clearing the HIGH floor by 0.037 points**. It is now
45.303/MEDIUM, and both README and CLAUDE.md are corrected.

Rejected: warning whenever two detectors share a lane. `demo_rules.yaml` ships `rules(lane:
flow)` *alongside* `flow_burst` on purpose, because signatures and statistics are
complements (ADR-0032) — so that guard would fire on the best demo. The defect was the
factor's definition, not the configuration.

### D2 — Alerts group by proximity, capped, not by an absolute grid

Grouping keyed on `(asset, floor(window_start, bucket))`, a lattice anchored to the epoch.
Membership therefore depended on *where* an incident fell rather than how close its
detections were: three detections 25 s apart scored CRITICAL at 12:00:00 and split into HIGH
+ MEDIUM at 12:04:40. Only `window_start` was read, so a long collective span did not absorb
a detection lying inside it.

Now: sort per asset, open a new bucket when `window_start - running_max_end > bucket`.
Sorting restores the order-independence the grid got for free; comparing against `max_end`
is what fixes containment. Single-link chaining is transitive, so `MAX_BUCKET_SPAN = 10
minutes` bounds one alert — a product judgment about what reads as one incident, not a
derived quantity.

### D3 — A detector may say why it will be silent (`Diagnosable`)

`team3_zscore` skips an asset whose channels its artifact never learned; `meter_iforest`
skips a meter it has no model for. Both correct, both previously indistinguishable from "no
anomalies". `why_silent(events)` is an **optional, pure** capability — a predicate over the
same events `detect` would see, not state recorded during a run — consulted by both the
runner and the harness. Not implementing it is not a defect.

### D4 — The label law is enforced, not just documented

ADR-0027 D1 says a label is made of the instants a behaviour *actually perturbed*. Nothing
checked it: a behaviour returning the declared window instead of the measured perturbation
passed every test. `tests/eval/generate/test_label_law.py` rebuilds each scenario with
`faults=()` and diffs the normalized events, which works because faults are applied last and
streams are name-seeded. The three shipped corpora pass exactly; the check was
mutation-tested (widening a span → caught, dropping a label → caught).

The builder additionally round-trips every rendered case through the matching normalizer, so
an asset id that does not survive its own renderer is refused at generation rather than
producing an answer key that names a device absent from the data.

### D5 — Every value-bearing boundary refuses what it cannot represent

Collected, because they are one rule applied in seven places: non-finite numbers inside
`evidence`/`metadata`/`score_components`; a timestamp that overflows on UTC conversion; a
meter interval of zero; a meter id reporting two utilities; a `stub_threshold` on a lane
carrying no value, or with a non-positive threshold; a sink path that names no file; an
action step the `action_text`/`action_steps` round trip would lose.

Each previously produced a crash, a phantom number, or a silent drop. Each now produces a
`MalformedRecord` or a construction error naming the field — ADR-0004's law, applied where
it was missing rather than restated.

### D6 — Reads are pageable and say how much they left behind

`repository.Page` carries `items` and `total`; the read API publishes `X-Total-Count` and
accepts `offset`. A header rather than an envelope, so the response body stays exactly the
contract objects and the generated TypeScript keeps working. `/alerts` gains `asset_id` and
time bounds, which the ranked queue — the product's headline output — could not previously
be filtered by at all.

### D7 — CI runs the gate it claimed to run, on the interpreter it claims to use

CLAUDE.md said CI ran `npm run check`; it did not, leaving ~10k lines of the product UI
unverified. Nothing pinned a Python version either, so CI resolved whatever the runner
shipped — which is how a committed corpus became unrebuildable on 3.12 while passing on 3.13
(the defect ADR-0028's amendment fixed). A `web` job and `.python-version` close both.

## Consequences

- **Published numbers moved, and the docs moved with them.** `demo_rules`' gateway is
  MEDIUM; the stub engine can now emit LOW (its floor was an accident of starting at 30);
  the meter demo's score/threshold shifted because the DST fix recovered a lost hour of
  clean history, so its artifact was regenerated.
- **`bucket_minutes` changed meaning** from grid width to maximum gap. Same units, same
  default, and the new reading is the one operators already assumed.
- **Two findings are deliberately NOT fixed here** and are the honest open items for
  whoever picks this up: `intensity` does not land exactly on a detector's boundary (the
  injected quantity is converted, the device's own baseline is not), and adding a fourth
  modality is not "one behaviour + one renderer" because `BEHAVIOURS` is keyed by `Lane`
  and `Calibration` cannot express a learned boundary. Both need design, not a patch.
