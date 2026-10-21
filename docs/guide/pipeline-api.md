# The Pipeline API

The Pipeline API is how anything other than the CLI runs the pipeline. It is a small HTTP
service that answers three questions: what adapters exist, would this configuration work,
and run it.

Read `docs/guide/architecture.md` first. This guide is written from the outside in: what the
service offers and how to drive it, rather than how the runner works internally.

## Why it is its own service

There are two HTTP services in this repo and the split is deliberate.

| | Serves | Writes | Guide |
|---|---|---|---|
| Pipeline API | `/modules`, `/runs/validate`, `/runs/preview`, `/runs`, `/data-sources` | yes | this one |
| Read API | `/alerts`, `/events`, `/detections`, ... | never | `docs/guide/read-api.md` |

A run is the expensive, slow, occasionally crashing part of this system. Keeping it in a
separate process means a run that eats memory or dies cannot take the triage queue offline
with it, and the two scale independently. The read plane stays genuinely read-only, which is
a property you can check rather than a convention: it has no write path at all.

Serve it with:

```
IOTSHIELD_DATABASE_URL=... uv run python -m iotshield.api.control
```

It exits 2 with `error: IOTSHIELD_DATABASE_URL is not set` if that variable is missing. Host
and port come from `IOTSHIELD_PIPELINE_API_HOST` (default `127.0.0.1`) and
`IOTSHIELD_PIPELINE_API_PORT` (default `8100`). In compose it is the `pipeline-api` service,
published on `127.0.0.1:8100`.

FastAPI gives you `/docs` and `/openapi.json` for free, and because every response model is
a pydantic model the OpenAPI schema is generated from the same types the handlers return.
The dashboard's TypeScript client is generated from that schema, so the wire format has one
definition.

## The six endpoints

| | | |
|---|---|---|
| `GET /health` | liveness plus a real `SELECT 1` | proves the database is reachable, not just that the process is up |
| `GET /modules` | the adapter catalogue | what a UI builds its menus from |
| `POST /runs/validate` | the compatibility guard, no execution | live feedback while somebody edits a config |
| `POST /runs/preview` | one source read by its normalizer, nothing judged or stored | "is this file in the format I said?" |
| `POST /runs` | run it, return a summary | the only path that writes results |
| `/data-sources` | the organization's IT/OT data sources: list (anyone), add/edit/remove (admins) | see *Data sources* below |
| `GET /docs` | generated API docs | |

With `IOTSHIELD_AUTH` set, a seventh group appears: the account routes under `/auth`, and every
route but `/health` needs a session. See *Login and organizations* below.

There is no `GET /runs/{id}`. A run has no identity here: it writes its results through its
sinks and returns a summary, and everything else is read back from the read API. That keeps
this service stateless, which is what lets it be restarted or scaled without coordination.

## A spec is not a config

This is the one concept worth getting right before anything else, because it is a security
boundary rather than a naming choice.

```
PipelineSpec     what the operator chooses:  sources, detectors, alert_engine, assets, sinks
PipelineConfig   PipelineSpec + database_url
```

The API accepts a **spec**. The server supplies `database_url` itself from its own
environment, in `config_from_spec`, and only then does it have something runnable. A caller
cannot name a database, so a caller cannot point a run at somebody else's data or use the
service as a connector to an arbitrary host. `PipelineSpec` simply has no field for it.

The CLI, which is trusted because whoever runs it already has the machine, loads a
`PipelineConfig` directly from YAML and may name any database it likes. Same runner, two
front doors, different trust.

Everything on the wire is `extra="forbid"`. A typo is a 422 that names the path:

```json
{"type": "extra_forbidden", "loc": ["body", "spec", "detector"],
 "msg": "Extra inputs are not permitted"}
```

`detector` instead of `detectors` fails immediately rather than running a pipeline with no
detectors and reporting a clean night.

## `GET /modules`: the catalogue

The single most useful endpoint if you are integrating, because it means a client never has
to know what this product ships.

```
normalizers:   ['stub', 'syslog', 'flow', 'passthrough', 'meter']
detectors:     ['stub_threshold', 'team3_zscore', 'log_burst', 'flow_burst',
                'meter_iforest', 'rules']
alert_engines: ['stub', 'weighted']
sinks:         ['postgres', 'file']
```

Each adapter reports its name, its capability tokens, its input formats, and its full
parameter schema:

