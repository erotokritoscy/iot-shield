# Architecture

How iotshield is put together, and why it is put together that way. Read this before the
other guides in `docs/guide/`; they assume it.

## What the product does

An IT/OT estate produces three kinds of evidence about its devices. Telemetry is numbers,
reported repeatedly. Logs are text, written when something happens. Network flows are one
summary line per conversation between two hosts.

Each stream on its own is weak evidence. A compromised IP camera keeps reporting a normal
22 degrees and does not write a log line about being compromised; only its network behaviour
shows anything. A camera that is merely broken looks alarming on one stream and normal on
the others. So the streams have to be readable together, per device, on one clock.

iotshield turns all three into one ranked, explained queue:

```
input -> normalize -> detect (per modality) -> correlate & prioritize -> store -> API -> dashboard
```

The stage is called *prioritize* rather than *filter*. Every detection becomes an alert, and
ranking decides what an analyst looks at first. A gate that silently discards findings would
mean a real attack could disappear without anyone knowing it had been considered.

## The shape of the codebase

The pipeline is a spine with sockets. Each stage is defined by a *port*, which is a fixed
interface, and each concrete module is an *adapter* sitting behind one. This has two
practical results:

- `pipeline/runner.py` is written against the ports. It has not changed to accommodate any
  of the adapters added since it was written.
- Choosing a different module is editing a name in a YAML file, not editing code.

Ports are `typing.Protocol`, so they are structural: any object with the right method shape
*is* a Normalizer. There is no base class to inherit and nothing to import from us. A
contributor's class only has to have the right shape.

The repo exists because five graduate student modules needed to work together and none of
them shared a data shape. The alternative to ports was a converter between each pair of
modules, which does not survive adding a sixth.

There is a rule about bringing outside code in. Wrap a module if its behaviour fits the
port; rewrite its ideas if it does not. The syslog normalizer is a rewrite because the
original dropped lines it could not parse and substituted `datetime.now()` for unreadable
dates. Both are things this system forbids, and both happened inside it, upstream of any
seam an adapter could sit on. Once information has been destroyed an adapter cannot recover
it. Either way, the adapter's docstring records where the ideas came from.

## The contracts

`src/iotshield/contracts/` holds five models. They are frozen in the sense that changing one
requires an ADR explaining what breaks.

| Contract | What it is | Notes |
| --- | --- | --- |
| `Asset` | A monitored device, the "who" everything correlates on | `criticality` is a business fact the alert layer reads |
| `NormalizedEvent` | One observation | One number per event, `raw` kept verbatim, UTC-aware timestamp |
| `MalformedRecord` | An input record that failed to parse | Captured as data, never dropped |
| `DetectionResult` | One detector's positive judgment over a time window | `score` and `threshold` are the method's own numbers |
| `PrioritizedAlert` | The analyst-facing decision | `rationale` is required and non-empty |

Four rules here are enforced at construction rather than documented and hoped for:

1. **Timestamps are timezone-aware UTC.** A naive datetime is rejected, and so is a bare
   epoch number, which pydantic would otherwise read as a 1970 date. Everything correlates
   on `(asset, time)`, so a source in the wrong zone produces no correlation at all, and
   that looks identical to a night when nothing happened.
2. **Unknown fields are rejected** (`extra="forbid"`), so a typo fails at construction.
3. **One number per event.** A multi-channel reading fans out into one event per channel,
   and a multivariate detector pivots it back into a matrix. This is what lets one envelope
   carry a temperature reading and a log line without either being a special case. The
   pivoting is a real cost and was accepted knowingly.
4. **Numbers are finite**, including numbers nested inside `evidence`, `metadata` and
   `score_components`. `NaN` is not valid JSON: Postgres rejects it, SQLite stores it
   invalidly, and the file sink rewrites it to `null`. Three different wrong answers.

The contracts are also the API's wire format. FastAPI serializes them directly, so there is
no translation layer, the OpenAPI schema is the contract schema, and the dashboard's
TypeScript types are generated from it. A contract change shows up as a build failure rather
than as a wrong number on a page.

`MalformedRecord` is the one that surprises people. A parse failure gets an id, the verbatim
text, a reason, and its origin, and it is stored alongside the successes. Normalization is
the trust boundary, and input that does not parse is both expected and disproportionately
suspicious, so it is modelled as data. Every normalizer obeys the same law: every non-blank
input line either produces events or produces a `MalformedRecord`.

## The four ports

