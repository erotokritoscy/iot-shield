# ADR-0052: A pull has no window limit; a long window is read a quarter per query

- **Status:** accepted
- **Date:** 2026-11-11
- **Related:** ADR-0042 (pulling from a source's own database, D5's limits), ADR-0047 (a pull is
  read by the run; D5 "the 92-day window stays"), ADR-0049 (HTTP API sources), ADR-0050 (jobs)
- **Supersedes:** ADR-0047 D5, and the *Longest window* row of ADR-0042 D5
- **Scope:**
  - `connect/port.py`: `MAX_PULL_DAYS` is gone; `SLICE_DAYS`, `time_slices`, `day_slices`,
    `window_days`
  - `connect/sensipro.py`: `meters_per_query(days)`; the count is summed over time slices
  - `connect/biology.py`: the stream and the count are read a time slice at a time
  - `connect/api/client.py`: `pages` asks for one `day_slices` span at a time
  - `jobs/model.py`: `RollingWindow.days` has no upper bound
  - the dashboard: the job form's `MAX_JOB_DAYS` and the Compose help text

  **No contract changes, no schema changes.**

## Context

Asking for 1,220 days of meters was a 422: *1220 days asked for; one pull covers at most 92*.
ADR-0047 removed the row ceiling, because the run streams a pull and judges it a batch of assets
at a time, so memory no longer grows with the window. It kept the 92-day limit on purpose, because
the limit was protecting the **query**, not the result: each source session has a 30-second
statement timeout, and some of the connector code assumes the window is at most a quarter:

- SensiPro reads **100 meters per query**, a number sized for 100 meters x 92 days x 24 hours. One
  query over 1,220 days would sort 13 times as many rows before returning the first.
- Biology reads the whole window as **one query** ordered by timestamp, and its `COUNT(*)` scans
  the whole window too.
- An HTTP API source stops after `MAX_PAGES` pages **per window**, so a long window could fail
  only because of its length.

Deleting the check alone would have replaced a clear refusal with a timeout partway through a run.

## Decisions

### D1 — The window is unlimited; the query stays bounded

`_window` refuses only an end before the start. `SLICE_DAYS = 92` is now the most days one *query*
covers, and each connector reads a longer window in pieces that each stay within the size the
timeout was measured against.

### D2 — Each connector slices along the axis its order allows

- **Biology** (ordered by time) reads consecutive half-open slices `[a, b)`, `[b, c)`, … in order.
  The concatenation is the same stream, and a row stamped exactly on a boundary is read once. The
  count is the sum of each slice's count.
- **SensiPro** (ordered by **meter**, because the normalizer differences each meter's counter row
  by row) cannot be sliced by time without breaking that order: meter A's second quarter would
  arrive after meter B's first. So it shrinks the meter group instead.
  `meters_per_query(days) = max(1, 100 x 92 // max(days, 92))` keeps the rows per query where they
  were: 1,220 days reads 7 meters a query. The count is a cheap aggregate with no sort, and is
  summed over time slices.
- **An HTTP API** is asked for consecutive inclusive day spans (`day_slices`). `MAX_PAGES` applies
  to each span, so it still stops a runaway next-page chain without refusing a long window.

### D3 — Jobs are where a window of years belongs

A rolling window has no upper bound (`days >= 1`). An interactive run is still one request under
nginx's 600 s timeout (ADR-0047), and years of a fleet can take longer than that. A job runs in the
worker with no request waiting on it, so the Compose help text points long windows there.

## Consequences

- A pull of any length is accepted and returns the same rows, in the same order, as the old reads
  did. Tests pin this for 1,220 days on both connectors, check that each slice of a long window
  holds its order, and check that the API asks for 14 spans that never share a day.
- The query count grows linearly with the window. That is the accepted cost: each query is the
  same size as before, so the timeout behaves as it was measured.
- An interactive run over a very long window can still exceed nginx's 600 s. That limit was
  already recorded in ADR-0047 and is not changed here.
