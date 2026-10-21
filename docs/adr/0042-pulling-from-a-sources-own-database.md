# ADR-0042: Pulling from a source's own database

- **Status:** accepted
- **Date:** 2026-10-21
- **Related:** ADR-0041 (IT/OT data sources), ADR-0037 (a rule set is named, not pathed),
  ADR-0030 (the meter normalizer; "reading SensiPro directly would be a new port"),
  ADR-0023 D4 (flow dialects are data), ADR-0017 (the reader is never guessed)
- **Scope:** a new `connect/` package (the connector port, two connectors, deployment-declared
  connections), a third flow dialect (`cicflowmeter-csv`), the data-source register (a
  `connection` column, three routes), Compose (a Pull panel), compose (`extra_hosts`, a mounted
  `config/connections.yaml`, two password variables). **No contract changes**; a run's request
  is unchanged.

## Context

The iotShield prototype read its two sources straight from their databases, and the request
was to do the same here:

- **SensiPro (OT):** Postgres 16, `~/projects/sensipro`, host port 5433.
  - `ascendinghourlyreading`: 5.2 M hourly rows from Mar to Dec 2023, for 754 electricity and
    624 water meters.
  - 3,651 per-device `dst_*_atomic` tables holding 305 M raw readings.
- **Biology (IT):** MySQL 5.7, `~/projects/biology`, host port 3307. Its pipeline is tshark
  → cicflowmeter → `packets`.
  - `packets`: 40 flows, all BACnet/UDP 47808 except two DHCP requests.
  - `scans`: the same rows, scored by that project's classifier.

Both stacks log in as an administrator (`grafana`, `root`). We were asked to use those logins
rather than create read-only ones.

## Decisions

### D1 — A connector renders an existing format; normalizers stay text-to-events

A **connector** queries one known schema for a window and writes the rows as text in a format
an existing normalizer already reads:

| Connector | Reads | Writes | For the normalizer |
|---|---|---|---|
| `sensipro` | `ascendinghourlyreading` joined to `devices` | `sensipro-hourly-csv` | `meter` |
| `biology` | `packets` | `cicflowmeter-csv` (new) | `flow` |

The normalizer is never taught SQL. That keeps the property everything downstream relies on:
a normalizer is a pure function of text, testable from a string and the inverse of a renderer.
`meter.py` already drew this line in ADR-0030 ("adding a database reader would be a new port").
This is that port, kept as small as it can be: `fetch` plus an optional `devices` list.

What a connector reads, and what it leaves alone:

- **SensiPro: the hourly rollup, not the `dst_*_atomic` tables.** The meter detector judges
  hourly consumption, and the rollup is SensiPro's own. A meter is named by
  `devices.destination_address`, the address iotShield displayed and the atomic tables are
  named after.
- **Biology: `packets`, not `scans`.** `scans.is_attack` is another model's output, not ground
  truth: `ClassifyCommand` sets it to "score > 0.5", and `attack_type`/`attack_id` are empty in
  every row. iotshield judges the flows itself.

### D2 — `cicflowmeter-csv` is a dialect, as ADR-0023 D4 anticipated

cicflowmeter differs from the other two flow dialects in three declared ways, two of them new
fields on `Dialect`:

- **IANA protocol numbers** (`parse_protocol`): 17 becomes `udp`, so rules that say `flow.protocol: udp` match.
- **Microsecond durations** (`duration_unit_s`).
- **Naive UTC timestamps.** The capture host's clock is UTC, verified on the Biology server.

It is listed in `NOT_STORED`: the flow shape is already storable as a benchmark bundle, so a
third renderer would only cover a column mapping.

### D3 — A pull fills the source; it does not change the run

`POST /data-sources/{id}/pull {start, end, params}` returns the window as content, together
with its normalizer, format and an origin such as `SensiPro meters 2023-07-01..2023-07-01`.
Compose puts that into the source exactly as an uploaded file, so the wrong-format check,
the guard and `POST /runs` see no difference.

