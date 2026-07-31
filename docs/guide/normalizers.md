# Normalizers

A normalizer turns raw input text into `NormalizedEvent`s. It is the only layer that
knows any input format exists, and it is the first code in the system that touches
bytes somebody else authored.

Read `docs/guide/architecture.md` first if you have not. This guide assumes you know
what a port, a contract and a capability are.

## What you get and what you owe

The port is one method:

```python
def normalize(self, text: str, *, source_format: str, origin: str) -> NormalizeResult
```

`NormalizeResult` holds two lists: `events` and `malformed`. Both are populated by the
same call, because rejects are not an error path.

Three laws, enforced by `tests/ports/test_normalizer_port.py`:

1. **Nothing is silently dropped.** Every non-blank line becomes events or one
   `MalformedRecord`. Blank lines are skipped, and so are header rows, because neither
   is a record.
2. **`raw` is verbatim.** On events and on rejects, `raw` is the input text exactly as
   it arrived.
3. **An unsupported `source_format` raises.** That is a config error, not a data error.

You do not have to enforce the contract's own rules. Timezone-aware timestamps, finite
values, required fields, unknown fields: hand the field mapping to
`NormalizedEvent.model_validate` and let it reject, then capture its message. Never
re-implement a contract rule in an adapter.

## The five that ship

| Name | Formats | Lane | Shape |
|---|---|---|---|
| `passthrough` | `normalized-jsonl` | param | line-wise |
| `stub` | `stub-csv` | telemetry | line-wise |
| `syslog` | `syslog` | log | line-wise |
| `flow` | `zeek-conn`, `flow-csv` | flow | whole-file |
| `meter` | `eoa-meter-csv`, `sensipro-hourly-csv` | telemetry | whole-file |

Try them. The first three are **generated**, not hand-authored, so what each one contains is
declared in one module docstring rather than hidden in the bytes:

```
uv run python scripts/make_demo_data.py
```

| Input | Format | What it exercises |
|---|---|---|
| `data/demo/camera_flow.log` | `zeek-conn` | a `#fields` directive instead of a header row, orientation against the home networks, a 4.2 MB flow |
| `data/demo/camera_syslog.log` | `syslog` | both RFC dialects in one file, plus two lines that genuinely cannot parse |
| `data/demo/camera_telemetry.csv` | `stub-csv` | the reference format: three devices on one clock |
| `data/demo/demo_meter_history.csv` | `eoa-meter-csv` | a cumulative counter differenced into hourly consumption — the one hand-authored file here, and the sole input to the committed meter artifact, so the generator leaves it alone |

Upload one of them on the dashboard's **Operate -> Compose** page, or name it in a config of
your own. The walkthrough is driven from that page, which posts the file's *content* rather
than a path on the server, so a run needs no YAML at all; `config/demo.yaml` is the same three
sources as a config, for a shell or a scheduler, and is the only run config the repo ships.

`passthrough` ships no demo input. Its input is our own `NormalizedEvent` JSONL, which only
occurs where a partner system already emits it, so the worked example is its case in the port
suite rather than a file you can point at something.

## Line-wise or whole-file

This is the first decision when you write one.

**Line-wise** means one input line maps to one output, and the decision needs no other
line. Use `normalize/_linewise.py`:

```python
def _parse_line(line: str) -> dict[str, Any]:
    parts = line.split(",")
    if len(parts) != 4:
        raise LineParseError(f"expected 4 fields, got {len(parts)}")
    return {"timestamp": parts[0], "asset_id": parts[1], ...}

class MyNormalizer:
    def normalize(self, text, *, source_format, origin):
        return normalize_linewise(
            text,
            source_format=source_format,
            expected_format=SOURCE_FORMAT,
            origin=origin,
            parse_line=_parse_line,
        )
```

`normalize_linewise` owns all three laws: the format guard, the blank skip, verbatim
`raw`, and the translation of both `LineParseError` and pydantic's `ValidationError`
into rejects. Return the field mapping minus `raw`; the loop knows that.

`stub.py` (61 lines) and `passthrough.py` (124) are the two to copy.

**Whole-file** means you need state across lines, or all the lines at once. Then you
write your own loop, but you still import `split_lines` from `_linewise` (see
"Record boundaries" below) and you still translate every failure into a
`MalformedRecord` rather than raising.

