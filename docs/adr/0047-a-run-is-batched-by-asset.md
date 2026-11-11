# ADR-0047: A run is batched by asset, and a pull is read by the run

- **Status:** accepted
- **Date:** 2026-11-02
- **Related:** ADR-0042 (connectors — this removes its row ceiling, D5), ADR-0018 (result sinks —
  this changes the port's shape), ADR-0019 (multi-source runs), ADR-0046 (saved runs — a pull is
  now saved with the run), ADR-0033 (`MAX_SOURCE_CHARS`)
- **Scope:**
  - connectors: `ConnectorSpec.fetch` → `stream` + `count`; `pull_chunks`, `pull_summary`,
    `check_pull`; `MAX_PULL_ROWS` removed
  - the pipeline: `execute_run` is batched (`pipeline/batches.py`, `runner.Feed`); `compute_run`
    is unchanged
  - the sink port: `ResultSink.open(header) -> RunWriter` with `add` / `finish` / `abort`;
    `write_result` for a whole result
  - the Pipeline API: a run input is `content` **or** `pull`; `POST /data-sources/{id}/pull`
    returns `sample` + `rows`, not `content`; a saved run's source carries its `pull`
  - the dashboard: Compose sends the pull, never the rows; Open in Compose restores pulls

  **No contract changes.** `NormalizedEvent`, `DetectionResult`, `PrioritizedAlert` and the
  rest are untouched; the sink port is not in `contracts/`, but it is a port, so it is recorded
  here.

## Context

A pull from a data source was refused above 100,000 rows: "more than 100,000 rows in this
window; pick fewer days or devices". One month of SensiPro's electricity meters is past it, so
the ceiling blocked the ordinary question "what happened last month?".

The ceiling existed because of the route the rows took. A pull was fetched whole, **sent to
the browser**, and **posted back** with the run as `SourceInput.content` — so the browser held
it as a string, JSON-escaped it into a request under nginx's 8 MB body limit and the 4,000,000-
character `MAX_SOURCE_CHARS`, and the run then held the whole input plus every event it fanned
into (measured at ~41x the input, ADR-0033). Every one of those limits was protecting
something real. Removing the ceiling alone would have moved the failure from "refused with a
message" to "the browser tab locks" or "the pipeline-api container is killed for memory, failing
every other user's request with it".

So two things had to change: the rows must not go through the browser, and the run must not
need to hold them all at once.

## Decisions

### D1 — A pull is a reference; the run reads it