```json
{
  "name": "meter_iforest",
  "kind": "detector",
  "consumes": ["lane:telemetry", "payload:meter"],
  "produces": ["detections"],
  "formats": [],
  "params": [
    {"name": "artifact", "type": "str", "default": null, "required": false,
     "choices": ["demo_meter"]},
    {"name": "fp_budget", "type": "float", "default": 0.0014, "required": false,
     "choices": null},
    {"name": "min_train_hours", "type": "int", "default": 883, "required": false,
     "choices": null}
  ],
  "artifact_param": "artifact"
}
```

Three fields there are worth understanding, because they are what keeps a UI from drifting
away from the code.

**`choices`** is the allowed values when a parameter has a closed vocabulary: a detector
preset, a lane, a shipped artifact id, a rule set on the deployment's shelf. Without it a
client renders every non-numeric parameter as a free-text box, which is how a mistyped preset
used to become a guard error instead of an impossible input, and how a `ruleset` typo passed
`validate` and failed only after the payload had been ingested. Two different things fill this
one field, a `Literal[...]` annotation on the factory and vocabularies the registry discovers
by listing files, and a client renders both identically. That is the point: the shape of the
fact is the same and only its provenance differs.

One law goes with it: a parameter whose default is not `None` must be a member of its own
choices. A default outside them makes a form *display* the first option while *holding*
something else.

**`default`** comes from the factory signature, except for learning adapters, whose factory
parameters all default to `None` as a "not given" sentinel. For those the catalogue reads
the real defaults out of the adapter's hyperparameter model, which is why `min_train_hours`
reports 883 rather than null. A form can prefill.

**`artifact_param`** names the parameter that carries a cached fit, when the adapter needs
one to judge at all. It is what separates "ready to run" from "untrained until you name
weights", and a run refuses the latter outright. It is declared by the adapter, never
guessed from parameter names.

The catalogue is built **per request**, and it used to be a module-level constant on the
reasoning that the registries are module-level constants and therefore so is the catalogue.
That held until one parameter's vocabulary stopped being fixed for the process: an artifact id
is a file in a package and cannot change while Python runs, but a rule set is a directory on a
shelf the deployment mounts and the operator is invited to add to. Cached at import, a new rule
set was invisible until the container restarted, which is not a thing an operator should have to
do to publish a rule. A full rebuild walks all four registries — fifteen factories, a signature
introspection per parameter — and measures 0.84 ms. That is cheap; being wrong about what
exists is not.

If you add an adapter and register it, it appears here with no change to this file. That is
the whole extension story for the API surface.

## `POST /runs/validate`: would this work

Send a spec, get back issues. Nothing executes, nothing is read, nothing is written.

```json
{"ok": true, "issues": []}
```

`ok` is `false` only when there is an ERROR-level issue. The distinction matters:

**ERROR** means the run is structurally broken and would crash.

```json
{"level": "error", "stage": "sources[0]",
 "message": "normalizer 'stub' does not accept source_format 'syslog' (accepts: stub-csv)"}
```

**WARNING** means a stage will be idle, which is legal and sometimes intentional.

```json
{"ok": true, "issues": [
  {"level": "warning", "stage": "detectors[0]",
   "message": "detector 'meter_iforest' judges only events carrying meter payload(s), which
               no configured source declares it produces (nothing); its lane matches, so it
               will be handed events and discard all of them"}
]}
```

That one is the interesting case and the reason warnings exist. The lanes line up perfectly,
so every surface that asks "can these connect?" says yes, and the run then publishes zeros.
A warning is the only thing standing between an operator and a clean-looking empty result.

Every issue carries a `stage` that indexes into the spec (`sources[0]`, `detectors[2]`,
`alert_engine`, `ingest`), so a UI can attach it to the field the operator is editing rather
than showing a list of prose at the bottom of a form.

One issue can produce several findings, and they are not deduplicated:

```json
{"ok": false, "issues": [
  {"level": "error",   "stage": "detectors[0]",
   "message": "unknown detector 'nope'; known: flow_burst, log_burst, meter_iforest,
               rules, stub_threshold, team3_zscore"},
  {"level": "warning", "stage": "ingest",
   "message": "source lane 'telemetry' has no detector that consumes it; those events will
               produce no detections"}
]}
```

Both are true. The unknown name is the thing to fix; the coverage warning is a consequence
of it. The guard does not try to guess which findings are downstream of which.

