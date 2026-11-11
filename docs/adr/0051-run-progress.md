# ADR-0051: A run says how far it has got, and the Compose page draws it as a progress bar

- **Status:** accepted
- **Date:** 2026-11-10
- **Related:** ADR-0047 (a pull is read by the run, a batch at a time), ADR-0050 (jobs run in a
  worker), ADR-0039 (tenancy)
- **Scope:**
  - `pipeline/runner.py`: `execute_run(progress=...)`, `RunProgress`, `Feed.rows` /
    `Feed.header_lines`, `text_rows`
  - `connect/`: `pull_count`; `PullTarget.count` / `header_lines` (a database counts, an API
    cannot without reading)
  - `api/progress.py` (the in-memory `ProgressBoard`), `api/execution.py` (counts each pull,
    passes the callback), `api/control.py` (`RunRequest.progress_id`,
    `GET /runs/progress/{progress_id}`)
  - the dashboard: `pages/compose/RunProgress.tsx`, which replaces Review & run's spinner

  **No contract changes, no schema changes.**

## Context

`POST /runs` is one synchronous request. A quarter of every SensiPro electricity meter is about a
million rows and minutes of work, and all the page could show for it was "Running…" — no way to
tell a slow run from a stuck one, or how much was left.

## Decisions

### D1 — The run reports three phases, measured where they are already counted

`execute_run` takes an optional callback and calls it with a `RunProgress(phase, done, total)`:
**reading** after every chunk (rows, towards the sum of every source's `Feed.rows`, or `None` if
any source cannot say), **judging** after every batch (events, towards every event read), and
**saving** once before the sinks finish (one commit, no count). The callback only observes; it
runs on the run's thread, and an exception from it fails the run like any other, so a writer is
never left half-finished by a broken observer.

### D2 — A pull's total is its connector's count, taken before the read

The same `COUNT(*)` the pull preview shows (`pull_count`), so the bar has a denominator from the
first second. It counts rows *read*, before a connector drops its filler hours, so reading may end
short of it; the reported total is never less than what was done. A count that fails is `None`,
not an error — the read fails a moment later with the run's own 502. An API source is not counted
(that would be the read itself), so a run reading one shows a count without a total.

### D3 — The browser names the run, and polls a board in the API's memory

The page mints a `progress_id` and sends it with `POST /runs`; while the request is open it polls
`GET /runs/progress/{progress_id}` once a second. Minted by the client because the server's
`run_id` exists only once the run does, and the request does not answer until it is over. The
board is in memory because the Pipeline API is one process and nothing in it is a record: an
entry is forgotten two minutes after its run ends (the saved run is the durable account), and the
board holds at most 1,000. Several API processes would need a shared store; that is the recorded
limit.

The board is tenanted like a row: an entry is answered only to the organization whose run it is,
and an id that is another organization's is 404 — the same answer as "not started yet" and "long
finished", on purpose. An id already in use by a live run, or ever by another org, is a 409, so
nobody can attach a run to an id they did not mint in order to read it.

### D4 — One bar, a share per phase

Reading takes 2–60% of the bar, judging 60–95%, saving holds at 95%: reading a pull is most of a
long run's time, and a bar that reached 100% and then waited for a commit would read as stuck. A
404 is "Starting…" (the server is still checking the chain and counting), and a failed poll keeps
the last state rather than replacing the bar with an error — the bar is a view of the run, never
part of it.

## Consequences

- A long run is visibly alive, with rows, events and elapsed time.
- Leaving the page still drops the bar with the result (the run itself carries on and is saved,
  as before); coming back cannot re-attach to it, since the id lived in the page.
- The job worker does not report progress yet. `perform_run` already takes the callback, so
  Operate → Jobs can show the same bar by storing it beside the execution's heartbeat — left for
  a follow-up.
