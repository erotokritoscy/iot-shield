# The read API

The read API serves what a run produced. It is the only thing the dashboard talks to for
data, and it cannot write.

Read `docs/guide/architecture.md` first, and `docs/guide/pipeline-api.md` for the other
service. This guide is written from the outside in.

## Read-only by construction

There is no write path in this service. Not a disabled one, not a guarded one: the module
imports `select` from SQLAlchemy and no insert, and every route is a `GET`. The pipeline is
the only writer.

That is worth being precise about, because it is what makes the split useful. A slow or
crashing run cannot take the triage queue offline, the two scale independently, and you can
point as many read replicas at the store as you like without thinking about consistency.

The API also never creates the schema. `python -m iotshield.storage.init` owns that as a
one-shot step (in compose, everything else `depends_on` it completing), and the CLI creates
it for standalone runs. `create_app` assumes the tables exist.

Serve it with:

```
IOTSHIELD_DATABASE_URL=sqlite:///demo.db uv run python -m iotshield.api
```

Host and port come from `IOTSHIELD_API_HOST` (default `127.0.0.1`) and `IOTSHIELD_API_PORT`
(default `8000`). It prints the resolved database location on startup, absolute for SQLite,
so a working-directory mistake pointing at an empty file is visible instead of looking like a
quiet night.

There is deliberately **no CORS**. The dashboard's nginx proxies to this service
server-to-server, so no browser cross-origin request ever happens in this architecture and
adding CORS headers would only widen what can reach it.

## The endpoints

| | Returns | Filters |
|---|---|---|
| `GET /health` | status and version, after a real `SELECT 1` | |
| `GET /assets` | the inventory, id-ordered, unpaged | |
| `GET /events` | normalized events, oldest-first | `asset_id`, `lane`, `start`, `end` |
| `GET /detections` | detections, oldest-first | `asset_id`, `lane`, `start`, `end` |
| `GET /alerts` | the ranked queue, priority-descending | `status`, `asset_id`, `start`, `end` |
| `GET /alerts/{id}` | one alert, 404 if unknown | |
| `GET /alerts/{id}/detections` | the evidence behind one alert | |
| `GET /malformed` | inputs that failed normalization | |
| `GET /summary` | per-table counts plus the top alert | |

Every list endpoint takes `limit` (1 to 10,000, default 1000) and `offset`.

`/health` runs an actual query rather than returning a constant, so it answers "can this
process serve requests?" rather than "is this process alive?". A container whose database
went away fails its healthcheck instead of serving 500s.

## The contracts are the wire format

The endpoints return contract objects. FastAPI serializes them. There is no serialization
layer, no DTOs, no mapping code:

```python
@app.get("/alerts")
def alerts(...) -> list[PrioritizedAlert]:
    return _paged(response, repository.get_alerts)(session, ...)
```

So the OpenAPI schema *is* the contract schema, and it cannot drift from it, because there is
nothing in between that could drift. Adding a field to `PrioritizedAlert` changes the API in
the same commit, necessarily.

The dashboard exploits this twice over:

1. `npm run gen:types` dumps both OpenAPI documents and generates TypeScript from them. The
   output is committed, so a clone builds without Python. A server-side contract change that
   the client has not been taught about is a **compile** error.
2. `web/src/lib/api/schemas.ts` validates every response with zod at runtime, and asserts at
   compile time that each schema matches the generated server type. So a *deployed* API that
   drifted from what the bundle was built against fails loudly at one boundary instead of
   rendering something wrong.

Both halves exist because types are erased at runtime and cannot do the second job. If you
change either API, run `npm run gen:types` in `web/`.

## Three orderings, on purpose

This trips people up, so it is worth stating plainly.

**Events, detections and malformed records come back oldest-first**, because they are
timelines and a chart reads left to right.

**Alerts come back priority-descending**, because the ranked queue *is* the product. The page
order is the ranking. Live, against a store holding both runs of the demo walkthrough:

```
critical    81.5  10.0.0.24   Remote Services (T1021)
high        55.7  10.0.0.24   Remote Services (T1021)
medium      39.6  10.0.0.11   External Remote Services (T1133)
medium      39.6  10.0.0.11   External Remote Services (T1133)
```

**Assets come back id-ordered** and unpaged, because an inventory is a lookup table.

Every ordering is **total**: each one ends in a unique id (`event_id`, `detection_id`,
`alert_id`). That is not decoration. An ordering with ties lets a row move between pages as
you walk them, so paging can silently skip or repeat rows. Ending on a unique key makes that
impossible.

## Paging, and the part that surprises people

The timeline endpoints query **descending** and then reverse the page:

```python
return list(session.scalars(stmt.limit(limit).offset(offset)))[::-1]
```

Descending because a cap must drop the *oldest* rows, not the newest: an ascending query with
a limit would hide recent data, which for a monitoring product is the wrong half to lose.
Reversed because the caller wants a timeline.

The consequence is that **`offset` pages backwards in time**. Page 0 is the newest `limit`
rows; page 1 is the `limit` before those. Verified:

