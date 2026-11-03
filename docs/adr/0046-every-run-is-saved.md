# ADR-0046: Every run is saved

- **Status:** accepted
- **Date:** 2026-11-01
- **Related:** ADR-0045 (a run is addressable — this builds the `runs` table its D2 deferred),
  ADR-0018 (result sinks), ADR-0039 (organizations), ADR-0007 (the read API writes nothing)
- **Scope:**
  - storage: a `runs` table and `storage/runs.py`; deleting an organization deletes its runs
  - the sink port: `RunResult.spec`, `SinkResources.run_name` / `run_by`
  - the Pipeline API: `POST /runs` takes an optional `name`; `GET /runs`, `GET /runs/{id}`,
    `PATCH /runs/{id}` (rename), `DELETE /runs/{id}` (admins)
  - the CLI: `--name`; the seed names its demo run
  - the dashboard: a Runs page under Operate, the run's record on the Run page, a Run name
    field on Compose, and **Open in Compose** (`/compose?from=<run_id>`)

  **No contract changes.**

## Context

ADR-0045 made a run *addressable*: its rows carry a `run_id`, so its results can be read back
as a set. But nothing recorded the run itself, so the only way back to one was the result
tiles of the browser tab that ran it. We were asked that every composed run be saved under a
name and be reviewable later.

## Decisions

### D1 — The run's record is written by the Postgres sink, in the run's transaction

A `runs` row (name, who, when, the chain, the sources, the four counts, the warnings) is
inserted by `PostgresSink` before the run's rows, in the same transaction. So a listed run
always has its results and a failed write lists nothing. Writing it in the API instead would
miss the CLI and the seed, whose runs land in the same store and belong in the same list.

The **name** and **who ran it** reach the sink on `SinkResources`, beside `org_id`: like the
tenant, they are decided by who asked, not computed by the run. The **chain** rides on
`RunResult.spec`, because it *is* a fact about what was computed — and only the
`PipelineSpec` fields, since `compute_result` may be handed a `PipelineConfig` that also
carries the deployment's `database_url`.

### D2 — The chain is saved, the inputs are not

A run's inputs can be 8 MB per source and ten sources, and every line of them is already
stored, on an event's or a malformed record's `raw`. Saving them again would double the store
to make one button — re-run with the same bytes — slightly shorter. **Open in Compose**
therefore restores the chain exactly and asks for the data again, naming the files the run
read.

### D3 — A name is never missing

`name` is optional on `POST /runs`; blank or absent, the sink names the run after its inputs
and the time (`pumps.csv — 2026-11-01 14:03 UTC`). Inputs first, because that is what tells
two runs apart in a list. The CLI defaults to `<config file> (CLI)`.

### D4 — Members rename, admins delete, and deleting takes the results

Anyone in the organization may list, open and rename its runs; only its admins delete one —
the data-sources split. Deleting a run deletes every event, malformed record, detection and
alert it stored: a result whose run is gone would sit in Triage with nothing to say where it
came from, which is what this record exists to end. Assets stay; a device outlives the runs
that saw it.

### D5 — No foreign key from the data rows to `runs`

The data rows' `run_id` predates this table (ADR-0045), and `create_all` adds a missing table
but never alters an existing one. A constraint would therefore exist on a fresh database and
not on an upgraded one — two schemas under one version. `delete_run` deletes a run's rows
explicitly instead. The upside is that this change needs **no `down -v`**: the init step
creates the new table beside the existing data.

### D6 — The routes live on the Pipeline API

Renaming and deleting write, and ADR-0007 keeps the read API write-free; the list and the
record sit beside them and beside `POST /runs`, which creates them. A run's *results* are still
read from the read API, by `run_id`.

## Consequences

- Runs made before this change have rows but no record: the Run page says so and still shows
  their results; the Runs list does not show them.
- A reopened chain that names an adapter the server no longer ships is restored as-is, and the
  chain check reports it — the honest answer to "this run used something that is gone".
- **Open:** runs cannot be searched or filtered yet; the list shows the latest 200 and says
  when there are more.