**What the guard cannot see.** It resolves every adapter's declared capability *without
constructing it*, which is what makes it cheap enough to run on every edit. The cost is that
a bad parameter *value* passes validation: `min_count: 1` is a perfectly well-typed integer
and only the adapter's constructor knows it is nonsense. So `validate` legitimately returns
`ok: true` for a spec that `POST /runs` then refuses. That is not a bug in either one; they
answer different questions.

## `POST /runs/preview`: can this file be read

The guard above reads the chain's *shape* — which normalizer, which format — and never the
records, so a CSV handed to the syslog normalizer passes it and becomes a pile of malformed
records after the run. This endpoint closes that gap for one source: it builds the source's
normalizer exactly as a run does and reads the content with it. Nothing is detected, nothing is
stored.

```json
{"source": {"normalizer": "syslog", "source_format": "syslog"},
 "content": "2026-07-07T12:00:00+00:00,pump-02,Pressure,45.0\n..."}
```

```json
{"events": 0, "malformed": 3, "level": "unreadable",
 "rejected": [{"line": 1, "raw": "2026-07-07T12:00:00+00:00,pump-02,Pressure,45.0",
               "error": "unrecognized syslog line (neither RFC3164 nor RFC5424)"}]}
```

`level` is one verdict from `pipeline/runner.py: rejection_level`, the same function behind the
run's own "rejected N% of input" warning (ADR-0017), so the preview and the run cannot disagree
about where the line is:

| `level` | means |
|---|---|
| `clean` | every record read |
| `some_rejected` | a few rejects, under half: an ordinary feed with corrupt lines |
| `mostly_rejected` | half or more rejected: probably the wrong normalizer or format |
| `unreadable` | not one record read: almost certainly the wrong normalizer, format or file |
| `empty` | nothing to read (only blank lines, or a single meter reading) |

`rejected` quotes the first three rejects with the line they were found on. A normalizer that
cannot even be asked — an unknown name, bad params, a format it does not read — is a 422 with
the reason in `detail.message`. The content limit is the same as one source of a run.

The Compose page calls this for every source as it is filled in, and refuses to leave step 1
while a source is `unreadable`.

## `POST /runs`: run it

```json
{
  "spec": { ... },
  "inputs": [{"content": "<raw text>", "origin": "nightly-export"}]
}
```

`inputs` aligns to `spec.sources` **by position**, one payload per source. The content is the
raw bytes as text, exactly what the CLI would have read off disk, so a source's `path` is
ignored here entirely. `origin` is a provenance label that lands on the events and on every
malformed record, and defaults to `api-run`.

A 200 is a summary, not the results:

```json
{
  "events": 1,
  "malformed": 1,
  "detections": 1,
  "alerts": 1,
  "warnings": ["[input:probe] the 'stub' normalizer rejected 50% of input (1/2 records);
                the input may not match the chosen normalizer"],
  "destinations": ["postgres (postgresql+psycopg://iotshield:***@postgres:5432/iotshield)"],
  "top_alerts": [ ... ]
}
```

`top_alerts` is the five highest-priority alerts as full contract objects, so a caller has
something to show without a second request. Everything else is read back from the read API.

`warnings` carries what the guard said plus what the run itself noticed: a source whose
reject rate passed 50%, and any detector that declined to judge and said why. Those are
run-time observations that no amount of validation could have produced.

`destinations` is where the result actually went, resolved and credential-safe (the password
is masked). The Postgres entry resolves a SQLite file URL to its absolute path, because a
relative path plus a different working directory is otherwise invisible: two processes
launched from different directories quietly use two different databases.

### What comes back when it does not work

| Status | Body `message` | Cause |
|---|---|---|
| 422 | `expected 1 input(s) to match spec.sources, got 2` | payload count does not match the spec |
| 422 | `incompatible pipeline configuration` + `issues` | an ERROR-level guard issue |
| 422 | `invalid params for detector 'log_burst': min_count must be >= 2 (a burst is 2+ events), got 1` | a bad parameter value, caught at construction |
| 422 | pydantic field errors | a malformed request body |
| 500 | `the run failed while writing its result: ...` | a sink failed |

The three 422 shapes all carry `{"message": ..., "issues": [...]}`, so a client has one thing
to parse. `issues` is populated only for guard failures.

The parameter-value 422 is worth noting because it used to be a bare 500. The adapter's own
carefully worded message reached nobody, and the caller could not tell a mistake of theirs
from a server fault. It is caught inside the run, **before any sink writes**.

