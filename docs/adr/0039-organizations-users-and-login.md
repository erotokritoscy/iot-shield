# ADR-0039: Organizations, users and login

- **Status:** accepted
- **Date:** 2026-10-13
- **Amends:** ADR-0007 Consequences ("auth is out of scope; the API must not be exposed beyond
  the compose network in this form"), ADR-0005 (an asset is keyed by `asset_id` alone)
- **Related:** ADR-0015 (two APIs, one writer plane), ADR-0018 (`SinkResources`), ADR-0021 D1 /
  ADR-0031 D5 (a plane that is *absent*, not guarded, and probed for by the dashboard)
- **Scope:** storage (three new tables, a tenant column on every data table, composite asset
  key), both APIs (a login gate, an `/auth` router), the sinks and the runner CLI (`--org`),
  the dashboard (login, register, account and users pages). **No contract changes**: nothing in
  `contracts/` and no adapter knows a tenant exists.

## Context

The dashboard was reachable by anyone who could reach port 8080, and both APIs by anyone who
could reach 8000/8100 — the Pipeline API with the power to write runs, unauthenticated. ADR-0007
accepted that for an MVP bound to loopback. The request now is a login page, a register page,
and a dashboard reachable only after login — and, clarified with the requester, that
**organizations** register, an organization's **admins** add its users, and **each organization
sees only its own data**.

## Decisions

### D1 — Login is enforced by the APIs, not only by the dashboard

A login page in front of an open API protects nothing: the browser's network tab shows the
`/api/alerts` call, and `curl` makes it without the page. So both FastAPI apps install one
**app-level** dependency (`api/_auth.py: authenticate`) that resolves the session cookie and
refuses with 401. App-level rather than per-route because the safe default is deny: a route
added next year is protected without anyone remembering to protect it. The exceptions are a
named set of route *templates* (`/health`, `/auth/login`, `/auth/register`, `/auth/logout`) —
templates, not URLs, so `//health` or a path-parameter trick cannot match one.

`tests/api/test_auth.py` holds this as a property: it enumerates every route of both apps and
asserts each non-public one answers 401 without a session.

### D2 — One flag, off by default, absent rather than guarded

`IOTSHIELD_AUTH` decides it, the same shape as `IOTSHIELD_EVAL_API`. **Off**, the product is
exactly what it was: one tenant (`DEFAULT_ORG_ID = "default"`), every request acting as its
admin — so host runs, tests and `npm run dev` need no account. **On** (compose sets it), the
`/auth` router is registered on the Pipeline API and the gate requires a session on both. With
it off the router does not exist, so the dashboard probes `/auth/me` and reads a 404 as "no
login here", a 401 as "log in", a 200 as the user — one server flag decides login end to end,
and the static bundle needs none of its own (ADR-0031 D5's argument, again).

### D3 — Sessions are opaque server-side rows in an HttpOnly, SameSite=Strict cookie

Not JWTs: logout, password change and removing a user must take effect *now*, and a stateless
token cannot be revoked without reintroducing the server-side state it avoided. The token is 32
random bytes; only its SHA-256 is stored, so a database read yields no usable session. The
cookie is `HttpOnly` (no script can read it — the SPA never sees a token, only whether it is
logged in) and `SameSite=Strict` (no cross-site request carries it), which is also the CSRF
stance. `Secure` is set when the browser is on HTTPS, read from `X-Forwarded-Proto` behind
nginx — not unconditionally, because this stack is served over plain `http://localhost`.
Sessions last 12 hours; an expired row is ignored when read and purged on the next login.

The read API **resolves** sessions but never writes them, so it stays write-free (ADR-0007).
That is why expiry is a filter on read plus a purge on login rather than a delete on read.

### D4 — Passwords: standard-library scrypt, self-describing

`hashlib.scrypt` with a per-hash salt, stored as `scrypt$n$r$p$salt$hash` so the work factor can
rise later without invalidating existing hashes. No new dependency — the runtime surface is
already the subject of an open hardening item (ADR-0030). A login for an unknown username is
verified against a dummy hash, so it costs the same and says the same as a wrong password.

### D5 — The tenant is a storage fact, stamped on the way in; contracts do not change

Every data table gains `org_id`; `assets` is keyed by `(org_id, asset_id)`, and events,
detections and alerts reference it through a **composite** foreign key, so a row can only name
a device of its own organization. Two organizations may each own `10.0.0.24`.

The alternative — a tenant field on the contracts — would have handed every adapter a value it
must carry and could forge or drop, for a concern no adapter has. Instead the pipeline computes
a run without knowing whose it is, and the tenant travels beside the result in
`SinkResources.org_id`: decided by **who asked** (the session, or the CLI's `--org`), never by
anything in the request body, in the same way a caller can never name the database (ADR-0015).

Every repository function takes `org_id` as a **required** keyword. Tenancy is enforced at the
one place SQL is written, and forgetting the filter is a `TypeError` rather than a query that
returns another organization's alerts and looks healthy doing it. Lookups by id are scoped too,
so another tenant's alert is a **404**, indistinguishable from an id that never existed — a 403
would confirm it exists.

The file sink writes under `<output_dir>/<org_id>/`. The evaluation plane is untouched: a
benchmark cannot write to a store (ADR-0021), so it has no tenant to scope; it is merely behind
the login like every other route.

### D6 — Two roles, and no route that changes one

An **admin** manages the organization's users; a **member** does everything else (reads, runs,
benchmarks). Registering creates the organization and its first admin. An admin adds a user with
a temporary password and a role, and can remove any user but themselves — which is what keeps
every organization at one admin or more, since there is no role-change route to break it.
Usernames are global and case-insensitive, so login needs no organization field. An
organization's id is a slug of its name (`Acme Water` → `acme-water`), which is what the CLI's
`--org` and the file sink's subdirectory use.

### D7 — An unreachable Pipeline API does not lock the dashboard

The `/auth` routes live on the Pipeline API, so the dashboard's guard cannot always ask. When it
cannot (the service is down), the guard lets the shell render: Monitor reads the read API, which
enforces login on its own, so if a session is needed the first read answers 401 and the
dashboard goes to the login page. Blocking instead would let a down write plane take the read
plane with it — the coupling ADR-0015 split the APIs to avoid. Any 401 from any request is
handled once (`http.ts` → `SessionExpiryBridge`), because a session can end while a page is open.
Signing in or out **clears the whole query cache**: queries are keyed by endpoint, not tenant,
so otherwise the next user of a browser would be served the previous organization's data.

## Consequences

- **The schema changed and there are no migrations** (ADR-0005's standing gap). An existing
  database must be recreated: `docker compose down -v`. The demo data is reloaded with
  `IOTSHIELD_DEMO_ORG=<org-id> docker compose run --rm runner`.
- The runner is a **trusted writer**: `--org` creates the organization if absent, with no admin.
  It has direct database access, so this grants nothing it did not already have.
- Ports 8000/8100 still publish to loopback for development, and are now behind the same login
  as the dashboard (the cookie is host-scoped, so a browser session works on them too).

### Open items, deliberately not done

- **No rate limiting or lockout** on `/auth/login` and `/auth/register`. scrypt slows guessing
  per attempt, but nothing caps attempts. Belongs in nginx (`limit_req`) or the app.
- **Open registration.** Anyone who can reach the dashboard can create an organization. That
  matches the request; a deployment that wants invite-only registration needs a flag.
- **No password reset, email verification or invitation links** — there is no mail
  infrastructure. An admin resets a member by removing and re-adding them.
- **No role changes or admin transfer.** An organization whose only admin leaves needs a
  database edit.
- **No audit log** of logins or user changes.

## Amendment (2026-10-13)

ADR-0040 adds a **super admin** above organizations (created from the server CLI, managing every
organization, seeing none of their data), closes the open items "open registration" (now a
switch, `IOTSHIELD_OPEN_REGISTRATION`) and "no role changes or admin transfer" (the super admin
can promote, demote and reset; an organization's last admin is protected), and adds suspension.
D6's "no route that changes a role" now holds for organization admins only.
