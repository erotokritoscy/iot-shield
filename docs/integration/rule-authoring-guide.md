# Writing a rule for iotshield

Hi Antonia,

This is the field list I promised in the integration letter (§6), plus the recipe for adding
a rule. Everything your rules asked for is in place: the detector is built, your three
observable rules are ported with their MITRE mappings intact, and your threat knowledge now
fills the "threat class" and "recommended action" fields on the final ranked alert.

Adding a detection from here is writing one file. No Python, no rebuild.

---

## 1. What a rule looks like

```yaml
id: high-upload-volume # stable; appears in the alert as "per rule:high-upload-volume"
title: High Outbound Upload Volume
lane: flow # which kind of data: flow, log, or telemetry
description: >-
  A single network conversation in which the device sent more than 10 KB outbound.
one_line: Outbound bytes on one flow exceed 10000. # the analyst-facing headline
severity_hint: medium # low | medium | high | critical
confidence: medium # low | medium | high

detection:
  selection:
    flow.bytes_out:
      operator: ">"
      value: 10000
  condition: selection

mitre:
  - tactic: Exfiltration
    technique: Exfiltration Over C2 Channel
    technique_id: T1041

suggested_actions:
  - Review the source device for unusual outbound traffic.
  - Check whether the destination is expected.
```

Drop it in `src/iotshield/detect/rules/library/` to make it part of the product, or in a rule
set of your own on the deployment's shelf (`config/rules/<your-set>/`, see
`config/rules/README.md`), which a run selects with `ruleset: <your-set>`. Either way it is
picked up automatically — the shelf is read per request, so a rule set added while the API is
running appears without a restart.

`ruleset` names a rule set on that shelf; it is not a path (ADR-0037). If your rules live
somewhere else entirely, point `IOTSHIELD_RULES_PATH` at the directory that holds them.

**Your existing rule files work almost unchanged.** The grammar is yours — selections,
operators, `condition` — and the extra keys you added for us (`rule_type`,
`integration_status`, `signal_basis`, `severity`, `correlation`) are all accepted. The one
thing that must change is the **field names on the left**, because they refer to your
canonical event model and we have a different one. Section 2 is that list.

Two keys behave slightly differently here, and neither needs anything from you:

- `correlation.group_by` is **accepted and ignored**. Our normalizers decide which device an
  event belongs to when it is ingested (for network data, by working out which endpoint is
  ours), so a rule does not choose. You can leave it in or take it out.
- `severity` is treated as your old alias for `severity_hint`. Only `severity_hint` is read.

---

## 2. The field list

These are the only names allowed on the left of a comparison. Anything else is refused when
the rule loads, with a suggestion — a rule that quietly matched nothing would be far worse.

**You can always ask the system rather than trusting this page:**

```
uv run python -m iotshield.detect.rules
```

That prints the table below straight from the code, along with every rule currently shipped.

### Every lane

| Field | Kind | Meaning |
| --- | --- | --- |
| `severity` | tier | The event's severity: `debug` < `info` < `warning` < `error` < `critical` |
| `source_format` | text | Where the record came from, e.g. `syslog-rfc5424`, `zeek-conn` |

### `lane: flow` — network conversations

| Field | Kind | Meaning |
| --- | --- | --- |
| `flow.bytes_out` | number | Bytes sent **by** the device |
| `flow.bytes_in` | number | Bytes received by the device |
| `flow.packets_out` | number | Packets sent by the device |
| `flow.packets_in` | number | Packets received by the device |
| `flow.duration_s` | number | How long the conversation lasted, in seconds |
| `flow.peer_port` | number | Port on the **other** end — the service the device **dialled**, on an outbound connection |
| `flow.asset_port` | number | Port on the **device's** end — the service the device **exposed**, on an inbound connection |
| `flow.peer_ip` | text | Address of the other endpoint |
| `flow.protocol` | text | Transport protocol, e.g. `tcp` |
| `flow.service` | text | Application service as observed, e.g. `smb`, `ssl` |
| `flow.state` | text | Connection state as the exporter saw it, e.g. `S0`, `SF` |
| `flow.direction` | text | `outbound`, `inbound`, or `internal` |

> **The one trap worth reading twice.** Every flow field is written from the **device's**
> point of view, not the packet capture's. `bytes_out` is what the device sent, regardless of
> which column the exporter happened to put it in — that is the point, because it means a
> rule never has to re-derive which endpoint mattered.
>
> The consequence for ports: **a service port lands on `peer_port` for a connection the
> device made, and on `asset_port` for one it received.** "Telnet" is
> `flow.peer_port == 23` outbound and `flow.asset_port == 23` inbound. I got this wrong in
> my own first draft of `inbound-remote-admin`; the rule read perfectly and could never have
> fired.

### `lane: log` — log lines

| Field | Kind | Meaning |
| --- | --- | --- |
| `log.message` | text | The message text (the parsed body, not the whole raw line) |

Your OpenSSH rules relied on `event.type` / `event.activity` / `event.outcome`. Those come
from your normalizer's template table, which classifies each line semantically. Ours keeps
the message text and the severity and classifies nothing, so a log signature here matches the
text — which is why `ssh-possible-break-in` reads:

```yaml
log.message:
  operator: contains
  value: POSSIBLE BREAK-IN ATTEMPT
```