Untrained detectors are refused the same way:

```json
{"message": "incompatible pipeline configuration", "issues": [
  {"level": "error", "stage": "detectors",
   "message": "detector 'team3_zscore' has no trained state; name its artifact in params,
               or fit it in the evaluation harness (a run never fits)"}
]}
```

That check needs the constructed object, so it cannot live in the guard, but it is reported
in the guard's own shape so a client handles it identically.

The 500 names the failure rather than returning an empty body. Sinks run in config order with
no cross-store atomicity, so a run can legitimately have committed its Postgres rows and then
failed writing a file — which is why the message quotes the sink's own error instead of a
generic one. *Which* destinations completed is not in the body yet; read it back from the read
API. An opaque 500 there is genuinely ambiguous about whether the data landed.

## Data sources

An organization's register of where its data comes from, each source typed **IT** (network
traffic, logs) or **OT** (meters, sensors) — ADR-0041. Always mounted; with auth off it is the
default organization's. Anyone in the organization lists; its admins write.

| | |
|---|---|
| `GET /data-sources` | IT before OT, then by name |
| `POST /data-sources` | 201; 409 for a name the organization already uses |
| `PUT /data-sources/{source_id}` | replaces every field; 404 for an id that is not this organization's |
| `DELETE /data-sources/{source_id}` | 204; data already ingested is untouched |
| `GET /data-sources/connections` | the source databases this organization may pull from (ADR-0042) |
| `GET /data-sources/{source_id}/devices` | what a pull can be narrowed to; query string = connector params |
| `POST /data-sources/{source_id}/pull` | a date range of the source's database, as text for its normalizer |

```json
{"name": "SensiPro meters", "type": "OT", "description": "hourly export",
 "normalizer": "meter", "source_format": "sensipro-hourly-csv"}
```

`normalizer` and `source_format` are optional, and checked against the registry exactly as a
run's are: an unknown normalizer, a format it does not read, a format without a normalizer, or
a multi-format normalizer with none named are all 422; a normalizer with one format gets it
filled in. Blank optional text is stored as not set. The register is not read by the pipeline:
a run still names its own normalizer and format.

### Pulling from a source's database (ADR-0042)

A source may name a **connection**, one of the databases declared in `config/connections.yaml`
(never a host). Its reader is then the connector's own (`sensipro` → `meter` /
`sensipro-hourly-csv`; `biology` → `flow` / `cicflowmeter-csv`), and anyone in the
organization can pull from it:

```json
POST /data-sources/{id}/pull
{"start": "2023-07-01", "end": "2023-07-01", "params": {"utility": "water", "devices": ["a01a035"]}}

{"content": "device_id,from_timestamp,last_value,unit\n...", "normalizer": "meter",
 "source_format": "sensipro-hourly-csv", "rows": 24, "origin": "SensiPro meters 2023-07-01..2023-07-01"}
```

The content is a run's input as-is. Days are the source's own calendar (SensiPro: Cyprus time,
Biology: UTC), both inclusive. Every session is read-only with a 30 s query timeout. A window
over 92 days, over 100,000 rows, or over one source's 4,000,000 characters is a 422 that gives
the size; it is never truncated. A database that does not answer is a 502 naming the connection
and nothing else. A connection not declared, or reserved for another organization, is a 422
either way.

## Where results go

`sinks` is part of the spec, so the caller chooses destinations even though it cannot choose
the database:

```json
"sinks": [{"name": "postgres"}, {"name": "file", "params": {"path": "nightly.jsonl"}}]
```

Two ship. `postgres` is the default and the one the read API and dashboard consume. `file`
appends the entire run as one JSON line, losslessly, for a deployment that wants no database
at all.

Every sink is constructed **before any of them writes**. A typo in `sinks[1].params` used to
commit the run to Postgres and then exit, so re-running after the fix duplicated the data.

A file sink's path is confined to the server's `IOTSHIELD_OUTPUT_DIR` (default `results`,
bind-mounted to the host in compose). Directory components in a caller-supplied path are
dropped rather than honoured, so `sub/dir/out.jsonl` writes `results/out.jsonl`, and a path
that names no file at all is refused:

```
422  invalid params for sink 'file': sink path '..' does not name a file; give a plain
     filename (it is written under the server's configured output directory)
```

Validating rather than silently sanitising matters here: `Path("..").name` is `".."`, so an
earlier basename-only version handed the sink a directory and the caller got an opaque
`OSError` instead of a message naming the field.

