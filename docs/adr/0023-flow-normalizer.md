# 23. FLOW-lane normalizer, and the lane vocabulary it needed

Date: 2026-07-20

## Status

Accepted. Detector side: ADR-0024.

## Context

`Lane.FLOW` has existed in the contract since ADR-0003 and has never been used. Two of the
three modalities the product claims — telemetry and logs — are ingested and judged; the
third has never been ingested at all.

Flow is worth adding because it detects things that are *structurally invisible* in the
other two lanes. A compromised IP camera scanning the internet for other victims produces
perfectly normal telemetry (it still reads 22 degrees) and writes no log lines
(compromised firmware does not log about itself). Only its network behaviour betrays it.

A **flow record** is not a packet: it is the summary of one conversation — every packet
sharing a 5-tuple within a window, collapsed into a row of counters. It is what routers
and network sensors actually export, because full packet capture is too large, too
privacy-hostile, and usually unavailable.

Four properties drive everything below:

1. A flow is an **interval** (start + duration); our events are instants.
2. A flow is **bipartite** — it names *two* hosts, and we correlate on one asset.
3. A flow's essential content is **categorical** (who, whom, which port, what outcome),
   not a scalar; only the byte counters are numbers.
4. The unit of anomaly is a **host's behaviour over a window**, not a row. One connection
   to port 23 is nothing; forty, to forty hosts, in ten seconds, is a worm.

## Decision

### D1 — One event per flow record, not a fan-out per metric

The contract's one-value-per-event rule (ADR-0003 D1) makes a multivariate telemetry
reading fan out into one event per channel. Applying that to flows is wrong twice over: it
multiplies an already enormous stream by 5–80x, and — fatally — it destroys the joint
tuple, which *is* the signal. Once a flow is split into separate numeric events you can no
longer ask "how many distinct destinations did this asset contact"; the destination is not
a number and does not survive the split.

The right analogy is the **log** lane, not telemetry. A log event also carries no
meaningful scalar, puts its content in `message`, and rides its lane as one event per
record. Flows are discrete semantic events, not sampled channels.

So: one event per flow; `metric`/`value` carry `bytes_out` (the one scalar meaningful on
its own, and the exfiltration signal); `message` carries an analyst-readable summary; the
structured tuple rides in `metadata`; `raw` is the original line.

A free consequence worth noting: because `value` is populated, the lane-parameterized
`stub_threshold` detector works on the FLOW lane with no new code.

### D2 — The asset is the endpoint inside the home network

Something must decide which of a flow's two hosts is "the asset". Three options were
considered:

- **Always the originator.** Wrong half the time: when an external scanner probes our
  gateway, the originator is the attacker, and we would create an asset for them while
  attributing nothing to our own device.
- **Emit two events, one per endpoint.** Doubles volume, and auto-discovery would register
  **every external IP on the internet** as an asset. The inventory becomes garbage in one
  run.
- **Home-network rule** (chosen). The endpoint inside the monitored ranges is the asset;
  the other is the peer. Both local -> the originator wins, so an internal sweep is
  attributed to the scanner rather than to each victim. Neither local -> the record is
  rejected as a `MalformedRecord`: it is not our traffic.

This is what real network-security tooling does (Zeek's `local_nets`, Suricata's
`$HOME_NET`), and it is what makes `bytes_out` well defined — every counter is re-expressed
from the asset's point of view, so "out" means "left the asset" regardless of which
endpoint the exporter happened to list first.

Default: RFC1918 + link-local space, which is correct for almost every IoT deployment with
no configuration at all. Overridable via `home_networks` because some fleets are publicly
addressed.

### D3 — `timestamp` is the flow's start; the interval is not modelled

Adding a duration/end to `NormalizedEvent` is a golden-rule-1 contract change that ripples
into the ORM, the read API and the dashboard, and would hang a flow-only field on every
telemetry and log event. Not warranted: flow start is what NetFlow calls "first" and Zeek
calls `ts`. `duration_s` rides in the flow metadata.

