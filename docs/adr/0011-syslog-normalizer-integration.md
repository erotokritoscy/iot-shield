# ADR 0011: First module integration — syslog normalizer, by rewrite not wrapper

Date: 2026-07-09 · Status: accepted (approved by Andreas)

## Context
The platform's premise is that external modules plug in behind our ports. The
first integration is a deliberate lifecycle test: prove the adapter path works
with real student code, on the simplest module first (a normalizer), before the
harder detector (team3). We chose team4-log-normalizer's **syslog** parsing.

Reading team4's core surfaced a structural problem. Our Normalizer port has six
mechanically-enforced laws (tests/ports/); team4's core violates four of them by
design, because it is built to be best-effort and never fail:

- it **drops** unrecognized lines (`if log: results.append`), where we require
  every non-blank line to become an event *or* a MalformedRecord (ADR-0004);
- it **fabricates** `datetime.now()` for an unreadable date, destroying the
  evidence that the record was bad;
- it emits timezone-**naive** timestamps, which our contract rejects (golden
  rule 4); and
- its CSV parsers store `str(dict(row))` as `raw`, not the original text.

These losses happen *inside* team4's solution, before its output boundary — the
only surface an adapter can act on. An adapter can bridge a **representational**
mismatch (names, types, formats: the information survives, translation is
lossless) but not a **destructive** one (the module discarded or fabricated the
information our contract needs). So no adapter over team4's output could satisfy
our port.

## Decision
**Rewrite the idea against our contract; do not wrap or import team4.** This is
the documented fallback for a contract-unaware legacy module (golden rule 3
permits "selectively re-implemented against our contracts"). We keep the
*wrap-first* default: the choice is made per module/per format by the test
above, not as a blanket policy. `src/iotshield/normalize/syslog.py`:

**Borrowed from team4** (`.../core`, cited in the module docstring): the two
syslog line shapes (RFC 3164 + RFC 5424) and the severity taxonomy (numeric PRI
levels; message-keyword heuristics). team4's *domain knowledge* is the reusable
asset.

**Rewritten to our laws:** unrecognized lines and unreadable dates become
MalformedRecords (never dropped, never a fabricated "now"); the original line is
`raw`; events land in the **LOG lane** (`value`/`metric` unset — a syslog record
has no numeric reading, so the telemetry detector correctly ignores them).
Events carry the fine-grained dialect (`syslog-rfc5424` / `syslog-rfc3164`);
rejects carry the incoming `syslog` (the dialect is unknown precisely because
the line did not parse).

### D2 — timezone policy: attribute UTC to zone-less input
RFC 3164 timestamps carry no zone (and no year); our contract demands aware UTC.
The gap is in the *data*, not the code, so the rewrite cannot dissolve it — it
must choose. We **attribute UTC** to any zone-less timestamp (and the current
year to a bare RFC 3164 date), documented as an explicit adapter assumption. An
RFC 5424 timestamp that carries an offset keeps it and is normalized to UTC by
the contract; the demo data is RFC 5424 so it is correlation-correct by
construction, leaving assume-UTC as the scoped fallback for legacy lines. A
per-source configurable zone is the future-proof upgrade (deferred).

### D4 — scope: syslog only for v1
`network`/`web` are also line-based and would extend the same adapter cheaply,
but v1 proves the lifecycle on one format. The CSV formats (`sensor`/`windows`)
are deferred: they need per-row provenance and metric fan-out, and team4's
sensor CSV is a *worse* duplicate of our telemetry stub rather than a new
capability.

## Consequences
- We can now normalize logs, not just telemetry: `--config config/demo_syslog.yaml
  --input data/demo/demo_syslog.log` (compose: `docker compose run --rm runner …`).
- The adapter joins the port suite as the `syslog` case — the swap-safety net now
  guards two normalizers.
- This sets the precedent and the rule for every future integration: wrap when
  the module's output preserves what our contract needs, rewrite (with cited
  provenance) when it doesn't. Adaptability is the module author's responsibility,
  made legible by this record.
- Known limitation (inherited from team4): RFC 5424 STRUCTURED-DATA containing
  spaces folds into the message; the common nil form ("-") parses cleanly.
- Detections are unchanged by a syslog run (LOG lane, telemetry-only detector) —
  by design; detector activation is what the next integration (team3) proves.