### `lane: telemetry` — sensor readings

| Field | Kind | Meaning |
| --- | --- | --- |
| `telemetry.metric` | text | Channel name, e.g. `Pressure` |
| `telemetry.value` | number | The reading itself |

---

## 3. Operators

Exactly the seven your engine has. Which ones apply depends on the field's kind, and a
mismatch is refused when the rule loads rather than crashing a run.

| Operator | Works on | Note |
| --- | --- | --- |
| `==`, `!=` | number, text, tier | On text, **exact** including case |
| `>`, `<`, `>=`, `<=` | number, tier | On a tier, compares rank: `severity >= error` means what you mean |
| `contains` | text | Substring, **case-insensitive** |

Two behaviours that differ from your engine, both deliberate:

- **A missing value never matches, whatever the operator.** In your engine
  `flow.service != "smb"` is true for an event with no service at all; here it is false.
  Absence is "cannot say", not a positive claim.
- **`contains` ignores case.** Where casing varies between data sources, prefer `contains`
  over `==`.

There is no regular-expression operator, on purpose: `contains` covers the signatures we
need, and a regex written in YAML is one typo away from matching everything or running
pathologically slowly, with nothing between it and a live run.

### Conditions

```yaml
condition: selection # one selection
condition: sel_a AND sel_b # both must match
condition: sel_a OR sel_b # either
```

Flat — no parentheses, no `NOT`, no mixing `AND` with `OR` in one condition (that is
refused). If you need more, it is two rules. Fields *within* one selection are always ANDed,
same as yours.

---

## 4. What happens to your rule after it fires

Worth knowing, because it explains why `severity_hint` and `confidence` both matter.

1. **Matches become one finding.** Matches of the same rule on the same device within five
   minutes collapse into a single finding carrying the count — so a rule firing forty times
   in a burst produces one queue entry saying "fired 40 times", not forty entries. Our alert
   engine treats separate findings as *corroborating evidence*, so restating one fact forty
   times would wrongly inflate the alert's priority.

2. **Your two judgments set where it lands.** Severity sets the base, confidence scales it:

   | | confidence: low | confidence: medium | confidence: high |
   | --- | --- | --- | --- |
   | **severity: low** | 7 · LOW | 17 · LOW | 25 · LOW |
   | **severity: medium** | 21 · LOW | 30 · MEDIUM | 38 · MEDIUM |
   | **severity: high** | 40 · MEDIUM | 47 · MEDIUM | 53 · HIGH |
   | **severity: critical** | 51 · HIGH | 58 · HIGH | 63 · HIGH |

   (Priority out of 100 for one finding on a normal-criticality device with nothing
   corroborating it.) The alert still climbs from there if the device is critical, if another
   data source agrees, or if the rule fires repeatedly.

   Note that **a single rule cannot reach the top of the queue** — even critical at high
   confidence lands at HIGH on its own. That is deliberate: a rule states its case, and the
   pipeline decides the ranking. It is also why `confidence` is worth using rather than
   always writing `high`. A rule you are only partly sure about can ship at low confidence
   and sit in the queue without dominating it.

3. **Your MITRE mapping and actions land on the alert.** The technique becomes the alert's
   threat class; the actions become its recommended action. When several findings correlate
   into one alert, the most specific claim names it and the **actions from all of them are
   combined** — so an analyst looking at a device with both a failed-login burst and a bulk
   transfer gets both sets of next steps.

`mitre` is optional. If a signature is a real operational signal rather than an attack (a
critical log line, say), leave it out and the rule's `title` becomes the threat class. That
is more honest than inventing a technique id.

---

## 5. Checklist for a new rule

- [ ] `lane` set, and every field on the left comes from that lane's list in §2.
- [ ] Only real observable fields — never a dataset label.
- [ ] `severity_hint` and `confidence` both set, and `confidence` is honest.
- [ ] `one_line` says what real-world signal it looks for, including the threshold.
- [ ] `mitre` present if it is an attack technique; omitted if it genuinely is not.
- [ ] `suggested_actions` says what to *do*, not what happened.
- [ ] `uv run pytest tests/adapters/test_rules.py` passes — the library is validated by the
      same checks your `check_rule_integration_contract.py` makes, so a malformed rule fails
      there rather than in a run.

---

## 6. What changed on our side, and one thing to know

Three of your four production rules are in: `high-upload-volume`, `smb-service-activity`,
`ssh-possible-break-in`. Their MITRE mappings are yours, unchanged.

**`ssh_failed_login` is not**, and I want to be straight about why rather than have you find
it missing. As a single-event rule it fires on *every* failed login. We already have a
detector that handles the version that is actually a security event — twelve failures inside
a minute becomes one HIGH alert — so shipping both would produce twelve rule findings plus
one burst finding for one brute-force attempt, and our alert engine would read that
repetition as twelve pieces of evidence. It is the same "count over time is already built
in" point from §4(A)2 of the letter; you applied it correctly to your correlation rules, and
neither of us noticed that this single-event rule is the same thing in miniature.

If you want it back, the version that would work here is a rule for the *success after the
failures* — a successful login from an address that had been failing — but that needs the
multi-step correlation we have deliberately not built.

Your `check_rule_integration_contract.py` now lives here as a test, so the conventions you
wrote are enforced on every commit rather than when someone remembers to run a script.