`flow.py` needs state because a Zeek file declares its columns in a directive partway
down. `meter.py` needs the whole file because it differences consecutive readings.

## Record boundaries

Do not use `str.splitlines()`. It breaks on eight characters besides real newlines,
including U+2028, which is legal unescaped inside a JSON string, and form feed, which
appears in real syslog payloads. Splitting there turns one record into two, breaks the
verbatim-`raw` law, and produces a reject that blames the operator's data for our
parsing.

Use `normalize._linewise.split_lines`, which splits on `\r\n|\r|\n` and nothing else. Both
whole-file readers share it, and so does the line-wise loop, so no reader can disagree with
another about where a record ends.

## Timestamps

Timestamps are the field most likely to be silently wrong, so the rules are strict.

- **Always aware, always UTC.** The contract rejects naive datetimes. Attach the zone
  in the adapter, or let the contract reject the record.
- **Never fabricate.** An unreadable date is a reject. `datetime.now()` at the trust
  boundary invents data.
- **Never trust a bare number.** The contract rejects epoch numbers explicitly, because
  pydantic's lax mode would read a column-shifted CSV line as a 1970-dated event
  instead of a reject.
- **Say when a zone is assumed.** RFC 3164 carries none, so `syslog` attributes UTC and
  ADR-0011 records the decision.
- **Naive local time needs a DST check.** One local hour never happens each spring and
  one happens twice each autumn. `zoneinfo` resolves both silently, placing a reading up
  to an hour from where it belongs. `meter._localize` compares the two `fold`
  interpretations and quarantines the reading if they disagree.
- **Convert to UTC before you subtract.** CPython short-circuits `datetime` subtraction
  when both operands share the same `tzinfo` object, so subtracting two zone-attached
  local times compares wall clocks. That lost one hour of meter consumption per meter
  per year, silently (ADR-0033).

If your events describe an interval, decide which end the timestamp names and document
it. Flow events are stamped at the flow's **start**; meter consumption events at the
interval's **end**. Neither is inferable, so the meter carries `interval_hours` in
metadata.

## Rejecting well

A `MalformedRecord` carries `raw`, `error`, `source_format` and `origin`. The `error`
is read by an operator on the Data health page, so write it for them:

- name the field and show the value: `flow is not a usable number: 'n/a'`
- say what was expected: `header is missing required column(s): protocol`
- say what to do when there is something to do: `give each utility its own device id`
- do not suggest a fix that makes things worse. The flow orientation error deliberately
  does not say "widen home_networks", because `0.0.0.0/0` auto-discovers the whole
  internet as assets.

Two failure modes to guard against specifically:

**Silence that reads like success.** If a misconfiguration makes your adapter emit zero
events *and* zero rejects, the run reads as a clean night. `meter._report_cadence` exists
for exactly this: a 15-minute export against an hourly dialect produced nothing at all,
so one reject per meter is emitted naming the observed cadence. At the run level the
pipeline warns when a source's reject rate passes 50%, which catches the wrong
normalizer entirely.

**An exception instead of a reject.** A bad record must never escape `normalize()`; one
corrupt row would kill a multi-source run. The ones that have actually escaped:
`json.JSONDecodeError` (a `ValueError`, which the shared loop does not catch),
`OverflowError` from `int(float("1e400"))`, and `UnicodeDecodeError` from a truncated
escape in a Zeek directive. Catch broadly at the row level, and reject.

## Non-finite numbers

The contract guards `value` with `FiniteFloat` and walks `metadata` rejecting nested
nan/inf. But a number you parse into a local variable before it reaches metadata is not
guarded yet. Check `math.isfinite` yourself.

This is not cosmetic. `json.dumps` emits the bare tokens `NaN` and `Infinity`, which are
not valid JSON. SQLite stores them; Postgres rejects the transaction, and a run is one
transaction, so one bad number destroys every event, detection and alert in the run.

## Adding a dialect to `flow` or `meter`

If the format differs only in column names, delimiter and timestamp convention, it is a
dialect, not an adapter. This is the common case for flow exports (NetFlow CSV,
CICFlowMeter) and for meter platforms.

1. Add a `Dialect` entry in `normalize/flow.py` or `normalize/meter.py`:

   ```python
   NETFLOW_CSV = Dialect(
       name="netflow-csv",
       columns={"ts": "first_seen", "src_ip": "src_addr", ...},
       parse_timestamp=_parse_iso,
   )
   ```

