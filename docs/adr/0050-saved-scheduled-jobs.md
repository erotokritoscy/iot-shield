# ADR-0050: A chain of pulls can be saved as a job, run again or on a schedule, by a worker

- **Status:** accepted
- **Date:** 2026-11-09
- **Related:** ADR-0046 (every run is saved), ADR-0047 (a pull is read by the run), ADR-0049
  (HTTP API sources), ADR-0039 (tenancy), ADR-0015 (the Pipeline API is the write plane)
- **Scope:**
  - `jobs/`: `model.py` (fixed and rolling windows, the schedule), `run.py` (check a job, run
    one execution), `worker.py` (the compose `worker` service)
  - `api/execution.py`: what `POST /runs` did inline — checking pulls, streaming them, mapping
    a failed run to a status — moved so a job runs through the same code; `pull_target` takes
    the tenant rather than an account
  - storage: two new tables, `jobs` and `job_executions` (`storage/jobs.py`); deleting an
    organization deletes both
  - the Pipeline API: `/jobs` (list, create, read, replace, delete, `POST /jobs/{id}/run`,
    `GET /jobs/{id}/executions`)
  - the dashboard: *Save as a job* on Compose's Validate & run step, `?job=` to edit one there,
    and Operate → Jobs
  - deployment: a `worker` service; the run environment the Pipeline API and the worker share is
    one YAML anchor (`x-run-env`)

  **No contract changes.**

## Context

A run was something you clicked once. ADR-0046 made it listable and ADR-0047 made a pull a
*reference* the server reads, so a saved run could already be reopened and run again by hand —
but nothing could run it *without* a hand on the button, and reading a source every night meant
someone opening Compose every night. The request was to make the validate-and-run step produce
**jobs**: chains saved to run again on demand or on a schedule.

## Decisions

### D1 — A job's inputs are pulls, and each pull's window is a rule

An upload is not kept (ADR-0046), so a job made of one would have nothing to read next time; a
chain with any non-pull source cannot be saved as a job, and the dashboard says which source is
in the way. Each pull carries a **window rule**: `fixed` (the composed days, re-read each run)
or `rolling` (the last N days ending yesterday or today), resolved in **UTC** when the run
starts. A scheduled job with a fixed window is allowed — re-running one week is a real use — and
the form warns that it re-reads the same days.

### D2 — Three schedule shapes in UTC, not cron

Hourly at a minute, daily at a time, weekly on a day at a time. They cover every request so far,
need no parser or library, and cannot be mistyped into "every minute". `Schedule.next_after` is
strictly after the given moment and ignores seconds, so a tick at 02:00:20 does not fire the
02:00 slot twice. The dashboard shows the viewer's local time beside the UTC one.

### D3 — A worker service runs jobs; the API only queues them

Runs execute in a new compose `worker` (same image, `python -m iotshield.jobs.worker`), not in
the API process: a job survives an API restart, a long job holds no request open, and the
proxy's 600 s limit does not apply. "Run now" is `POST /jobs/{id}/run`, which **queues** an
execution (202) for the worker. Each tick of the worker (every 15 s):

1. fails every `running` execution whose **heartbeat** (beaten every 30 s from a side thread) is
   older than 5 minutes — its worker died, and its run's single transaction was never committed,
   so nothing it read was stored;
2. queues every enabled job whose `next_run_at` has come and moves `next_run_at` to the next slot
   after *now* — a worker down for three days runs a missed job **once**, not 72 times;
3. claims the oldest queued execution and runs it to completion, then repeats.

A job never runs twice at once: a slot that comes while it is queued or running is **skipped**
(not stacked), and "Run now" answers 409. Rows are claimed with `FOR UPDATE SKIP LOCKED`, so a
second worker is safe on Postgres; SQLite has no row locks and needs none with one worker.

### D4 — A job is checked and run by the code a clicked run uses

`api/execution.py` now holds what `POST /runs` did inline: every pull checked before any is read
(the source is this organization's, its reader, format and lane match, the window is valid),
then streamed, then each failure mapped to the status and words the Compose page shows. Saving
a job runs the compatibility guard and those pull checks **without reading anything**, with every
rolling window resolved as of now, so a job that could never run is refused at save (422 with
the run's own message) rather than failing every night. The worker runs the same checks again,
since a data source may have been deleted or its key rotated since; such an execution fails with
the message the route would have returned, and the job's history shows it.

The worker raises nothing to a browser, so it reads the route's `HTTPException` detail back as
one line (`failure_message`): one wording, two readers. A job's run is an ordinary saved run —
named `<job> · <time> UTC`, credited to whoever pressed Run now or else the job's author — so
the Runs page, Open in Compose and the result links need nothing new. A suspended organization's
jobs do not run (their executions fail saying so).

### D5 — Tenancy and roles follow runs

Both tables carry `org_id` and every function a request reaches takes it as a required keyword;
another organization's job is a 404 and its data source is "not found" when named in a job. The
worker's functions act across organizations by design and hand each execution its own `org_id`.
Members list, create, edit and run jobs; admins delete them (deleting a job keeps the runs it
made, as with runs and their rows). Deleting an organization deletes its jobs and history.

## Consequences

- An organization can read its data sources on a schedule with no one at the dashboard, and the
  results arrive as ordinary saved runs.
- Two new tables, so `create_all` adds them to an existing database: **no `down -v` needed**.
- One more long-running service. Without it jobs queue and wait; the API still answers.
- The Pipeline API and the worker must agree about the shelves, the connection passwords and
  `IOTSHIELD_SECRET_KEY`, so compose gives both one anchor rather than two lists.
- The Jobs page and a job's history **poll** (every 5 s) while something is queued or running —
  the first polling in a dashboard that otherwise refreshes only when asked.
- Open: a job with uploaded inputs (would need storing the upload), notifying anyone when a job
  fails (e-mail, webhook), cron expressions, retries, and a time zone other than UTC for the
  schedule.
