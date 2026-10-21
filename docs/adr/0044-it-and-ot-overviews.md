# ADR-0044: The IT and OT overviews

- **Status:** accepted
- **Date:** 2026-10-21
- **Related:** ADR-0041 (IT/OT data sources), ADR-0042 (pulling SensiPro and Biology),
  ADR-0043 (SensiPro's six anomaly types), ADR-0031 (the dashboard), ADR-0033 (a capped list
  must say so)
- **Scope:**
  - the read API: an `/overview` router and `storage/overview.py`
  - the meter normalizer: a reject's tag now names its meter and hour
  - the dashboard: IT overview, OT overview and meter pages under Monitor

  **No contract changes.**

## Context

The iotShield prototype had two dashboards, one per source type:

- **Biology (IT):** an attack-vs-normal pie, attacks per day and the flows table.
- **SensiPro (OT):** anomaly counts per day or per hour, anomalies per device, a pie by
  anomaly type and the anomaly records; plus, per device, a day of measurements and a line
  per anomaly type.

We were asked to bring the charts over, drawn from **iotshield's own results**, not from
Biology's classifier or SensiPro's own anomaly tables.

## Decisions

### D1 — Count on the server

The list routes cap at 10,000 rows, and one week of SensiPro meters is 136,000 events, so a
chart counted in the browser would count the cap.

The `/overview` router on the **read** API counts in SQL and returns chart-sized answers:

- **`/overview/it`:** flows, the flows inside a detection, flow-lane detections per day and
  per detector.
- **`/overview/ot`:** the six types (all of them, zeros included, in SensiPro's code order),
  a series by day or by hour, the top 20 meters, and the number of meters affected.
- **`/overview/ot/records`:** the anomalies themselves, with `X-Total-Count`.

Ranges are whole UTC days, both ends included, at most 366 days. Each answer also carries
`latest_day`, the latest day holding any flow (IT) or meter reading (OT). SensiPro's export
is from 2023, so a page that opened on "this week" would be empty with no hint where to
look; the page offers a jump to the week ending at `latest_day` instead.

### D2 — "Attack" becomes "inside a detection"

Biology's `is_attack` is another model's guess (ADR-0042). The IT pie instead splits the
flows that fall inside the window of a **flow-lane detection on the same device** from the
rest. That is what iotshield can actually claim, and the label says so.

### D3 — OT counts both places the six types live

The six types come from two places (ADR-0043):

- **heartbeat** and **high:** `meter_quality` detections, placed by their window start.
- **valueError, zero, negative, small:** tagged meter rejects, placed by the reading they
  are about.

A `MalformedRecord` has no asset and no reading time, so the tag now carries both:
`[zero] meter e1 at 2023-07-01T08:00:00+00:00: …`, read back by `meter.reject_subject`. To
include the meter for a reject about an unreadable counter, the meter normalizer now reads
the meter id before the counter.

A reject whose line yields neither a meter nor a time (an unparseable timestamp, for example)
is still counted by type. It is reported as `unplaced` and is left out of the by-day and
by-meter charts, which have nowhere honest to put it.

### D4 — Charts follow the house rules, with one stated exception

The builders are in `web/src/charts/overview.ts`, following `specs.ts`:

- a magnitude in one hue
- the status red only for "a detector said something"
- UTC on every axis
- `null` when there is nothing to draw

Empty buckets are drawn at **zero**. A count over a range the server was asked about is a
real zero, unlike an evaluation score that could not be measured.

The six types are identities, not a magnitude, so each gets a fixed Okabe–Ito colour that is
the same on every chart. Every such chart travels with a legend and a table, and each type
has an (i) saying what it means.

## Consequences

- **Pages:** Monitor gains **IT overview**, **OT overview** and a **meter** page
  (`/overview/ot/meter/:id`). The meter page shows a day of hourly consumption and one line
  per type. The range and the chosen type are kept in the URL.
- **The OT pages reflect the `high` defaults.** ADR-0043 kept SensiPro's numbers, so a fleet
  week shows the big consumers. Measured on Postgres with the real week: 294 anomalies on 42
  water meters, all `high`.
- **The IT flows table is capped at 500**, and says so with the true total.
- **Older tagged rejects are counted by type only.** Rejects stored by Phase 3 code have no
  meter and time in their tag, so they count as `unplaced`.

## Amendment (2026-10-21): Replay

The overview pages and the meter page have a **Replay** that plays the chosen range forward
in time, as if the results were arriving now. The controls are ▶/⏸, ⟲ and a speed choice
of 1×, 2× or 4×.

**What moves during a replay:**

- The time charts draw bucket by bucket on an axis fixed to the whole range, with a dashed
  "now" line.
- The counters, the pies and the per-meter bars show only what has arrived so far.
- An "arriving now" feed lists the newest records.

**It is a replay, not a live feed.** The dashboard polls nothing (H5), and the data is
historical. The replay reveals counts the page already holds, in time order, and fetches
nothing. When it ends, the page shows the whole range again.

**What it needed from the server and the pages:**

- `/overview/it` gained a `bucket` parameter and a per-bucket `series` of flows, flagged
  flows and detections.
- `/overview/ot` gained `meter_series`, the top 20 meters per bucket.
- Ranges of up to a week are now drawn hour by hour, so a week replays in 168 steps.

It never starts on its own. Under *reduce motion* it steps three times slower, and the feed's
slide-in is off.
