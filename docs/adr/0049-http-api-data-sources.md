# ADR-0049: A data source may be an HTTP API, with a mapping and write-only credentials

- **Status:** accepted
- **Date:** 2026-11-09
- **Related:** ADR-0041 (the data-source register), ADR-0042 (connectors — this amends its
  "never a host in a request" stance, D4), ADR-0047 (a pull is read by the run), ADR-0025
  (`passthrough`, which reads what the mapping writes), ADR-0039 (tenancy)
- **Scope:**
  - connectors: `connect/api/` (endpoint, mapping, guard, client) and `connect/target.py`
    (`PullTarget`, with `DatabaseTarget` and `ApiTarget`); `port.chunked` / `port.render` shared
  - storage: a new table `data_source_endpoints` (settings JSON + sealed credentials);
    `storage/secrets.py`; `normalize/_linewise.validation_message` names the field in a reject
  - the Pipeline API: `DataSourceRequest.api` + write-only `credentials`;
    `DataSourceInfo.api` / `has_credentials` / `reader_params`; `pull_target` replaces
    `connected_source`; a run refuses a pull read in the wrong lane
  - the dashboard: Data sources → *Pull from: An HTTP API…*; Compose pulls an API source like a
    connected one
  - deployment: `cryptography` is a runtime dependency; `IOTSHIELD_SECRET_KEY`,
    `IOTSHIELD_API_ALLOWED_HOSTS`

  **No contract changes.** The API's records become `NormalizedEvent`s through the existing
  `passthrough` normalizer; nothing in `contracts/` moved.

## Context

A data source could be pulled from only if its data sat in a database the **deployment**
declared in `config/connections.yaml` (ADR-0042). Many IT/OT systems instead expose an HTTP API
— a SCADA historian, a cloud IoT platform, a building-management system — usually behind a
user name and password or an API key, answering JSON in a shape of its own. The request was to
let an organization register one in the product: its URL, and either username + password or a
key.

Three things are different from a database connection. The person registering it is an **org
admin**, not whoever runs the server, so the host arrives in a request. The credentials are
typed into a form, so they cannot live in the environment. And the response is JSON nobody here
chose, so there is no connector to write for it.

## Decisions

### D1 — An API is a second kind of pull target, not a new Compose mode

A run, the pull route, the device list and saved-run restore were all written against "a pull"
(ADR-0047), so the API joins there: `connect/target.py` turns the old
`(Connection, ConnectorSpec)` pair into a `PullTarget` — what it is read with, `check` (refuse a
window or params before reading anything), `chunks`, `summary`, `devices` — with two
implementations. The run, the saved run and Compose's restore see no difference, and an API
source is chosen in the same "Pull from a data source" panel. A Compose mode with the URL and
key typed per run was rejected: the key would travel in every run request and a saved run could
not be re-opened without retyping it.

### D2 — Credentials are write-only and sealed

The password or key is stored as a Fernet token (AES-128-CBC + HMAC) under
`IOTSHIELD_SECRET_KEY`, in its own column of its own table; the endpoint's settings are plain
JSON beside it. No response ever carries a secret — `has_credentials` says whether one is
stored — so an edit opens with the secret fields blank, and **blank keeps the stored one**.
Switching the authentication kind needs the new secret, since the old one is for a different
scheme. With no key the server **refuses** to save an API source that needs credentials (503,
naming the variable) rather than storing it in the clear.

Rotating the key makes stored credentials unreadable; each source then says "enter them again".
A key ring is deferred until someone needs rotation without retyping.

### D3 — The mapping writes `normalized-jsonl`; `passthrough` reads it

The admin declares where the records are and which dotted path is the timestamp, the asset, and
either the readings (telemetry, one event per named metric — ADR-0003 D1's fan-out) or the
message and level (log). The mapper does **not** build events: it writes one `normalized-jsonl`
line per event and the run reads them with `passthrough`, so contract validation, the UTC rule
and `MalformedRecord` capture are inherited rather than re-implemented. A missing path leaves
the field out and the contract names it; `validation_message` now prefixes each problem with
its field (`timestamp: Field required`), which every line normalizer benefits from. A missing
*reading* is marked explicitly (`"missing": "<path>"`), because the contract lets an event carry
no value and the blank would otherwise pass silently.

Timestamps: an epoch is UTC by definition; an ISO string with no offset is refused (rule 4)
unless the mapping names the zone the API speaks. The lane is part of the mapping, so the source
publishes `reader_params: {lane}`, Compose sets it on the source, and **a run refuses** a pull
read in another lane — `passthrough`'s default (telemetry) would otherwise reject every line of
a log API.

Flow is not offered: a flow record's tuple rides in `metadata` behind `FlowRecord`, which a
dotted-path mapping cannot fill honestly. Arbitrary `metadata` mapping is deferred with it.

### D4 — The URL guard replaces "never a host in a request"

ADR-0042 kept hosts out of requests so no organization could aim the server at internal
addresses (SSRF) and read the error to learn what answered. An API source gives that up on
purpose, so `connect/api/guard.py` takes it over:

- `https` only; `http` only for a host the operator allowlists;
- no user info in the URL (credentials have their own sealed fields);
- **every** address the host resolves to must be public — loopback, RFC 1918 / ULA,
  link-local (the cloud metadata service), CGNAT, multicast, reserved and unspecified are
  refused; `IOTSHIELD_API_ALLOWED_HOSTS` exempts named hosts (how `host.docker.internal` or a
  plant's LAN API is reached, decided by whoever runs the server);
- checked when a source is **saved and before every request**, including each next page;
- **redirects are never followed**, and a next page must be on the configured URL's origin, so
  a response can steer neither the server nor the credentials elsewhere;
- a 30 s timeout, 20 MB per response (read in pieces and abandoned past it), 1,000 pages;
- a failure names the source and the HTTP status, **never the response body**, which can echo
  the request and so the credentials.

**Residual risk, recorded rather than solved:** DNS rebinding. A name that resolves to a public
address for the check and to a private one for the connection a moment later is not stopped,
because httpx connects by name. Pinning the checked address into the connection is the fix,
deferred until a deployment exposes the stack to untrusted org admins.

### D5 — The window and pagination are declared, not guessed

A pull asks for calendar days (ADR-0042's 92-day limit applies). The admin names the two query
parameters and their form: `date` (YYYY-MM-DD, end inclusive) or `iso` / `epoch_s` / `epoch_ms`
UTC instants with an exclusive end at the midnight after the last day. Pagination is one shape —
a dotted path to the next page's URL, absolute or relative, followed until it is null or empty;
a loop is refused. Page-number and cursor-token pagination are deferred. An API takes no pull
params and lists no devices.

## Consequences

- An organization can pull from its own HTTP APIs with no server change, and runs, saved runs
  and Open in Compose work for them unchanged.
- `cryptography` joins the runtime image (it is small and was already a transitive dependency
  of nothing we ship — it is the first crypto library here).
- The pull summary reads every page once to count records; the run reads them again. No cache.
- A new table, so `create_all` adds it to an existing database: **no `down -v` needed**.
- Rejected lines now name their field for every line normalizer, which changes the stored
  `error` text of `stub`/`passthrough` validation rejects (more specific, same meaning).
- Open: DNS rebinding (D4), key rotation (D2), other pagination shapes and flow / metadata
  mapping (D3, D5), and a "test this mapping" button that works before the source is saved.