| Port | Question it answers | In | Out | File |
| --- | --- | --- | --- | --- |
| Normalizer | What does this raw text mean? | text, declared format, origin | events and rejects | `normalize/port.py` |
| Detector | Is anything wrong on this modality? | events of one lane | detections | `detect/port.py` |
| AlertEngine | What should an analyst look at first? | detections and assets | ranked alerts | `alert/port.py` |
| ResultSink | Where does the result go? | the whole `RunResult` | nothing (a side effect) | `sink/__init__.py` |

Output is a port because where results go is a deployment choice: Postgres for the
dashboard, a JSONL file for a research run with no database, a webhook into a SIEM later.
Replacing `sinks: [{name: postgres}]` with `sinks: [{name: file, params: {path: run.jsonl}}]`
runs the identical chain and reports `destinations: file`, with no database involved.

The sink is the one port that declares no capability. The other three are joints in a
transformation chain, where the shape leaving one stage has to fit the shape entering the
next. A sink consumes the finished result and has nothing downstream to fit. It still gets
registration, discovery and swap-by-name like the others.

Detectors may also implement three optional capabilities. Not implementing one is not a
defect.

| Capability | Means | Who calls it |
| --- | --- | --- |
| `Fittable` | I can learn a baseline from clean data | Only a benchmark. The pipeline never fits |
| `Diagnosable` | I can say in advance why I will find nothing | The runner and the harness, for their reports |
| `Stateful` | I can exist in an untrained state | The runner, to refuse an untrained model |

`Diagnosable` exists because a detector that skips an asset by design and a detector looking
at a healthy asset both report zero detections. An operator cannot tell those apart from the
counts.

## Lanes and payloads

A **lane** is a modality. There are three: `telemetry`, `log`, `flow`. Events carry theirs
in `source_type`, and each detector declares the one lane it judges.

Routing is the runner's job. For each detector it filters the pooled events down to that
detector's lane and sorts them by `(asset_id, timestamp)` before handing them over. The sort
is part of the port's promise rather than a convenience: a sliding-window detector fed
unordered events does not fail, it reports nonsense, because the trailing edge of the window
stops advancing and every position qualifies. One of the shared helpers raises instead of
continuing if it sees time go backwards. Detectors are also required to ignore foreign-lane
events themselves, even though the runner has already filtered.

A **payload** is a second axis inside a lane: which typed record rides in `metadata`. Some
modalities do not fit the contract's own fields. A flow's signal is a tuple (who talked to
whom, on which port, with what result) of which only the byte count is a number `value` can
hold. A meter reports a cumulative counter, while the useful observation is consumption over
an interval, and one `value` cannot be both. Those travel as `flow.FlowRecord` and
`meter.MeterReading`, with the untyped hop through `metadata` kept one function call wide.

That created a problem worth understanding, because it is the reason there are two axes and
not one. `lane: telemetry` means two different things: a bare numeric reading, and a meter
reading carrying a `MeterReading`. The meter detector needs the second and could only
declare the first. Benchmarking it against a SKAB telemetry corpus produced sixteen rows of
zeros, and under the default fit protocol no explanation at all. Every layer was behaving
correctly.

Giving meters their own `Lane` would have fixed the declaration and broken something real. A
lane is the correlation axis: it decides which events pool together, which detector is
handed them, and what counts as independent agreement about one device. `Lane.METER` would
mean a meter anomaly and a temperature spike on the same device stop corroborating each
other. So the typed vocabulary became its own axis instead (ADR-0035).

The axis belongs to a *configuration*, not to an adapter, which is why it had to be a token
rather than a flag on the registry entry:

```
rules(lane=flow)       consumes[lane:flow, payload:flow]     # reads its fields through FlowRecord
rules(lane=log)        consumes[lane:log]                    # reads the contract's own fields
```

## Capabilities and the compatibility guard

A **capability** is a stage's declared plug shape: the set of tokens it `consumes` and the
set it `produces`.

| Token | Axis | Wire form |
| --- | --- | --- |
| `FormatToken` | Raw input format, at ingest | `format:zeek-conn` |
| `LaneToken` | Modality, at normalize to detect | `lane:flow` |
| `PayloadToken` | Typed vocabulary within a lane | `payload:meter` |
| `SignalToken` | Pass-through at the later joints | `detections`, `alerts` |

They are typed objects rather than strings, so the type checker catches a mismatched axis or
a mistyped lane, and the `lane:` prefix scheme is not scattered through the code. Tokens
render to their wire form only at the API boundary.

Two properties matter. A capability is declared next to its adapter, so an adapter cannot be
registered without declaring its shape, because the registry entry requires both in one
record. And it is readable without constructing the adapter, which is why it is static
metadata: building the meter detector loads a trained model, and building the rule detector
compiles every YAML rule in the library. Validating a chain should not pay either cost, and
neither should a catalogue endpoint that lists every adapter.