## Two declared limits

Both exist because a synchronous HTTP run has real resource bounds, and a declared limit
gives a caller a 422 that names the field instead of a container that dies.

**`MAX_SOURCE_CHARS = 4,000,000` per source.** A run executes inside the request and holds
the whole payload plus every event it fans out into, each retaining its own `raw` line.
Measured at roughly 41x amplification: 4.3 MB of CSV became about 177 MB of heap and 3.5
seconds of one worker. Unbounded, a handful of concurrent requests takes the process down.
Over the limit you get:

```json
{"type": "string_too_long", "loc": ["body", "inputs", 0, "content"],
 "msg": "String should have at most 4000000 characters"}
```

**The run is synchronous.** There is no job queue and no polling. A big input holds a worker
for seconds, which is why the nginx template in front of the dashboard sets a long
`proxy_read_timeout` on the pipeline routes specifically. If you need long runs, use the CLI;
an async job API is not built.

## Login and organizations

Off unless `IOTSHIELD_AUTH` is set (compose sets it). On, the `/auth` router is registered and
both APIs refuse a request without a session with **401** (ADR-0039):

| | | |
|---|---|---|
| `POST /auth/register` | `{organization, username, password}` | creates the organization and you as its admin, and logs you in. 409 if the name or username is taken |
| `POST /auth/login` | `{username, password}` | sets the session cookie. 401 for a wrong username *or* password, worded identically |
| `POST /auth/logout` | | ends the session server-side; works with an already-expired cookie |
| `GET /auth/me` | | who you are and your organization (`org_id` is the slug `--org` takes) |
| `POST /auth/password` | `{current_password, new_password}` | logs out your other sessions |
| `GET /auth/users` | | admin only: the organization's users |
| `POST /auth/users` | `{username, password, role}` | admin only: add a user with a temporary password |
| `DELETE /auth/users/{id}` | | admin only; not yourself; another org's user is a 404 |

The session is an `HttpOnly`, `SameSite=Strict` cookie holding a random token; the database stores
only its SHA-256. Passwords are scrypt-hashed (`auth/passwords.py`), minimum 8 characters.

**A run belongs to whoever asked.** `POST /runs` stores its results under the caller's
organization — taken from the session, never from the request body, the same way the caller
cannot name the database. A file sink writes under `IOTSHIELD_OUTPUT_DIR/<org_id>/`. The CLI has no
session, so it takes `--org` (default `default`) and creates that organization if it is absent.

Off, there is no `/auth` router at all (a 404, which is how the dashboard knows not to show a
login) and every request is the admin of the one `default` organization.

`GET /auth/options` (public) answers `{open_registration}`: `IOTSHIELD_OPEN_REGISTRATION=0`
closes `POST /auth/register` with a 403, and the login page stops offering it.

### The super admin's `/admin` routes

A **super admin** (ADR-0040) manages every organization. It may have a **home organization**,
whose data it then sees like that organization's admin; with none, every data route answers it
403. It never sees another organization's data. Its home cannot be suspended or deleted, and no
organization route can remove, demote or reset it (409/403). Compose's `init` step seeds the
`UCY` organization, a super admin at home in it (`admin@admin.com` / `password`) and the demo
data into UCY (`python -m iotshield.auth.seed`; create-only). Otherwise super admins are created
only from the server:

```
python -m iotshield.auth.superadmin create --username root     # prompts for the password
python -m iotshield.auth.superadmin set-password --username root
python -m iotshield.auth.superadmin list
```

| | | |
|---|---|---|
| `GET /admin/organizations` | | every org with counts (users, admins, events, alerts) — never rows |
| `POST /admin/organizations` | `{organization, username, password}` | an org and its first admin; works with registration closed |
| `GET /admin/organizations/{org}` | | one org's summary |
| `POST /admin/organizations/{org}/suspend` | | signs its users out, blocks login (403 after a correct password), keeps data |
| `POST /admin/organizations/{org}/reactivate` | | |
| `DELETE /admin/organizations/{org}?confirm={org}` | | the org, its users and **all its data**; `confirm` must repeat the id (400 otherwise) |
| `GET/POST /admin/organizations/{org}/users` | | list / add (role `admin` or `member`; `superadmin` is a 422) |
| `PATCH /admin/organizations/{org}/users/{id}` | `{role}` | demoting the last admin is a 409 |
| `POST /admin/organizations/{org}/users/{id}/password` | `{password}` | new temporary password; signs them out |
| `DELETE /admin/organizations/{org}/users/{id}` | | removing the last admin is a 409 |

