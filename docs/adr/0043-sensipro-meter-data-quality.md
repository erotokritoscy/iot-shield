# ADR-0043: SensiPro's meter data-quality anomalies

- **Status:** accepted
- **Date:** 2026-10-21
- **Related:** ADR-0030 (the meter normalizer: gaps yield no event, rollbacks are quarantined),
  ADR-0041/0042 (SensiPro as an OT data source, pulled from its database), ADR-0032 D4
  (detector-tier enrichment)
- **Scope:** a `QualityIssue` vocabulary in `meter.py`, tagged rejects in the meter
  normalizer, a new `meter_quality` detector. **No contract changes.**

## Context

The iotShield prototype charted SensiPro's own anomaly records: six types in its
`anomalyclass` table (heartbeat; valueError, zero, negative, high, small). The program that
wrote them, `~/projects/sensiproscript/script_finderrrorvalues.py`, compares each pair of
consecutive **raw** counter readings `v1 → v2` of a meter. The raw readings come from the
per-device `dst_*_atomic` tables, measured at a median of 3.2 and a mean of 3.5 minutes apart.
The script writes four of the six types:

| Type | SensiPro code | Rule |
|---|---|---|
| zero | (2,1) | `v2 = 0` and `v1 ≠ 0` |
| negative | (2,2) | `v2 < 0` and `v1 > 0` |
| high | (2,3) | both above 0, and `v2 − v1` > 35 kWh or 0.075 m³ |
| small | (2,4) | both above 0, and `v2 < v1` |

`heartbeat` (1,0) and `valueError` (2,0) are defined, but this script never writes them.

iotshield reads SensiPro's hourly rollup (ADR-0042), not the raw series. Its meter
normalizer already **refuses** four of these cases at ingest, because consumption across them
is undefined (ADR-0030):

- a counter that is unreadable
- a counter that is negative
- a counter that falls (to zero or below the previous reading)

A detector never sees those readings.

## Decisions

### D1 — Six types, found in two places, named once

`meter.QualityIssue` names the six types exactly as SensiPro does and carries SensiPro's
`(category, subcategory)` codes. They are found where iotshield can actually see them:

| Type | Found by | As |
|---|---|---|
| valueError | meter normalizer | a reject: the counter is missing, unreadable or not finite |
| negative | meter normalizer | a reject: the counter is below zero |
| zero | meter normalizer | a reject: the counter fell to 0 |
| small | meter normalizer | a reject: the counter fell, but not to 0 |
| heartbeat | `meter_quality` | a detection: consecutive events more than `max_silence_hours` apart |
| high | `meter_quality` | a detection: an hour's consumption above `high_kwh` / `high_m3` |

We considered the alternative, letting the bad readings through as flagged events so that one
detector reports all six, and rejected it. It reverses ADR-0030: a counter reset would flow
into every meter detector as data, and "consumption across a reset" would need a value it
does not have.

### D2 — A reject's type is a prefix on its reason, not a contract field

`MalformedRecord` carries its reason as one string. The type is the prefix `[zero] …`, written
by `tag_reject` and read back by `reject_issue`; the TypeScript side reads it the same way.
The reason text after the prefix is unchanged. Rejects that are none of the six types, such
as a duplicate timestamp or a header problem, carry no prefix. A contract field for one label
would have needed an ADR of its own and a migration of every stored record.

### D3 — `meter_quality`: written rules, no learning

The detector consumes the telemetry lane and the `meter` payload, like `meter_iforest`, so the
guard pairs it only with meter sources. It has no fit and no artifact.

**heartbeat.** The normalizer emits no event across a gap: the reading after a gap only opens
the next interval. So one missed hourly reading leaves the neighbouring events **3 h** apart,
and the default `max_silence_hours: 2` catches a single miss. The detection spans the silence
and reports the number of missed readings (`hours/interval − 2`). An end-to-end test checks
that arithmetic through the real normalizer.

**high.** Consecutive hours above the limit merge into one detection (`merge_runs`) with the
peak as its score, so a sustained surge counts as one finding in the alert engine's `volume`.

Every detection carries:

- `evidence.quality`: the type
- `evidence.sensipro_code`: SensiPro's `(category, subcategory)`
- a detector-tier enrichment: the threat class "Meter data quality: …", two suggested actions,
  and **no MITRE technique**, as with `log_burst`'s error storm. A silent or surging meter is
  an operational fact first, and naming an attack technique for it would be invention.

### D4 — The `high` defaults are SensiPro's numbers, knowingly too strict per hour

The defaults are 35 kWh and 0.075 m³, so the two systems start from one written agreement.
They are params, because the script applied them per raw step (~17 per hour); hourly they are
about 17 times stricter. The hourly equivalents would be about 600 kWh and 1.3 m³.

Measured on 1–7 Jul 2023 of the real fleet:

- **0 heartbeats and 0 rejects.** The hourly rollup is complete for that week.
- **high: 294 detections on 42 of 373 water meters, and 35 detections on 5 of 440 electricity
  meters.** Every one is a meter that is above the limit continuously, for 23 hours a day:
  large consumers, not anomalies.

The runs are 23 hours long because the rollup repeats 23:00's counter at 00:00, so every meter
shows zero consumption at midnight. This is a property of SensiPro's rollup, recorded here
because it caps the length of any run of high hours at 23.

## Consequences

- With the defaults, `high` is a list of the fleet's big consumers. Comparing a fixed absolute
  limit against meters of very different sizes cannot do better. A per-meter limit is what
  `meter_iforest` already learns, so the two detectors are complementary rather than
  redundant. Raising the defaults to the hourly equivalents is a one-line change, left as a
  decision for the user.
- The OT charts (the next phase) can count all six types:
  - detections, by `evidence.quality`
  - rejects, by their `[type]` prefix
- `zero` and `small` are told apart by the counter's new value only, as the script does.
  A real meter replacement and a corrupted reading both read as `small`.

## Amendment (2026-10-21): SensiPro's filler hours

SensiPro's hourly rollup never leaves an hour out. When no usable raw reading arrived, it
writes the hour anyway, repeating the previous counter, and says so in two columns:
`count_accepted` and `count_rejected`. Read by `last_value` alone, every silent hour looked
like an hour of **zero consumption**, so the real data showed no heartbeat and no valueError
at all.

The `sensipro` connector (ADR-0042) now honours the two columns:

| `count_accepted` | `count_rejected` | The connector | Becomes |
|---|---|---|---|
| 0 | 0 | leaves the hour out | a gap, so a **heartbeat** |
| 0 | > 0 | writes the counter as `rejected-by-sensipro` | a **valueError** reject, tagged with its meter and hour |
| anything else | | passes the row on | the hour, as before |

The row ceiling is now judged on rows *read*, not rows written, so dropping hours cannot
hide a truncated pull.

**Measured.** The 10 meters that fail from 2023-07-18 have 7,091 hours with nothing received
and 17,286 with every reading rejected. Pulled for all electricity meters, 2023-07-17 to
2023-07-22, the result is:

| Type | Found |
|---|---|
| valueError | 728 |
| small | 48 |
| zero | 3 |
| heartbeat | 12 |
| high | 74 |

The counter drops (`small`, `zero`) were hidden behind the filler before.

**Limit.** A heartbeat needs a consumption event on both sides of the silence. A meter still
silent at the end of the pulled window shows its rejects but no heartbeat. Pull through its
recovery to see it.