The reason any of this exists: a misconfigured pipeline does not crash. It runs, reports
zero findings and exits 0, which is what a healthy run over quiet data also does. Most of
the checks below exist to tell those two states apart.

`pipeline/compat.py` walks the chain and reports at two severities.

| Level | Meaning | Consequence |
| --- | --- | --- |
| ERROR | Structurally broken, or a name or param that cannot be resolved | Refused before any work. CLI exits 2, API returns 422 |
| WARNING | A stage is idle. Legal, and sometimes intended | Reported, and carried into the run report |

What it checks:

| Joint | Level | Catches |
| --- | --- | --- |
| Source format against its normalizer | ERROR | A normalizer that would raise at runtime |
| Unresolvable adapter name or param | ERROR | Typos, bad values |
| Duplicate source | WARNING | The same bytes ingested twice, so every finding is reported twice |
| Duplicate detector | WARNING | The same detector built twice, so one piece of evidence counts as corroboration |
| Detector lane nobody produces | WARNING | A detector that will never fire |
| Detector payload nobody produces | WARNING | Lane matches, vocabulary does not: handed events, discards all of them |
| Lane no detector consumes | WARNING | Data ingested but never judged |
| Alert engine inputs | WARNING | An engine nothing feeds |

Lane and payload are reported first-unmet-wins. A detector on a foreign lane is also missing
that lane's payload, and reporting both would mean fixing two things to repair one break.

The duplicate-detector check compares what the entries *build*, not what they say. `{}` and
`{preset: fan_out}` are the same detector when `fan_out` is the default, and YAML's `80` is
the factory's `80.0`. It is the only place the guard constructs anything, and it does so
only for a name that already appears more than once.

Four of these, over the demo inputs `scripts/make_demo_data.py` writes. The four configs named
below are one-off files written to provoke each case and are not in the repo — the one run
config that ships is `config/demo.yaml`, and it is deliberately a chain the guard passes
clean.

```
$ uv run python -m iotshield.pipeline --config bad_format.yaml     # syslog normalizer, stub-csv source
error: incompatible pipeline configuration:
  - [sources[0]] normalizer 'syslog' does not accept source_format 'stub-csv' (accepts: syslog)
$ echo $?
2
```

```
$ uv run python -m iotshield.pipeline --config idle_detector.yaml  # camera_telemetry.csv, log detector
  events: 90   malformed: 0   detections: 0   alerts: 0
  Warnings:
   ! [detectors[0]] detector 'log_burst' consumes lane(s) log but no configured source produces
     them (telemetry); it will not fire
   ! [ingest] source lane 'telemetry' has no detector that consumes it; those events will
     produce no detections
```

Both directions are reported, because a detector that will never fire and a lane nobody
judges are different mistakes.

```
$ uv run python -m iotshield.pipeline --config payload_gap.yaml    # camera_telemetry.csv, meter detector
  events: 90   malformed: 0   detections: 0   alerts: 0
  Warnings:
   ! [detectors[0]] detector 'meter_iforest' judges only events carrying meter payload(s), which
     no configured source declares it produces (nothing); its lane matches, so it will be handed
     events and discard all of them
   ! meter_iforest judges consumption readings carrying a 'meter' payload; none of the 3 telemetry
     asset(s) here carry one, so nothing was scored - this input is telemetry but not metering
```

Two mechanisms report there. The first line is the guard reasoning from declarations before
the run; the second is the detector's own `Diagnosable` answer, from the data it was given.

```
$ uv run python -m iotshield.pipeline --config dupes.yaml          # camera_flow.log twice, rules twice
  events: 24   malformed: 0   detections: 6   alerts: 2
  Warnings:
   ! [sources[1]] source (data/demo/camera_flow.log) is ingested identically by sources[0]; a run
     pools its sources, so every record will be counted twice ...
   ! [detectors[1]] detector 'rules' builds the same detector as detectors[0] ...
  Top alerts:
   1. [HIGH] 10.0.0.24 priority=52.7 - 4 detection(s) (2 distinct) across 1 lane(s) ...
```

12 events became 24 and 3 detections became 6. The alert layer counts distinct claims rather
than rows, which is what `4 detection(s) (2 distinct)` is showing.

## The registry

`pipeline/registry.py` is four dictionaries mapping a config name to a spec. Three hold an
`AdapterSpec`; `SINKS` holds the lighter `SinkSpec`, which pairs a factory with
`requires_database` and no capability, for the reason above.