`POST /runs` takes, per source, **either** `content` (text the caller holds — an upload, a
paste; still capped at 4,000,000 characters, since it travels in the request) **or**
`pull: {source_id, start, end, params}`. For a pull the Pipeline API resolves the data source
(this organization's, its connection allowed for this organization), checks the spec reads it
with the connector's own normalizer and format, validates the window and params, and only then
streams the rows from the database during the run. Every pull is checked before any is read, so
a bad third source is refused before the first is streamed and before anything is stored.

`POST /data-sources/{id}/pull` no longer returns the window. It returns the **row count** and a
**sample** — the connector's header and the first 200 lines. The Compose page shows the count,
and checks the sample with the normalizer exactly as it checks an upload, so "wrong format?"
still appears beside the source. The sample is real connector output, so that check is honest.

A pull is a reference rather than data, so unlike an upload it **is saved with the run**
(`runs.sources[i].pull`), and Open in Compose restores it: those sources need nothing attached.

### D2 — Connectors stream, cut only where the normalizer allows

`ConnectorSpec.fetch` (all rows, with a limit) became `stream` (rows as they arrive, server-side
cursor, `yield_per=5000`) and `count`. Each streamed line carries a **group**: the run of lines a
chunk must not be cut inside, because the normalizer reads them together. A SensiPro meter is a
group — the meter normalizer differences each meter's counter row to row, so a meter split
across two chunks would lose the hour at the cut. A flow record stands alone (`None`). `pull_chunks`
cuts the stream into texts of about 20,000 lines, only where the group changes; each is
normalized on its own. A test holds that chunked normalization equals whole-text normalization.

SensiPro is streamed **one query per 100 meters**. The stream must be sorted by meter, and one
query sorted over a whole fleet and a quarter sorts millions of rows before the first arrives —
against the connection's 30 s `statement_timeout` (ADR-0042 D5). A group of meters is bounded by
construction, the groups follow the fleet's meter order, and each filters on the indexed
`device_id`.

### D3 — A run is judged a batch of assets at a time

`execute_run` reads each source chunk by chunk, normalizes it, and puts each event into one of
128 buckets by `crc32(asset_id)`. Buckets stay in memory until more than a batch (50,000 events)
is held, then spill to temp files. Then whole buckets are gathered into batches of about 50,000
events, and each batch is judged by `compute_result` — the same kernel `compute_run` uses — and
handed to the sinks.

**Why by asset, and why it changes nothing.** Every detector fans out per asset
(`detect/_group.py` is the port's routing law; the learning adapters keep one model per asset,
ADR-0034), and every alert bucket holds one asset (`alert/_correlate.py`). So judging A and B
together or apart produces the same detections and the same alerts. The one thing a batch must
never do is split one asset's events — that would cut a window, a burst or a baseline in two —
and whole buckets never do.

**Why the normalized `asset_id`, not the connector's device.** A flow's asset is its *oriented*
endpoint (ADR-0023), which can be the destination, so splitting a Biology pull by `src_ip` would
split an asset. Bucketing after normalization is right for every source, pulled or uploaded.

**A small run is the old run.** A run that never spills is one batch, in the order its events
were read — row for row what it was before batching existed. The demo's numbers (110 / 2 / 8 / 2)
are pinned through the batched path, and `test_batched_run.py` runs the demo at batch sizes 1, 7
and 1,000,000 and compares every event, reject, detection and alert with `compute_run`.

`compute_run` is unchanged: the evaluation harness and the tests want a `RunResult` they can
hold, and corpora are small by construction.

### D4 — The sink port takes a run in parts

`ResultSink.write(result)` became `ResultSink.open(header) -> RunWriter`, with `add(part)` per
batch and exactly one of `finish(totals)` / `abort()`. `RunHeader` is what is known before
anything is read (run id, time, sources, chain); `RunTotals` is what is known only at the end
(counts, warnings). `write_result(sink, result)` is the whole sequence for a complete result, and
both sinks keep a `write()` that calls it.

- **Postgres keeps one transaction per run.** Each part is flushed and then expunged from the
  session, so the process holds one batch while the database holds the uncommitted rest.
  `finish` saves the run's record with its totals and commits; `abort` rolls back. A run that
  fails in its last batch leaves nothing from its first — tested.
- **The file sink's line is unchanged.** Each array's items are appended to part files beside
  the target, and `finish` copies them through in order into the one JSON line, so the line is
  never in memory whole and is byte-identical to the old one for a one-batch run. `abort`
  deletes the parts.

Rejected rows belong to no asset, so each chunk's rejects are written straight away rather than
held. The report's top alerts are kept across batches with the engines' own ranking key.

### D5 — The 92-day window stays

The window bounds the *query* — how far back one pull scans — not the size of what comes back.
It was not what refused a month of meters, and dropping it is a separate question.

> **Superseded by ADR-0053 (2026-11-11).** There is no window limit now. A long window is read
> as more queries of the same size, so the query this decision protected stays bounded.

## Measured

A SensiPro-shaped window of hourly cumulative readings, through the meter normalizer,
`meter_quality` and the file sink:

| | events | peak RSS | time |
|---|---|---|---|
| whole in memory (the old path), 300 meters x 92 days | 662,100 | 1,629 MB | 20 s |
| batched, same input | 662,100 | **483 MB** | 29 s |
| batched, 900 meters x 92 days | 1,986,300 | **534 MB** | 92 s |

Three times the data cost 51 MB more, batched; the old path grows linearly (~4.9 GB at the
larger size). The time cost is the spill — JSON out and back for every event — paid only by a run
too large for one batch.

## Consequences

- A pull has **no row limit**. Memory is bounded by the batch (and by the busiest single asset,
  whose history a detector must see whole), not by the window.
- **A run is still synchronous** inside `POST /runs`, and nginx gives the Pipeline API 600 s. A
  very large window into Postgres — whose ORM inserts are the slowest stage — can exceed that.
  Running a run as a background job, with progress, is the next step if that happens; it is not
  done here.
- A detector's `why_silent` note is computed per batch, so a meter detector missing models for
  meters in two batches writes two notes. Identical notes are merged; different ones are kept.
- Across batches, the file sink's detections and alerts are ordered batch by batch rather than
  as one ranked list. The read API ranks by priority, so the dashboard is unaffected.
- An uploaded file keeps its 4,000,000-character and 8 MB limits, because it still travels in
  the request. A large file belongs in a data source or on the CLI.
- ADR-0042 D5's "Most rows: 100,000" and "Most characters" rows no longer apply to pulls.
