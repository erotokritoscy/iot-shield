# ADR-0052: A username is an email address, and a job emails its outcome

- **Status:** accepted
- **Date:** 2026-11-11
- **Related:** ADR-0039 (organizations, users and login), ADR-0040 (the super admin), ADR-0050
  (saved, scheduled jobs and the worker)
- **Scope:**
  - `storage/accounts.py`: `validate_email`, `validate_username` (now: an address, ≤ 64)
  - `auth/superadmin.py`: `create` requires an address; `set-password` only normalizes
  - `storage/orm.py`: `JobNotificationRow` (new table `job_notifications`); `storage/jobs.py`:
    `Job.notify_emails`, written by `add_job` / `update_job`, dropped with the job or the org
  - `api/jobs.py`: `JobRequest.notify_emails` / `JobInfo.notify_emails`
  - `notify/` (new): `mailer.py` (SMTP from the stdlib, configured from env),
    `report.py` (the message; renamed from `job_report.py` by the amendment), `outbox.py`
    (amendment)
  - `jobs/worker.py`: `Worker.mailer`, `Worker.dashboard_url`, `Worker.notify`
  - compose: a `mailhog` service; the worker's `IOTSHIELD_SMTP_*`, `IOTSHIELD_MAIL_FROM`,
    `IOTSHIELD_DASHBOARD_URL`
  - the dashboard: `lib/email.ts`; the Register, Users and Organizations forms; the job form's
    *Email results to*

  **No contract changes. One new table, no new column — no `docker compose down -v`.**

## Context

A saved job (ADR-0050) runs at 02:00 in the worker and its outcome sits on the Jobs page until
someone looks. A nightly job that started failing a week ago — a rotated API key, a database
moved — is found only by visiting the page. The obvious channel is email, and two things were
missing for it: somewhere to send mail, and an address to send it to. A username was any 3–64
characters of `a-z0-9._@-`, so `alice` was a valid account with no address behind it.

## Decisions

### D1 — A username must be an email address, wherever an account is created

`validate_username` already guarded every door that creates an account — public registration,
an admin adding a user, the super admin creating an organization or a user, and the
`superadmin create` CLI — so changing its rule changed all of them at once. The rule is
`validate_email` held to the 64-character column: a practical address (a local part of
`a-z0-9._%+-` not starting or ending with a dot, an `@`, a dotted domain with a letters-only
TLD, no `..`), lowercased. Deliberately not RFC 5322: quoted local parts and IP-literal domains
are legal and are not anyone's login.

**Login is untouched.** It compares against what is stored, so an account made before this rule
(`root`) still signs in, and `superadmin set-password` only normalizes for the same reason. Such
an account simply has no default address (D3). There is no migration that renames them: the
only correct new name is the person's real address, which the database does not know.

The dashboard mirrors the rule (`lib/email.ts`) so the form says why before the round trip; the
server stays the authority. The username field is labelled *Email* and is `type="email"`.

### D2 — Recipients are per job, in their own table

A job names who is emailed (`notify_emails`, up to 20, each checked by `validate_email` and
de-duplicated). They live in `job_notifications` (one row per job that has any) rather than a
column on `jobs`, for the reason `data_source_endpoints` is a table (ADR-0049): `create_all`
adds a missing table to an existing database but never a column to an existing table, so a
column would have needed `down -v`. Deleting the job or the organization deletes the row.