The approximation is real and stated rather than hidden: a long-lived flow (an hour-long
download, a persistent C2 session) is attributed entirely to the instant it opened, so a
sliding-window detector places its bytes at the wrong end of its life. Real exporters paper
over this with an "active timeout" that splits long flows into chunks; we do not. Revisit
when a detector needs sub-flow resolution — a beaconing/periodicity rule is the likeliest
trigger.

### D4 — One adapter, several dialects, selected by `source_format`

Flow text formats (Zeek, nfdump/NetFlow exports, CICFlowMeter) differ almost entirely in
*column names and units*; every one carries the same 5-tuple plus counters. A normalizer
per format would triplicate orientation, reject handling and event construction to vary a
lookup table.

So a **dialect** is declarative data — which source column feeds which canonical field,
how timestamps are written, what means "unset" — and the dialect is chosen by
`source_format`. This needed no new machinery: the guard already compares a source's format
against a *set* of accepted `FormatToken`s. It is the first normalizer to declare more than
one, which also exercises the orchestration UI's multi-format branch for the first time.

Two ship: **`zeek-conn`** (the de-facto standard in network security monitoring, and what a
real deployment plausibly already has) and **`flow-csv`** (a canonical header-driven CSV
with ISO timestamps — the FLOW lane's `stub-csv`: the permanent reference dialect the port
suite runs against). Adding NetFlow or CICFlowMeter later is a `DIALECTS` entry.

Provenance (rewrite, not wrap — golden rule 3): team1-lognorm holds the reference set's
only flow parsing (`zeek_parser.py`'s `#separator`/`#fields` handling, `unsw_nb15_parser.py`'s
field aliasing). Both flatten a flow into `{device_id, type, value}` and discard the peer
endpoint — precisely what a flow detector needs — so their ideas are rewritten against our
contract.

### D5 — The FLOW lane gets a typed vocabulary module (`src/iotshield/flow.py`)

D1 puts the tuple in `metadata`, which is `dict[str, Any]` and therefore unchecked. If the
normalizer writes `peer_port` and a detector reads `dst_port`, **nothing raises** — the run
simply produces no detections, indistinguishable from a healthy run that found nothing.
That is the silent-emptiness failure the compatibility guard exists to prevent one layer up.

Telemetry and logs needed no such module, and the reason is precise: their entire payload
fits in `NormalizedEvent`'s own typed fields (`metric`/`value`, `severity`/`message`), so
their normalizers and detectors are coupled through *attributes* that pydantic and the type
checker already check. **Flow is the first modality that outgrows the contract's field set.**

`flow.py` is that lost type safety put back: `FlowRecord` (frozen), `Direction`, the
orientation rule, and `to_metadata`/`from_event`. The untyped hop is exactly one function
call wide, and it is tested. It is deliberately *not* a contract in `contracts/` — these
fields mean something to one lane only, and hanging them on every telemetry and log event
(and on the events table, forever NULL) is the cost of pretending otherwise. The day two
lanes need the same structure is the day it graduates, with an ADR.

### D6 — Header lines are schema, not data

The Normalizer port requires every non-blank line to become events or a `MalformedRecord`.
A `#fields` directive or a CSV header row is neither — it *describes* records rather than
being one, the same category as the blank lines the port already skips. Data arriving
before a usable header **is** a reject: we refuse to guess column order.

### D7 — Rejects are specific

Flow data has its own junk: `-` for unset Zeek fields, portless ICMP, IPv6 where IPv4 was
expected, and the "neither endpoint is local" case. Each gets its own reason, because
ADR-0017's high-reject-rate warning is the operator's only clue that `home_networks` is set
wrong — a misconfiguration that would otherwise present as a completely silent empty run.
Unset *counters*, by contrast, become 0 rather than a reject: a rejected connection
legitimately has no byte counts, and quarantining those rows would throw away exactly the
scan traffic the lane exists to catch.

### D8 — Normalizers become parameterizable

`home_networks` needs to be configurable, and normalizers were the only one of the four
ports that could not be. That was an asymmetry rather than a decision: `build_normalizer`
hardcoded an empty mapping while `/modules` *already* introspected normalizer factory
signatures for parameters that could never arrive.