```
lane=log, limit=2            -> ['2026-07-30T12:10:00Z', '2026-07-30T12:12:00Z']
lane=log, limit=2, offset=2  -> ['2026-07-30T12:08:02Z', '2026-07-30T12:08:40Z']
```

Rows inside a page ascend; pages themselves walk into the past. Once you know it, it is what
you want for "show me the most recent activity". If you expected forward paging, you would
have got the oldest rows in the table.

Alerts are **not** reversed, for the same reason they are ordered by priority: reversing a
ranked queue puts the least urgent alert on top.

### `X-Total-Count`

Every paged response carries a header saying how many rows matched the filters, before
`limit` and `offset`:

```
GET /alerts?limit=5                        X-Total-Count: 4      -> 4 items
GET /alerts?asset_id=10.0.0.24&limit=3     X-Total-Count: 2      -> 2 items
GET /detections?lane=flow&limit=3          X-Total-Count: 6      -> 3 items
GET /events?lane=telemetry&limit=3         X-Total-Count: 90     -> 3 items
```

A list alone cannot say whether it is complete, and for a monitoring product that is the
worst kind of wrong answer: a client draws conclusions from a truncated queue with no way to
know it was truncated. Worse here specifically, because these queries order descending and
then reverse, so the rows dropped are the **oldest** ones, which means the front of the
returned list was silently wrong.

It is a header rather than an envelope so the response *body* stays exactly the contract
objects. Wrapping lists in `{items, total}` would break the generated types and every zod
schema for one number.

The header is set on every paged route by one wrapper, `_paged`, rather than per endpoint, so
no route can serve a page and forget to say how much it left behind.

## Time bounds: one is closed and the rest are half-open

Time ranges are half-open, `start <= t < end`, with exactly one exception: **`/events`' upper
bound is closed**, `t <= end`.

The reason is a specific query the product depends on. Recovering the evidence behind a
detection means asking for events in `(asset, window_start..window_end)`. A point anomaly is
a zero-length window, `start == end`. With `< end` that recovers nothing at all.

You can watch the difference in one pair of calls:

```
GET /events?start=T&end=T      -> 1 item    (closed)
GET /detections?start=T&end=T  -> 0 items   (half-open)
```

Both are right for what they do. The rule is stated on each repository function rather than
once at the top of the module, because a blanket claim is exactly what sent a reader to the
wrong bound.

Detections and alerts match time on **`window_start`**, not on overlap. An alert spanning
12:00 to 12:08 does not appear in a query for 12:05 to 12:10. Interval-overlap querying is
not built.

## Timestamps at the boundary

Query datetimes go through the contracts' own rule, not a weaker parallel one. Naive input is
refused:

```
GET /events?start=2026-07-07T12:00:00
422  Value error, timestamp must be timezone-aware (naive datetimes are rejected)
```

An aware non-UTC offset is accepted and normalized, so `14:00:00+02:00` and `12:00:00Z`
select the same rows.

This matters more than it looks. A naive timestamp means "some local time" and the server has
no way to know which, so accepting it would silently answer a different question than the
caller asked. The same policy applies at every boundary in the system: the contracts, the
storage layer's `UtcDateTime` column type, and here.

`limit` is bounded at both ends:

```
limit=0      422  Input should be greater than or equal to 1
limit=10001  422  Input should be less than or equal to 10000
```

Generous for an MVP, but never unbounded.

## Drill-down

The one relationship the API exposes is alert to detections, which is the path an analyst
actually walks:

```
GET /alerts/{id}/detections
```

```
flow      rules:flow      score 2.55  vs 1
telemetry stub_threshold  score 84.2  vs 80
log       rules:log       score 2     vs 1
telemetry stub_threshold  score 91.6  vs 80
telemetry stub_threshold  score 86    vs 80
log       rules:log       score 1.7   vs 1
flow      rules:flow      score 1.7   vs 1
```

That is the corroboration story from `docs/guide/alerts.md` made concrete: one alert, seven
detections, three lanes. The alert's `detection_ids` are the lineage and this endpoint
resolves them. Note `method` repeats and the scores do not, which is why both dashboards label
a row by its `evidence.explanation` rather than by `method`.

It 404s properly through the alert rather than returning an empty list for an unknown id:

```
GET /alerts/00000000-0000-0000-0000-000000000000   404  {"detail": "alert not found"}
GET /alerts/not-a-uuid                             422  uuid_parsing
```

Alert lineage is eager-loaded with `selectinload` in one batched query. Without it, reading
`detection_ids` off each row in a page of 1,000 alerts is 1,000 extra queries.

## `/summary`

The dashboard's polled endpoint: per-table counts plus the current top alert.

```json
{"counts": {"assets": 3, "events": 122, "malformed_records": 2,
            "detections": 11, "alerts": 4},
 "top_alert": {"asset_id": "10.0.0.24", "severity": "critical", "priority_score": 81.5, ...}}
```

All five counts come back in **one** round trip, via scalar subqueries, because this is the
endpoint something polls and five round-trips per poll is the one place they would add up.

`counts` are global row counts. They are not comparable with a filtered list's
`X-Total-Count`, and treating them as one is a real trap: `counts.events` is every event in
the store, while `/events?asset_id=X` is one asset's, so any asset with fewer events than the
limit would falsely look truncated.

