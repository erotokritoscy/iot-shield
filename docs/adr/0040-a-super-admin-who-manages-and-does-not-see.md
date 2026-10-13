# ADR-0040: A super admin who manages organizations and does not see them

- **Status:** accepted
- **Date:** 2026-10-13
- **Amends:** ADR-0039 D6 (two roles; no route changes a role), ADR-0039 open items ("open
  registration", "no role changes or admin transfer")
- **Related:** ADR-0039 (organizations, users, login), ADR-0021 D1 (absent, not guarded)
- **Scope:** one role, one column, one flag, one router, one CLI, two dashboard pages. No
  contract changes; no change to how a run or a read is scoped.

## Context

ADR-0039 left every organization self-governing: it registered itself, its admins added its
users, and nobody could see across organizations — including to fix one. Its open items named
the gaps that follow: an organization whose only admin leaves needs a database edit, roles
cannot change, and registration is open to anyone who can reach the dashboard. The request is a
**super admin** and a page to manage organizations. Clarified with the requester: the super admin
manages organizations **only** and never sees their data; they can create an organization with
its first admin, manage any organization's users, suspend and reactivate one, and delete one with
all its data; public registration stays open behind a switch.

## Decisions

### D1 — A super admin is a user with no organization, and the table enforces it

`users.org_id` becomes nullable and `role` gains `superadmin`, under a check constraint:
`role = 'superadmin'` **if and only if** `org_id IS NULL`. So a super admin with an organization,
or an organization user without one, cannot be stored at all. A separate `platform_admins` table
was the alternative; it would have needed its own login path, sessions and password handling
for what is otherwise an ordinary user. Usernames stay globally unique, so one login form serves
both.

`Account.organization` is `None` exactly for the super admin, and `Account.org_id` raises rather
than invent a tenant: any code path that asks a super admin for their tenant is a bug, and it
should fail loudly.

### D2 — "Management only" is enforced where the tenant is handed out

Every data route already depends on `AccountDep` to learn whose data to read (ADR-0039 D5).
That dependency now **refuses a super admin with 403**, so every data route on both APIs — Monitor
reads, `POST /runs`, the org admin's `/auth/users` — refuses them, including routes not yet
written, because a data route cannot be written without the tenant this dependency supplies. The
routes about the caller (`/auth/me`, `/auth/password`) take a new `PrincipalDep` (anyone logged
in), and `/admin/*` takes `SuperAdminDep`.

The super admin's view of an organization is **counts** — users, admins, events, alerts — never
rows. Counts are what make a delete or a suspension an informed decision ("this deletes 9,184
events"), and they disclose nothing an alert would.

The dashboard mirrors it: a super admin's sidebar has only *Platform → Organizations, Account*,
and every data page redirects them there (`TenantOnly`), so they never see a page of refusals.

### D3 — Created from the server, never from the website

`python -m iotshield.auth.superadmin create --username <name>` (also `set-password`, `list`)
prompts for the password, or reads it from stdin with `--password-stdin`; never an argument,
which would land in shell history and `ps`. There is no HTTP route that creates or promotes a
super admin: whoever can run the command already holds the database, so it grants nothing new,
while a route that could mint super admins would be the most valuable endpoint in the product
to attack. A super admin cannot be granted through `/admin` either — `role` there is `OrgRole`,
so `"superadmin"` is a 422.

### D4 — Suspend is the reversible half, delete the irreversible one

**Suspend** sets `organizations.suspended`, deletes that organization's sessions (its users are
signed out at once), and makes login answer 403 *after* a correct password — so it tells a
guesser nothing. `resolve_session` also ignores sessions of a suspended organization, so
suspension does not depend on the purge having caught every row. Nothing is deleted.

**Delete** removes the organization, its users, sessions and *every* data row, children before
parents in explicit statements rather than `ON DELETE CASCADE`, which SQLite honours only with a
per-connection pragma. It requires `?confirm=<org_id>`, checked by the server — the dashboard's
type-the-id box is a convenience, the guard is the API's, so a script gets the same protection.

### D5 — Roles can change, and an organization can never lose its last admin

The super admin can promote and demote (`PATCH /admin/organizations/{org}/users/{user}`), reset a
password (which signs the user out everywhere), add and remove users. Demoting or removing an
organization's **last admin** is a 409: an organization nobody can manage should only exist
because someone deleted it. Org admins still cannot change roles; that remains the super admin's.

### D6 — Registration stays open, behind a switch

`IOTSHIELD_OPEN_REGISTRATION` (default on; `0`/`false`/`no`/`off` closes it). Closed,
`POST /auth/register` is 403 and the super admin creates organizations. A public
`GET /auth/options` tells the login page whether to offer registration, so a closed deployment
does not advertise a form that will refuse. Read per request, so the route and the page agree on
the environment's current value.

## Consequences

- The schema changed again (nullable `users.org_id`, the check constraint,
  `organizations.suspended`) and there are still no migrations: `docker compose down -v`.
- ADR-0039's open items "open registration" and "no role changes or admin transfer" are closed.
  An organization whose only admin left is now a super admin's promotion away from managed.

### Open items

- **No audit log.** A super admin's suspensions, deletions and password resets leave no trail —
  the most privileged actions in the product are the least recorded.
- **No super admin management in the UI.** Creating, listing and re-keying super admins is CLI
  only, by design (D3); removing one is a database edit.
- **No "view as" for support.** By the requester's choice the super admin cannot see an
  organization's data; diagnosing a customer's queue needs one of its own users.
- Still no rate limiting on login (ADR-0039), which matters more now that one account can
  manage everything.

## Amendment (2026-10-13): a seeded super admin

Requested after D3: the compose stack should come up with a super admin already present. The
`init` step now runs `python -m iotshield.auth.seed` after the schema, which creates a super admin
from `IOTSHIELD_SEED_SUPERADMIN_USERNAME` / `IOTSHIELD_SEED_SUPERADMIN_PASSWORD`. It does not
weaken D3: the seeder is the CLI's `create` driven by the environment — still server-side, still
no HTTP route.

Two choices hold it to "convenient, not dangerous". It is **create-only**: an existing username
is left exactly as it is, so a password changed on the Account page is not reset by the next
`up`, and a clash with an organization user is a warning rather than a failed stack. And the
well-known defaults (`admin@admin.com` / `password`) live **only in `docker-compose.yml`**, which
binds every port to loopback; the Python has no default, so nothing is seeded anywhere the
variables are unset, and the seeder warns on every start while the default password is in use.
A stack anyone else can reach must override both.

## Amendment (2026-10-13): a super admin may have a home organization

Requested: seed a **UCY** organization and put the super admin *under* it — chosen explicitly as
"the super admin's home organization is UCY: it sees and works with UCY's data and manages all
organizations; other organizations' data stays invisible." This **replaces** D1's invariant and
narrows D2's; the reasoning of both survives, the boundary moves.

- **D1, revised.** The check constraint is now `role = 'superadmin' OR org_id IS NOT NULL`: every
  other user must belong to an organization, and a super admin *may*. One with no home (made by
  the CLI without one) behaves exactly as D1–D2 described.
- **D2, revised.** `AccountDep` refuses a super admin only when it has **no** home. One with a
  home passes, scoped to that home — so no super admin reads *another* organization's data on
  any route, which is the property D2 existed for. At home it is also that organization's user
  manager (`Account.is_admin`), but it is not counted among its admins, so the last-admin rule
  (D5) still protects a real org admin.
- **New: the platform cannot lock itself out.** A super admin's home organization cannot be
  suspended or deleted (409, `ProtectedAccount`): either would sign out, or delete, the account
  that manages everything. No organization route — an org admin's `/auth/users`, or `/admin`'s
  role change, password reset and removal — may touch a super admin (403/409); they are managed
  from the server (the CLI, the seed) and change their own password on the Account page.
- **Seed.** `IOTSHIELD_SEED_ORGANIZATION` (compose: `UCY`, id `ucy`) is created first, the seeded
  super admin is created *at home in it*, and `IOTSHIELD_SEED_DEMO_CONFIG` (compose:
  `config/demo.yaml`) is run into it **only while it has no events**. All three are create-only;
  an existing super admin is never moved into the seed's organization.

What this costs is the separation D2 bought: the platform's most privileged account now also
holds one tenant's data. That is a deliberate trade for this deployment, where UCY *is* the
operator; a multi-customer deployment should seed a homeless super admin (`IOTSHIELD_SEED_ORGANIZATION=`).