`SourceSpec` gains `params`, additive with a default. Not a contract change under golden
rule 1 (`contracts/` is untouched), but it *is* a public-schema change — `PipelineSpec` is
a FastAPI request body and appears in the OpenAPI document — so it is recorded here. Every
config file and API request written before it stays valid.

One consequence worth stating: the runner's normalizer cache now dedupes on name **and**
params. Deduping on name alone would build one normalizer and reuse it for two flow sources
watching different home networks, silently attributing the second source's traffic with the
first's ranges.

### D9 — The orchestration UI renders detectors as the list they always were

Not strictly flow work, but this change forced it. `PipelineSpec.detectors` is a
`list[DetectorConfig]`, and the compose page rendered it as a multiselect over *names* — a
set. Names are unique and the parameter widgets were keyed by name, so an operator could
never configure one adapter twice with different params. Every preset-bearing detector was
therefore limited to one preset per run from the UI, while YAML and the API had always
allowed several. With three flow presets arriving, that stopped being a papercut.

The composer now mirrors the *source* composer, which had already solved this: a count plus
per-index rows, keyed by index rather than by name. `log_burst` gets the fix for free, with
no rename and no migration (`method` is persisted data).

Two smaller things came with it:

- **`ParamInfo.choices`**, read off `Literal[...]` annotations on factories. Previously any
  non-numeric param rendered as a free-text box — an operator typed `preset` and `lane`
  from memory, and a typo surfaced only as a guard error. Meta-tests pin each `Literal`
  against the vocabulary it mirrors (`Lane`, each `PRESETS` table) so a new member cannot
  become silently unreachable from the UI.
- **A guard WARNING for two identical detector entries.** The multiselect structurally
  prevented duplicates; a list does not, and two identical detectors judge the same events
  twice — which the alert engine's `volume` factor reads as independent corroboration
  (ADR-0020 D4). Same adapter with *different* params stays silent: that is the legitimate
  case the list form exists to enable.

## Consequences

- The FLOW lane is real. `demo_flow.yaml` ingests a Zeek `conn.log` in which a compromised
  camera sweeps 41 hosts for telnet; the busy-but-boring gateway (30 flows, one
  destination) correctly stays silent, and a flow between two foreign hosts is quarantined
  rather than dropped.
- **No contract change, no guard change, no storage change.** `Lane.FLOW` was already in
  the enum, `formats_in`/`lanes_in` are generic, `EventRow.metadata` is already JSON/JSONB,
  and the guard produces correct flow-lane coverage warnings with zero edits. Adding a
  whole third modality touched no core machinery — the ports-and-adapters claim, cashed in.
- No new runtime dependency: `ipaddress` and the standard library. The numpy-only surface
  (ADR-0012) is untouched.
- Registering `flow` exposed a latent bug in the compose page: the source picker defaulted
  to the alphabetically-first normalizer, and `flow` sorts before `stub`, so every fresh
  session would have opened on a flow chain fed telemetry CSV. It now selects the
  normalizer that accepts the reference format — the same lesson the *detector* picker
  learned in ADR-0020, applied one widget over.
- **Asset identity across lanes is an open problem.** A flow identifies a device by IP
  because that is the only identity a flow record carries, while telemetry and syslog
  identify by name/hostname. `demo_trimodal.yaml` sidesteps this by keying all three
  sources on the camera's IP, which is a legitimate deployment choice but not a general
  answer. Reconciling hostname-keyed and IP-keyed identities needs an **asset-aliasing
  layer** (an `Asset` that owns several natural keys) that we have deliberately not built.
  It belongs with the deferred cross-run asset-enrichment work, and until it exists,
  cross-lane correlation involving flows requires the sources to agree on an identifier.
- Deferred: NetFlow/IPFIX and CICFlowMeter dialects (each a `DIALECTS` entry); sub-flow time
  resolution (D3); asset aliasing; converting `log_burst` to the same
  one-registry-name-per-variant shape the UI fix makes possible.