## What the store does and does not do

Three properties that shape what you can ask it.

**It accumulates across runs.** There is no run identity in the read model. The walkthrough's
two runs into one store leave **four** alerts rather than two: the gateway is judged
identically by both, so its `39.6` appears twice, and the camera's two verdicts sit side by
side at `81.5` and `55.7` with nothing in the model saying they describe one device twice.
Cross-run correlation and per-run views are not built.

**Assets arrive two ways.** Registered assets come from a config's `assets:` block;
everything else is auto-discovered from the data as a stub row typed `"unknown"` at MEDIUM
criticality. You can tell them apart:

```
('10.0.0.11', 'medium', auto_discovered=True)
('10.0.0.24', 'high',   auto_discovered=False)
('10.0.0.31', 'medium', auto_discovered=True)
```

The third row is the sensor that behaved: it produced telemetry, raised nothing, and is in the
inventory anyway, because discovery follows the data rather than the findings.

Registration always wins: a discovered row is only ever inserted where nothing exists, and an
existing row is never overwritten by discovery.

**`AlertStatus` has no mutation endpoint.** The contract models a triage workflow (`new`,
`acknowledged`, `closed`) and `/alerts?status=` filters on it, but nothing in this service can
change a status, because this service does not write. Every alert in the store is `new`. An
acknowledge/close endpoint is the obvious next thing to build, and it belongs here rather
than in the Pipeline API, which would mean this service stops being read-only. That is a
deliberate architectural decision to make, not a line to add quietly.

## Underneath: the repository rule

One rule governs everything below the routes: **ORM rows never escape
`storage/repository.py`.** Callers pass a `Session` and speak contract objects exclusively.
Every table carries `from_contract` / `to_contract`, and `tests/storage/test_roundtrip.py`
pins that the projection is lossless.

Two consequences worth knowing:

- Repository functions **flush but never commit.** Transaction boundaries belong to the
  caller, which is what lets the Postgres sink wrap an entire run in one transaction.
- Enums are stored as their wire-format strings. The contracts police the vocabulary, not the
  database, so adding a severity is a contract change and not a migration.

Six tables: `assets`, `events`, `malformed_records`, `detections`, `alerts`, and the
`alert_detections` junction. JSON columns are plain `JSON` on SQLite and indexable `JSONB` on
Postgres, from one annotation.

Five indexes, each named after the query it serves:

| Index | Serves |
|---|---|
| `ix_events_asset_ts` | a per-asset timeline |
| `ix_events_lane_ts` | a lane feed |
| `ix_detections_asset_ws` | correlation and alert drill-down |
| `ix_alerts_status_priority` | the triage queue |
| `ix_alerts_created_at` | recency |

Two engine settings are load-bearing rather than tuning. `pool_pre_ping` because both
services hold one engine for the process lifetime and would otherwise serve
`OperationalError`s after a database restart until the pool recycled. And a JSON serializer
with `allow_nan=False`, so a hand-built row cannot put the non-JSON tokens `NaN` or `Infinity`
into a JSON column, which SQLite accepts silently and Postgres rejects mid-transaction.

## Extending it

**Adding a filter** is two edits: the query in `repository.py` and the parameter on the route.
Keep them the same name. Mirror an existing one; `get_alerts`' `asset_id` and time bounds were
added exactly this way, and the note on that function says why (asking "which alerts fired on
this device last week" previously meant pulling the whole queue and filtering client-side).

**Adding an endpoint** means a repository function returning contract objects and a route
returning them with a type annotation. Return a `Page` and wrap the call in `_paged` if it is
a list, so the total header comes for free. Then regenerate the TypeScript client.

**Do not add a write path here.** If the product needs one, that is a decision about the
architecture, not a route to append. See the `AlertStatus` note above.

**Do not let ORM rows out of the repository.** Every layer above it speaks contracts, which
is what makes the store swappable and the API schema automatic.

## Where to look

| | |
|---|---|
| The service and its routes | `src/iotshield/api/app.py` |
| Serving, env vars | `src/iotshield/api/__main__.py` |
| Every query, and the bound rules | `src/iotshield/storage/repository.py` |
| Tables, indexes, contract projection | `src/iotshield/storage/orm.py` |
| Engine and session plumbing | `src/iotshield/storage/db.py` |
| The one-shot schema step | `src/iotshield/storage/init.py` |
| Generated TypeScript views | `web/src/lib/api/read.gen.ts` |
| Runtime response validation | `web/src/lib/api/schemas.ts` |
| Truncation handling in the client | `web/src/lib/api/client.ts` |
| Contracts as the wire format | `docs/adr/0003-data-contracts-v1.md` |
| The storage layer and discovery | `docs/adr/0005-storage-layer.md` |
| Read-only v1, one writer | `docs/adr/0007-read-side-api.md` |
| Why the writer is a separate service | `docs/adr/0015-pipeline-api.md` |
| Paging, totals, and the closed bound | `docs/adr/0033-pre-handover-review-fixes.md` |