2. Register it in that module's `DIALECTS` dict. The `CAPABILITY` is built from
   `DIALECTS`, so the new format token appears automatically, and the compatibility
   guard and `/modules` pick it up with no further change.

3. Add a case to `tests/ports/test_normalizer_port.py`. This is not optional: a
   meta-test asserts every format a registered normalizer declares has a port case, so
   skipping it fails the suite by name.

4. If the corpus generator should be able to emit the format, add a `Renderer`. A
   renderer is the inverse of a normalizer and a meta-test composes the two, which is
   how a whole corpus silently scoring zero was once caught.

That is the whole change. No new module, no registry entry.

## Adding a new normalizer

Five parts, and the fifth is the one people forget.

1. **The adapter.** A new module in `normalize/`. Pick line-wise or whole-file as above.
   Export a `SOURCE_FORMAT` constant (adapters advertise their formats as module
   constants, not on the port).

2. **The capability**, co-located in the same module:

   ```python
   CAPABILITY = Capability.of(
       consumes={FormatToken(SOURCE_FORMAT)},
       produces={LaneToken(Lane.TELEMETRY)},
   )
   ```

   Add a `PayloadToken` if your events carry a typed vocabulary in `metadata`. The
   token's value must be that vocabulary module's `METADATA_KEY`; a meta-test pins this,
   so a declaration cannot drift into a string no writer produces.

   If your lane genuinely cannot be known statically (the pass-through case, where the
   lane is in the data), make it a config parameter and write a
   `capability(params) -> Capability` function instead. Do not declare all three lanes:
   the guard would warn about uncovered lanes on every run and people would stop reading
   it.

3. **The registry entry** in `pipeline/registry.py`:

   ```python
   NORMALIZERS = {
       ...
       "mine": AdapterSpec(_my_normalizer, static(_MY_CAPABILITY)),
   }
   ```

   Annotate closed vocabularies in the factory signature as `Literal[...]`, so `/modules`
   publishes them as choices and the compose page renders a dropdown instead of a text
   box.

4. **Export it** from `normalize/__init__.py`.

5. **Join the port suite.** Add a `NormalizerCase` with a fixture whose good and bad line
   counts you know, and write the expected counts as a comment explaining each. The
   existing fixtures are worth reading as a checklist of what to include: a non-UTC
   offset, a blank line, a naive timestamp, a missing field, a non-numeric value, and
   something structurally wrong.

Then `scripts/check.sh`.

## Config surface

A source is routed by its declared format, never by sniffing:

```yaml
sources:
  - normalizer: flow
    source_format: zeek-conn
    path: data/demo/camera_flow.log
    params:
      home_networks: [10.0.0.0/8, 192.168.0.0/16]
```

Normalizers take `params` like every other port. The runner caches them by name **and**
params, so three syslog sources build one normalizer, but two flow sources watching
different home networks stay two.

A bad param value raises at construction, before any input is read, and surfaces as
`invalid params for normalizer 'flow': ...`. That is deliberate: a bad config would be
wrong for every record, so it fails loudly rather than becoming a million rejects.

## Where to look

| | |
|---|---|
| The port and its laws | `src/iotshield/normalize/port.py` |
| The shared line-wise loop | `src/iotshield/normalize/_linewise.py` |
| Smallest working adapter | `src/iotshield/normalize/passthrough.py` |
| Dialects, header state, orientation | `src/iotshield/normalize/flow.py` |
| Cross-row arithmetic, clocks | `src/iotshield/normalize/meter.py` |
| The port suite | `tests/ports/test_normalizer_port.py` |
| Clock and boundary regressions | `tests/adapters/test_normalizer_boundaries.py` |
| The demo inputs, and the scenario they encode | `scripts/make_demo_data.py` |
| Why syslog is a rewrite, not a wrapper | `docs/adr/0011-syslog-normalizer-integration.md` |
| Dialects and flow orientation | `docs/adr/0023-flow-normalizer.md` |
| A normalizer whose lane is a param | `docs/adr/0025-passthrough-normalizer.md` |
| Meter differencing, gaps, rollbacks | `docs/adr/0030-meter-detector-and-scikit-learn.md` |
| Record boundaries and DST | `docs/adr/0033-pre-handover-review-fixes.md` |
| Why the payload is its own axis | `docs/adr/0035-the-lane-is-not-the-whole-shape.md` |