```python
DETECTORS = {
    "meter_iforest": AdapterSpec(
        _meter_iforest,                # factory: how to build it
        static(_METER_CAPABILITY),     # capability: how it connects
        params_type=MeterParams,       # its hyperparameters (ADR-0022)
        artifact_param="artifact",     # which param names a cached fit
        discovered={"artifact": _meter_artifacts},  # which ids exist, asked when needed
    ),
}
```

Registered today: 5 normalizers covering 7 input formats, 6 detectors, 2 alert engines, 2
sinks. Ask the code rather than trusting that sentence:

```
uv run python -c "from iotshield.pipeline.registry import DETECTORS; print(sorted(DETECTORS))"
```

The last three fields are how an adapter that learns declares itself without the surrounding
tooling needing to know anything about it. `params_type` holds its hyperparameters,
`artifact_param` names the field that points at trained weights, and `discovered` says how to
*find out* the values a field can take - one channel for every vocabulary whose members are
files rather than literals, so `rules` publishes its shelf of rule sets through the same field
(ADR-0037). It holds a callable, not a list, because a shelf can change while the server runs.
That field exists because an artifact id decides whether a
detector can judge at all, and it used to be the only free-text box on a page where every
other closed vocabulary was a dropdown. An operator who does not already know that
`demo_meter` exists cannot guess it, and finds out through a config error after submitting a
run.

The registry is also where a construction failure becomes a named config error. A `TypeError`
from a misspelled param name, or a `ValueError` from a bad value, is re-raised as
`invalid params for detector 'log_burst': min_count must be >= 2 ...`, so the operator learns
which entry is wrong instead of reading a traceback from inside an adapter.

## Config: spec and config

A run is described by a spec: a YAML file if you drive it from the CLI, the same object as
JSON if you drive it from the Compose page or `POST /runs`. `config/demo.yaml` is the one the
repo ships — the walkthrough as a config, so the CLI and the compose `runner` stay exercised
and print the queue the dashboard prints. The file below is the same shape, cut to two sources
and with the `sinks` block spelled out rather than defaulted.

```yaml
sources:                        # a list, so one run can mix modalities
  - normalizer: flow
    source_format: zeek-conn    # asserted by the operator, never sniffed
    path: data/demo/camera_flow.log
  - normalizer: syslog
    source_format: syslog
    path: data/demo/camera_syslog.log
detectors:                      # instances, so one adapter may appear more than once
  - {name: rules, params: {lane: flow}}
  - {name: rules, params: {lane: log}}
alert_engine:
  name: weighted
  params: {bucket_minutes: 5}
sinks:
  - {name: postgres}
```

`sources` being a list is what makes cross-lane correlation possible: each source is routed
to its own normalizer and the events pool into the lanes. Routing is explicit because a text
blob has no intrinsic format, and the normalizer double-checks the format it was told.
Unknown keys are rejected, so a typo in a detection config does not become a detector that
quietly never runs.

There are two types, and the difference between them is a boundary.

| Type | Holds | Used by |
| --- | --- | --- |
| `PipelineSpec` | sources, detectors, alert_engine, assets, sinks | The operator, the API off the wire, the evaluation harness |
| `PipelineConfig` | the above plus `database_url` | The deployment, and the CLI |

`PipelineSpec` has no `database_url`. Where the database lives is a deployment decision, so
the Pipeline API accepts a spec from the caller and supplies the URL from its own
environment. A caller cannot point a run at an arbitrary database.

The same split does a second job. `compute_run`, the in-memory phase, takes `PipelineSpec`
and not `PipelineConfig`, so "a benchmark cannot write to an operator's triage queue" is
checked by the type checker rather than by review. The harness has no field that could hold
a store, so twenty benchmark runs cannot reach one.

## A run, step by step

Two phases, in `pipeline/runner.py`, separated so the result store can be swapped.

`compute_run(spec, inputs)` is pure and touches no storage:

1. Guard the chain. ERRORs raise here, before any work. WARNINGs are carried forward.
2. Build the normalizers, before reading any input, so a bad name or param fails fast. They
   are cached by name *and* params: three syslog sources build one normalizer, but two flow
   sources watching different home networks are two normalizers and must not collapse into
   whichever was built first.
3. Normalize each source and pool the events and rejects. If more than half of a source's
   input was rejected, that is a warning, because a high reject rate usually means the wrong
   normalizer was chosen and it otherwise hides as a large malformed pile.
4. Build the detectors, then refuse any that are untrained. This check needs the built
   object, so it cannot live in the guard. It runs before judging and before any sink.
5. Judge. Each detector receives its lane's events, sorted.
6. Resolve assets in memory. Seeded registrations win; anything else seen in the data
   becomes an auto-discovered stub at medium criticality.