We rejected the alternative, a `PipelineSpec` source that names a data source and makes the
*run* query the database. It would have changed the run's schema, and it would make one run
two network-bound operations instead of one.

### D4 — Connections are declared on the server and named in requests

`config/connections.yaml` declares each connection (overridable with
`IOTSHIELD_CONNECTIONS_PATH`) with these fields:

- `connector`: which schema it holds
- `driver`: `postgresql` or `mysql`
- `host`, `port`, `database`, `user`
- `password_env`: the *name* of the variable holding the password
- `organizations`: who may use it, or `*` for everyone

A data source and a request name a connection; neither ever carries a host. This is ADR-0037's
stance applied to databases. If a request could name a host, any organization could aim the
server at any address it can reach and read the error back to learn what answered.

A connection that is undeclared and one reserved for another organization give the same 422,
so asking about a name reveals nothing. The file is read on each request, so editing it needs
no restart.

Passwords live in `.env`, which is git-ignored, and reach the container as environment
variables. A driver error is logged on the server; the caller sees only
`could not read from connection 'x'`, because a driver message can contain the host, the user
and the server's own diagnostics.

### D5 — Every session is read-only and bounded

Each session is opened read-only:

- **Postgres:** `default_transaction_read_only=on`
- **MySQL:** `SET SESSION TRANSACTION READ ONLY` on every new connection

With administrator logins, this is the only thing that keeps a connector bug from writing to
the source.

Every pull is also bounded:

| Limit | Value |
|---|---|
| Connect timeout | 5 s |
| Query timeout | 30 s |
| Longest window | 92 days |
| Most rows | 100,000 |
| Most characters | 4,000,000 (one source's limit) |

A pull over any limit is **refused with its size, never truncated**. A truncated pull would
look like a quiet meter. Measured on the real SensiPro data:

- one day of all water meters: 8,952 rows, 0.4 s
- one week of all electricity meters: 73,920 rows, 3.4 MB, served
- one month: refused at the row limit

### D6 — Anyone in the organization pulls; only admins connect a source

Pulling is a read, like listing the sources, so members can do it. Setting a source's
`connection` is a write, so only admins can. A connected source's normalizer and format are
its connector's: they are filled in, and a different one is refused, so a source cannot
promise a format its pulls never produce.

## Consequences

- **The IT/OT split for the charts comes from the data, not from tagging each run.** ADR-0041
  left "a run records its data source" for this phase. A pull is still just content, so no
  run records one. The charts filter on what the events are instead: the meter payload for OT
  and the flow lane for IT. That is what each type contains in practice, and it needs no
  change to how runs are stored. Revisit this if one organization has two sources of the same
  lane on different sides.
- **The data-sources table gained a column after Phase 1 shipped.** `create_all` does not alter
  an existing table, so a database created in between needs
  `ALTER TABLE data_sources ADD COLUMN connection varchar(64)`, or `down -v`. The development
  stack was altered in place.
- **Biology is tiny.** 40 flows over two days is a connector test more than a dataset. The
  capture pipeline writes to the same table if it is ever restarted.
  For presenting the IT charts, `scripts/biology_fake_attack.py` inserts a clearly marked,
  removable synthetic attack (60 flows from `192.168.0.66`) into that table; its test
  (`tests/test_biology_fake_attack.py`) pins that each step trips its detector.
- **One new runtime dependency:** `pymysql`. It is pure Python, so the image grows only
  slightly.
- **Docker access to the host.** The two source stacks are reached at `host.docker.internal`,
  which compose maps with `extra_hosts: host-gateway`.

## Amendment (2026-10-21)

The `sensipro` connector reads `count_accepted`/`count_rejected` and drops or marks SensiPro's
filler hours (ADR-0043 amendment). `Fetch` now returns `(lines, rows read)`, and the row ceiling
is checked against rows read.
