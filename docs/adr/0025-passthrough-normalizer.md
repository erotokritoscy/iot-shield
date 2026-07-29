# 25. Pass-through normalizer

Date: 2026-07-24

## Status

Accepted (discussed and approved by Andreas). Part of the weeks-3-4 plan
(`docs/design/weeks-3-4.md` §5.2), Bucket A.

## Context

Every normalizer so far *parses* a wire format (CSV columns, syslog PRI headers, Zeek TSV).
But some sources already arrive in our canonical shape: a prior iotshield run, a partner
that adopted the `NormalizedEvent` contract, an ETL job that normalized elsewhere. The
tempting move — a bypass around the normalize stage — would puncture the trust boundary
(ADR-0004): unvalidated data would enter the pipeline, and malformed input would have no
capture path.

The correct framing is that **normalization is not "parsing" — it is "turn untrusted input
into a validated event, capturing what fails."** Parsing is only the most common way to do
that. For a source that already arrives structured, the job collapses to
**validate-and-envelope**, which is a new adapter *behind* the existing port, not a bypass
of it.

One thing makes this adapter unlike the others, and it is the whole design content: a
pass-through record carries its **own lane** in `source_type`. The lane is in the *data*.
But the compatibility guard (`pipeline/compat.py`) reasons about a normalizer's **static**
declared capability, without constructing it — and its coverage check warns when a produced
lane has no consuming detector. A pass-through that statically declared "produces all three
lanes" would emit false coverage warnings on every run that does not happen to configure a
detector for each lane, eroding the guard's trustworthiness (ADR-0014's whole purpose).

## Decision

Add `PassThroughNormalizer` (`normalize/passthrough.py`), registered as `passthrough`,
accepting the `normalized-jsonl` format: one JSON object per line, each matching
`NormalizedEvent`. It delegates to the shared `normalize_linewise` loop, so the port's laws
(conservation, verbatim `raw`, unsupported-format raises, `ValidationError → MalformedRecord`)
are reused rather than re-implemented; its only per-line work is `json.loads` plus the lane
handling below. Contract validation (aware-UTC timestamps, finite values, required fields,
`extra="forbid"`) is left entirely to the contract.

**The produced lane is a config parameter.** `passthrough` takes a `lane` param
(`telemetry` | `log` | `flow`, default `telemetry`), and its capability is *param-driven*
(a `CapabilityProvider`, not `static`) — the first **normalizer** to be so, mirroring the
`stub_threshold` detector, the first param-driven detector capability. The capability
reports exactly the declared lane, so the guard stays accurate. The parameter is then also a
**validation**: each record's `source_type` is *filled from* it when absent (a source may
omit the redundant field) and *checked against* it when present (a record on the wrong lane
is a misrouted source, quarantined rather than silently admitted to the wrong lane).

Two smaller provenance rules:

- `raw` is the verbatim JSON line (the port requires the event's `raw` to be this stage's
  input; the shared loop sets it, overriding any `raw` inside the JSON — still visible within
  the line itself).
- The event's `source_format` is *preserved* when the record carries one — it records where
  the data originally came from (e.g. `partner-scada`), which is distinct from the pipeline
  `source_format` (`normalized-jsonl`) that selected this normalizer — and defaults to ours
  only when absent, so the required field is always satisfied.

**Single-lane in v1.** A source declares one lane. A single JSONL file mixing lanes is not
supported and is not needed (a `lanes` extension can follow the first real demand); one
source, one lane matches how multi-lane runs are already built — multiple single-lane
sources pooled (ADR-0019).

No contract change; no ADR beyond this one is required, since this is an adapter behind an
existing port. It ships with a port-suite case, an adapter test, the capability meta-test
(declared lane follows params, verified against a built instance), a demo
(`config/demo_passthrough.yaml` + `data/demo/demo_passthrough.jsonl`), and a count-pinned
runner test.

## Consequences

- The narrowest front door for clean data now exists: a source that already speaks the
  contract is one config entry, no parser.
- Because it is the smallest adapter that honours every port law, it is the **hello-world**
  for the contributor guide (weeks-3-4 §5.6): the least code a working normalizer can be.
- A reusable precedent is set — **produced-lane-as-a-param** for a normalizer — for any
  future adapter whose output shape depends on data the static guard cannot see.
- The `passthrough` normalizer is the inverse shape of the file/JSONL sink (ADR-0018): the
  sink dumps contracts to JSON, this reads them back. Round-tripping a `NormalizedEvent`
  through it returns an equal event (modulo `raw`, which the port resets by design).