Everything under `/admin` is 403 to anyone else, organization admins included.

## The evaluation plane is absent unless you ask

The benchmarking endpoints (`/eval/...`) are mounted only when `IOTSHIELD_EVAL_API` is set to
a truthy value. Unset, they are not registered at all:

```
GET /eval/detectors        -> 404
app routes: /data-sources, /data-sources/{source_id}, /docs, /health, /modules, /openapi.json, /redoc, /runs, /runs/preview, /runs/validate
```

Absent rather than guarded, which is a stronger property: there is no route to probe, no
handler to reach, and the eval package is imported lazily so it can be deleted from a
product image without breaking this module. The compose stack in this repo is the research
stack, so it opts in; a product deployment gets the default.

That switch lives in its own tiny module, `api/_eval_gate.py`, with no eval imports of its
own. Reading the flag and using it are separate concerns, and only one of them is allowed to
depend on the harness.

## Driving it

From Python, in-process, with no server at all, which is also how the tests do it:

```python
from fastapi.testclient import TestClient
from iotshield.api.control import create_control_app
from iotshield.storage import create_schema, make_engine

engine = make_engine("sqlite:///probe.db")
create_schema(engine)
client = TestClient(create_control_app(engine))

client.get("/modules").json()
client.post("/runs", json={"spec": spec, "inputs": [{"content": csv_text}]}).json()
```

`create_control_app(engine)` takes an injected engine, which is the whole reason this is
testable: tests pass SQLite, production passes Postgres, and nothing in the module reads a
connection string.

Over HTTP:

```
curl -s localhost:8100/modules | jq '.detectors[].name'
curl -s -X POST localhost:8100/runs/validate -H 'content-type: application/json' \
     -d '{"spec": {"sources": [{"normalizer": "stub", "source_format": "stub-csv"}],
                   "detectors": [{"name": "stub_threshold",
                                  "params": {"threshold": 80.0, "lane": "telemetry"}}]}}'
```

The API never creates the database schema. `iotshield.storage.init` owns that (it is the
compose `init` step), and the CLI creates it for standalone runs. `create_control_app`
assumes the tables exist.

## Extending it

Most extensions do not touch this file. Adding a normalizer, detector, alert engine or sink
means registering it, and `/modules` publishes it, `/runs/validate` reasons about it, and the
dashboard renders a form for it with no change here.

Three things do require an edit:

1. **A new endpoint.** Add it inside `create_control_app` with a pydantic response model.
   The model is the schema, so regenerate the dashboard's client afterwards.
2. **A new kind of parameter metadata.** Add a field to `ParamInfo` and fill it in
   `describe_params`, which is where both catalogues' params are built. Keep it one field per
   fact: `choices` deliberately serves two sources rather than existing twice.
3. **A new resource a sink needs.** Add a field to `SinkResources` and wire it in
   `create_control_app`, where the deployment's handles are assembled.

Do not add a way for a caller to name a database. That is the boundary this service exists
to hold.

## Where to look

| | |
|---|---|
| The service, wire models, introspection | `src/iotshield/api/control.py` |
| Spec vs config, the desugaring, loaders | `src/iotshield/pipeline/config.py` |
| The compatibility guard | `src/iotshield/pipeline/compat.py` |
| `execute_run`, sink ordering, reports | `src/iotshield/pipeline/runner.py` |
| The adapter registries | `src/iotshield/pipeline/registry.py` |
| The sink port and its resources | `src/iotshield/sink/__init__.py` |
| Path confinement | `src/iotshield/sink/file.py` |
| The eval-plane switch | `src/iotshield/api/_eval_gate.py` |
| Service wiring, ports, mounts | `docker-compose.yml` |
| Why a separate writer service | `docs/adr/0015-pipeline-api.md` |
| The guard's two severities | `docs/adr/0014-capability-guard.md` |
| Multi-source ingest | `docs/adr/0019-multi-source-ingestion.md` |
| Pluggable sinks | `docs/adr/0018-result-sink.md` |
| The payload ceiling and the 422 mapping | `docs/adr/0033-pre-handover-review-fixes.md` |
| The payload axis in the catalogue | `docs/adr/0035-the-lane-is-not-the-whole-shape.md` |