On **create**, an omitted list means the creator, when their username is an address — so the
common case ("tell me") needs no typing, and the form shows that address prefilled so it is
visible rather than implied. `[]` means nobody. On **edit**, omitted means unchanged, so an old
client (or the Jobs page's enable/disable, which resends the job) cannot silently clear them.

### D3 — The worker sends it, after the execution is recorded, and a failure to send changes nothing

`Worker.run_one` records the execution first, then calls `notify`. The run is already stored and
the execution already says succeeded or failed; a mail server being down is logged and does not
fail, retry or re-run anything. One message per execution, success **and** failure — the failing
nightly job is the case this exists for.

The message reads what was stored rather than what the run returned: the saved run's four counts
and the first five alerts of that run in triage order (`get_alerts(run_id=...)`), plus a link to
`/runs/<id>` on `IOTSHIELD_DASHBOARD_URL`. It never carries events or raw lines: the data stays
behind the login. Plain text with an HTML alternative of the same content, every quoted value
escaped (an asset id is attacker-influenced text).

### D4 — SMTP from the stdlib; MailHog in compose; unset means off

`notify/mailer.py` is `smtplib` with optional STARTTLS and login — no new dependency. It is
configured only from env: `IOTSHIELD_SMTP_HOST` (unset or empty = **no mail**, which is what host
runs and tests get), `_PORT`, `IOTSHIELD_MAIL_FROM`, `_USERNAME`, `_PASSWORD`, `_STARTTLS`.

Compose runs `mailhog/mailhog`, which accepts every message and delivers none: SMTP on `:1025`
inside the compose network only, its web UI on `127.0.0.1:8025` like every other published
port. So a developer sees exactly what would have been sent without any real mailbox receiving
it. Pointing the same variables at a real relay is the whole of going live.

## Consequences

- A new account can no longer be `alice`; the seed's `admin@admin.com` already complied. Tests
  use `@example.com` addresses.
- Whether an email was sent is in the worker's log only, not on the execution. Showing it on the
  Jobs page would need a column on `job_executions` (a `down -v`) or a table of its own.
- Sending is synchronous inside the worker, with a 10 s socket timeout: a hanging relay delays
  the next queued job by at most that.
- Open: no email verification (an address is trusted as typed), no per-recipient opt-out, no
  "only on failure" setting. (Runs started from the Compose page: see the amendment.)
- ADR-0039's open item "no password reset, email verification or invitation links — there is no
  mail infrastructure" now has the infrastructure; the features are still not built.

## Amendment (2026-11-11): every run started from Compose emails whoever ran it

**Why.** D3 mailed only a job's executions, so the most common way the product is used —
compose a chain, press *Run* — sent nothing, and an operator who expected a mail after every run
found an empty MailHog. A run is one person's request, so the recipient is not a list to manage:
it is the person who pressed *Run*.

**What.**

- `RunRequest.email_result: bool = true` (`POST /runs`). On, the outcome goes to the session's
  user **when their username is an address** (D1's legacy accounts get nothing, as with D2's
  default) and the server has mail configured. The Compose page sends it from an *Email me the
  result* checkbox, ticked by default, with a caption naming the address — or saying that the
  username is not one, so nothing will arrive. The default is on in the API too, so a script
  that never heard of the field is mailed like the page; `false` is the opt-out.
- **What is mailed.** A run that **finished** — the stored run's counts, its top five alerts and
  the link, read back from storage exactly as the worker does (`notify.stored_run`, now shared by
  both) after the run's transaction has committed; with no Postgres sink there is no `runs` row,
  so it says what the run itself returned. A run that **failed while running** — any 5xx: a
  pulled source's database went away (502), a sink failed (500) — with the response's message. A
  **refused** chain (4xx: the guard, a bad param) is not mailed: nothing ran, and the page that
  asked is showing why.
- **Off the request path.** A run is one synchronous request, and a relay at the mailer's 10 s
  timeout must not hold up a response that already has its answer. `api/run_email.py` therefore
  posts a closure to `notify.Outbox` — one background thread, in order — which builds the message
  (reading the stored run) and sends it. A failure to read or to send is logged and changes
  nothing, as in D3. FastAPI's `BackgroundTasks` was not used because it does not run when the
  route raises, and a failed run is one of the two outcomes mailed.
- One composer for both: `notify/report.py`'s `Outcome(kind="job" | "run", name, ...)` and
  `compose_email`. A run's subject is `[iotshield] Run '<name>' finished: N alerts` / `failed`,
  and its footer says why the reader got it.
- Compose: the mail variables are one `x-mail-env` anchor shared by `worker` and
  `pipeline-api`, and `pipeline-api` depends on `mailhog`.

**Consequences.** The API process now holds one mail thread; an outbox's queue is in memory, so
an API restart in the seconds between a run and its send loses that one email (the run is
stored). CLI and seed runs are not mailed — no person started them. Still open: a per-user
default for the checkbox, and an email for a run's other readers.