7. Prioritize, and collect any `Diagnosable` notes explaining deliberate silence.

`execute_run(config, ...)` computes, then builds every sink before writing to any of them,
then writes in config order with the system of record first. There is no cross-store
atomicity: a sink that raises fails the run, so a best-effort delivery sink has to swallow
its own errors. Building all sinks first is there because construction used to be
interleaved with writing, and with the default `[postgres, file]` shape a typo in the file
sink's params committed the run to Postgres and then exited 2, so re-running after the fix
duplicated the data.

The refusals, in the order they can happen:

| Check | Failure |
| --- | --- |
| Chain coherence | exit 2 or HTTP 422, nothing done |
| Adapter construction | Named config error, nothing done |
| Untrained detector | Named config error, before judging |
| Sink construction | Error before any sink writes |
| Sink write | Run fails. Earlier sinks may have committed, and the API says so |

## Three planes

| Plane | What it is | Where | Reached by |
| --- | --- | --- | --- |
| Product runtime | The pipeline | `contracts/ normalize/ detect/ alert/ sink/ storage/ pipeline/` | CLI, or the Pipeline API |
| Control plane | Compose, run, read results | `api/` (read on 8000, pipeline on 8100), `web/` (8080) | HTTP |
| Evaluation plane | Judges the pipeline against labelled data | `eval/` | `python -m iotshield.eval`, or an opt-in API router |

The evaluation plane has three constraints, because it judges the product rather than using
it. The dependency arrow points one way: `eval` imports the pipeline and the pipeline never
imports `eval`, so the package can be deleted. It measures the real pipeline, composing the
same `normalize_sources` and `compute_result` the product composes with a fitting step
inserted between them, rather than re-implementing anything. And it is absent by default:
without `IOTSHIELD_EVAL_API` the `/eval` router is never registered, so there is no route to
reach and no OpenAPI entry, rather than a flag checked inside a live handler. The React
dashboard probes for the route and hides its Evaluate mode when it is missing.

## Adding an adapter

Every adapter has the same five parts, whichever port it implements. The per-port recipes,
with real code, are in `docs/guide/normalizers.md`, `docs/guide/detectors.md` and
`docs/guide/alerts.md`.

| Part | Where |
| --- | --- |
| The adapter itself: a class with the port's method shape | That port's package |
| Its capability, as a `CAPABILITY` constant or a `capability(params)` function | The same module |
| A registry entry | `pipeline/registry.py` |
| A case in the port's contract suite | `tests/ports/test_<port>_port.py`, for the three capability-bearing ports; a sink's tests go in `tests/pipeline/test_sinks.py` |
| A name in a run spec | a config file of yours, or the Compose page |

Two of those are less optional than they look.

**The port suite is how swapping stays safe.** Every adapter of a port runs the same
parametrized behavioural tests, which is the only reason a config edit can be trusted.
Joining it is adding a case to a list, not writing a test file.

**If your adapter needs a contract change, stop and discuss it.** Three times a lane has
needed structured data the contract could not hold, and all three times the answer was a
typed vocabulary module (`flow.py`, `meter.py`, `enrichment.py`) rather than a new contract
field. A contract change is a design conversation with an ADR attached, not a task.

One registry detail worth knowing while you are writing the factory: annotate any closed
vocabulary as `Literal[...]`. That is what makes `/modules` publish it as a set of choices,
so a UI renders a dropdown instead of a text box, and it is read from the factory signature
so the vocabulary is declared once.

## What keeps this honest

| Mechanism | What it gives you |
| --- | --- |
| `tests/ports/`, 141 cases | Every adapter of a port passes identical behavioural tests |
| `scripts/check.sh` | pytest, ruff check, ruff format, pyright. CI runs the same script on a pinned interpreter |
| `npm run check` in `web/` | typecheck, lint, vitest, build. Its own CI job |
| 38 ADRs in `docs/adr/` | Numbered and append-only. They record what was rejected, which is usually the more useful half |

State at the time of writing: `1250 passed, 15 skipped`.

## Further reading

| Topic | Where |
| --- | --- |
| Contracts v1 | ADR-0003 |
| Ports, and treating failure as data | ADR-0004 |
| The capability guard | ADR-0014 |
| Pluggable result sinks | ADR-0018 |
| Multi-source ingestion | ADR-0019 |
| Hyperparameters against fitted state, a law for anything that learns | ADR-0022 |
| Lane against payload, and publishing artifact ids | ADR-0035 |
| Per-stage guides | `docs/guide/` |
| Day-to-day developer notes | `CLAUDE.md` |
