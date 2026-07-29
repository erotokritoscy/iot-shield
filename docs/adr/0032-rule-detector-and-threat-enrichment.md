# 32. The rule detector, and threat knowledge on the alert

Date: 2026-07-29

## Status

Accepted. Adds one detector adapter, one top-level vocabulary module, and a packaged rule
library; fills `PrioritizedAlert.threat_class` and `recommended_action` for the first time.
Discharges ADR-0020 D6 (the deferred promotion of a detector's category to `threat_class`)
and the enrichment half of the design record `docs/design/weeks-3-4.md` §7 reserved and
never got written. Changes no contract. Defers the shape-based inference tier (D6).

## Context

Antonia Voutouri's *iot-ot-alert-correlation* is the fourth teammate module to be
integrated. The agreement is `docs/integration/rule-detector-and-enrichment-plan.md`, which
asked for eight changes on her side and promised four on ours. All eight are done and were
verified file by file before this work began: every rule now declares its lane, its
observable-vs-label basis, one confidence tier, a structured MITRE mapping, a one-line
description and a list of suggested actions; the label-based rules are separated from the
production ones; and her scripts import without side effects. She also wrote
`src/testing/check_rule_integration_contract.py`, a validator for the whole convention,
which is genuinely useful to us and lands here as a test rather than a script.

**Two things are being adopted, and they serve different purposes.**

*The rules* fill a capability gap. Every detector shipped before this one answers the same
shape of question — *is this unusual?* The threshold detector asks it of a number, team3's
z-score and the meter forest ask it of learned normal behaviour, the burst detectors ask it
of a rate. None can answer *is this a known-bad thing?* The two families fail in opposite
directions: statistics catch what nobody anticipated but cannot say what it is; rules say
exactly what something is but only catch what somebody already wrote down. Having only one
half was a real hole.

The second thing the rules change is *who can extend the product*. Every other detector
requires Python, the contracts and the port suite. This one is extended by writing a text
file — the difference between a system experts extend and a system experts use, and the
change most likely to keep this repo alive after the internship.

*The enrichment* fills a credibility gap. `PrioritizedAlert` has reserved `threat_class` and
`recommended_action` since contracts v1. Grep before this change: `threat_class` was the
literal default `"unknown"` on every alert this system had ever produced, and
`recommended_action` was a generated restatement of the alert ("Inspect cam-07 around
12:04; review flow_burst evidence"). The product's promise is *prioritized, **explained**
alerts*, and the second half was aspirational.

**One genuine incompatibility.** Her canonical event model keeps both endpoints of a network
conversation, a semantic classification of what happened (`event.activity`,
`event.outcome`), and observations grouped by domain. Ours is flat and minimal — one device,
one moment, one observation — because the whole product correlates on `(asset, time)`, so
"which device is this about" is resolved at ingest by flow orientation (ADR-0023 D2) rather
than by each rule. Neither model is wrong. But the *names* in her rules refer to things that
do not exist here.

## Decision

### D1 — Rules address a checked vocabulary of our own, not a translation of hers

`detect/rules/fields.py` publishes a closed, per-lane table of legal field names, each with
a kind (`number` / `text` / `tier`) and instructions for reading it off an event.

*The alternative considered and rejected:* keep her vocabulary and translate our events into
her shape, so her rule files run unmodified in both repos. It fails on the log lane. To
satisfy `event.type == "authentication"` we would have to invent a semantic classification
of log lines that our syslog normalizer does not produce — her OpenSSH normalizer produces
it from a hand-written template table. That is a shadow copy of a second canonical model,
permanently half-empty, owned by nobody. Golden rule 2's "if an adapter needs a contract
change, that's a design smell" applies to inventing a second contract as much as to editing
the first.

*Why a table rather than free-form dotted paths.* The failure mode this design exists to
prevent is a rule that loads clean and matches nothing — indistinguishable from a quiet
night, and for a detection system the most dangerous outcome available. `FlowRecord` closed
exactly this hole between normalize and detect by making the untyped hop one function call
wide, so a name mismatch is caught by the type checker. That trick is unavailable here: a
rule's field name is **text in a YAML file at runtime**. So the same principle takes a
different shape — the untyped hop becomes one *table* wide.

The load-bearing detail is that each entry **reads through the typed record**, never around
it into `metadata`. Three consequences, and they are the whole justification:

1. If `FlowRecord` loses or renames a field, the table stops type-checking and CI catches
   it. The vocabulary cannot drift from the data it describes.
2. The table is data, so it is enumerable: `python -m iotshield.detect.rules` prints it, and
   `docs/integration/rule-authoring-guide.md` is checked against it by a test. A published
   field list that is a hand-maintained document is a promise to keep two things in sync
   forever, which nobody keeps.
3. Rules are checked at **load**: an unknown name is refused with a "did you mean", and an
   operator that does not suit a field's kind is refused too — where upstream raises an
   uncaught `TypeError` mid-run.

*The vocabulary is closed.* No escape hatch for arbitrary metadata paths. An open path
re-opens the silent-failure hole for the least-defended audience — someone editing YAML,
with no type checker and no error message. A source needing fields we do not model is a
request to extend the table (one reviewed entry), not to bypass it.

*Placement.* It crosses no port boundary — YAML in, events read, findings out, all inside
one adapter — so it lives with the adapter. Contrast D4, which genuinely crosses
detect → alert and therefore sits at the top level beside `flow` and `meter`. Placement
follows who has to agree, not how important the thing feels.

### D2 — The grammar is ported verbatim; four behaviours are made stricter

Selections of field/operator/value triples ANDed together, combined by a flat `condition`
(`A`, `A OR B`, `A AND B`), seven operators, no parentheses and no NOT — upstream's language
exactly. It is small, it is what its author is fluent in, and it is the interface a future
contributor learns. Only the vocabulary moved.

Deliberately stricter, each fixing a failure we would otherwise inherit:

1. **A missing value never matches, whatever the operator.** Upstream, `!=` against an
   absent field returns True, so `flow.service != "smb"` fires on every event with no
   service at all — an absence read as a positive claim.
2. **Type mismatches are refused at load** (upstream: uncaught `TypeError`, run down).
3. **A condition naming a missing selection is refused at load** (upstream: `KeyError` on
   the first event, i.e. in production).
4. **Rule files load in sorted order** — "same input, same output" is a port law and an
   unsorted directory listing guarantees nothing.

`contains` is **case-insensitive** (it is a search, and casing differs between exporters far
more often than meaning does); `==` stays exact, because equality that quietly ignores case
is a surprise. **No regex operator**: `contains` covers every signature we need, and
ADR-0020 D3 already established the reason to stop there — an operator-authored regex in
YAML is a config typo away from being pathologically slow, with nothing between it and the
run.

Upstream's extra metadata keys (`rule_type`, `integration_status`, `signal_basis`,
`severity`, `correlation`, `one_line`) are **accepted rather than forbidden**, so a rule
moves between the two repos with only its field addresses changed. Where a key encodes a
claim, the accepted values are constrained — which turns each into a safety gate: a
label-based demo rule cannot be dropped into the production library by accident. Everything
else is refused (`extra="forbid"`), because for a detection rule a silently ignored key
means silently not detecting. `correlation` is accepted and **inert**: our normalizers
already decided the asset.

`mitre` is **optional**. Not every worthwhile signature is an attack — a kernel OOM kill has
no technique — and forcing a mapping would fabricate threat context. With none, the rule's
title becomes the threat class, which is far more honest than `unknown`.

### D3 — One adapter, one instance per lane, many rules per instance

The Detector port gives a detector a single `lane` and the pipeline routes by it, so a rule
set spanning network and logs is **two configured entries**. Not two adapters (the code is
identical), and emphatically not one entry per rule — that would put every new rule into
every deployment's config and destroy the "adding a detection is dropping in a file"
promise that is most of this adapter's value.

A consequence worth recording because it cost a decision: `method` is a property of the
*detector*, and the port suite requires every detection to carry it (law 1), so the firing
rule **cannot** be named there. The intent — that an analyst reads which rule fired, not
just which adapter — is served by `Enrichment.origin` (`rule:high-upload-volume`), which
reaches the alert rationale. `method` is `rules:<lane>`.

**Score and threshold** are synthesised from the author's two judgments, because a rule match
is a boolean and the port requires `score > threshold` while the alert engine reads
`score / threshold` as its severity factor. Severity sets the base
(low 1.5 / medium 2.0 / high 3.0 / critical 4.0), confidence scales it
(low 0.7 / medium 0.85 / high 1.0). The margin then *is* the authored severity — exactly how
the integration letter framed "severity becomes a hint that feeds the score". Two invariants
a test pins: every product is strictly above the threshold, and none exceeds the weighted
engine's severity cap of 4.0, so the scale is used end to end with nothing clipped.

Including confidence was a deliberate choice over severity alone. It gives contributors a way
to add *uncertain* knowledge safely — `inbound-remote-admin` is high severity at low
confidence, so it sits in the queue without dominating it — and a growing community rule set
needs that middle option to grow at all. It also means a field we asked her to make
consistent is actually read.

A single rule cannot reach the top of the queue: even critical at high confidence lands at
63/100 (HIGH) alone. Reaching CRITICAL needs a critical device, corroboration, or repetition.
A detector states its case; the pipeline decides the ranking.

### D4 — Threat knowledge crosses detect → alert as a typed record

`iotshield/enrichment.py`: `Enrichment` (threat class, source, origin, MITRE, actions) with
`to_evidence` / `from_evidence`.

The alert layer has never before looked at detector-supplied structured content — it read
only universal fields (score, threshold, lane, method). `threat_class` and
`recommended_action` cannot be filled that way: they are *content*, and only the detector
that made the judgment knows it. The only channel is `DetectionResult.evidence`, which is
`dict[str, Any]` and unchecked.

This is the **third** time this project has met that problem — the FLOW lane met it at
normalize → detect, the METER lane met it again, and both times the answer was a typed
record with its own read path, so neither side ever names a key. Three is enough repetition
to name it: this is the house solution for structured data crossing an untyped seam.

*Rejected:* a new first-class contract field. Cleanest and typed, but it means a storage
column, an API change and a migration for something whose shape we are still learning. The
typed-record pattern gets the type safety without the contract change.

### D5 — Three precedence tiers, and the middle one is the point

`docs/design/weeks-3-4.md` §5.1 proposed two sources — authored enrichment vs a label
inferred from the anomaly's shape — with authored winning. Working through the actual
detectors showed a third sitting between them, and it is the one that makes the queue
coherent:

1. **`RULE`** — the specific rule that fired says what this is.
2. **`DETECTOR`** — the detector's named preset maps to a known threat pattern. "A device
   contacted 41 hosts on the telnet port in one minute" *is* remote system discovery; the
   label is as authored as a rule's, merely authored in code rather than in YAML.
3. **`HEURISTIC`** — inferred from shape. Deferred (D6); the tier exists so the ladder is
   decided rather than discovered later.

Without tier 2 the queue splits into "rule alerts, which are explained" and "everything
else, which says unknown" — and the split tracks *which detector fired*, not how dangerous
anything is. An analyst scanning that column would reasonably infer the opposite. Tier 2 is
five preset entries and it closes that gap for every detector that fires in every shipped
demo.

The mappings are ours, cross-checked against her `docs/10_mitre_mapping.md`: `auth_failure`
→ T1110 (hers), `exfiltration` → T1041 (hers), `port_scan` → T1046 (hers), `fan_out` → T1018
(ours: many hosts on one service is *Remote System Discovery*, which is what ATT&CK
separates from T1046's many services on one host — the two presets therefore map to two
techniques rather than sharing one label). `severity_spike` gets a threat class and **no
MITRE**: an error storm is a real operational phenomenon and not an attack technique.

**Promotion.** `threat_class` is a single field, so one claim wins: highest tier, then
strongest detection, then position (deterministic). **`suggested_actions` are unioned**
across every contributing detection, capped at six — a device showing both failed logins and
a bulk transfer needs both sets of next steps. Promotion lives in `alert/_correlate.py` and
both engines use it, for the same reason the severity mapping is shared: which threat class
an alert carries is a product convention, not a property of a scoring formula.

Enrichment **never moves the priority** (a test pins it). Explanation is not evidence; if
writing a MITRE mapping promoted an alert, the ranking would stop meaning what it says.

### D6 — The shape-inference tier is deferred, not dropped

`docs/design/weeks-3-4.md` §5.1 specified a heuristic that reads an operational label off
the anomaly's shape (a spike as a transient, a long collective span as drift). It is not
built here, and the deferral is deliberate rather than incidental.

*Content quality.* "Sustained collective span — possible degradation or lasting fault" is
not a threat classification; it is a restatement of the shape in more words. Compare what
the meter detector already writes for itself — "consumption far above the usual level for
this hour of the week" — which is genuinely useful, is already produced, and came from the
detector knowing its own domain rather than from a shape heuristic.

*Risk.* A guessed label costs real trust when wrong. The plan anticipated this with an
"is this a guess" flag, which is the right mitigation and costs about as much as the
feature.

So for unsupervised detectors `threat_class` stays `"unknown"` — which now means something
honest: *nothing here claims to know what this is*. The `HEURISTIC` tier and `is_heuristic`
are implemented in the vocabulary and enforced in the rationale (`(inferred)`), so the
tier can be filled later without redesigning anything.

### D7 — One evidence key is standardized: the headline, not the schema

Every detector emits `evidence["explanation"]`, one analyst-readable sentence, **and the
port contract suite enforces it**. The rest of each detector's evidence stays whatever it
genuinely has.

§5.1 wanted all producers to agree on an evidence schema. Forcing that is wrong: the
detectors observe genuinely different things, and flattening z-scores, feature attributions,
top peers and sample log lines into one shape produces a lowest-common-denominator that
serves the dashboard and nobody else. Standardising the headline gives a uniform thing to
render without pretending the detectors are more alike than they are.

*Why it is a port law and not a convention:* it shipped as four string literals in six
adapters while this record and `CLAUDE.md` both said "every detector". `_group.py` already
states the principle — *"a law whose omission is silent belongs in the skeleton, not in a
comment"* — so the assertion lives in `tests/ports/test_detector_port.py`, which is
parametrized over every registered adapter. Writing the test is what revealed that
`stub_threshold` and `team3_zscore` did not comply; both now do.

### D8 — The rule set is measured, not demonstrated

`config/bench/rules_flow.yaml` runs the packaged library against `fleet_scan`, head to head
with `config/bench/flow.yaml`. Measured (5 cases, 5 labelled incidents):

| spec | detectors | evF1 | evP | evR | ovl | ptF1 | det/inc | flow point P/R |
|---|---|---|---|---|---|---|---|---|
| `rules_flow` | rules | **0.67** | 0.50 | **1.00** | 1.00 | 0.29 | 2.00 | 0.18 / 0.81 |
| `flow` | flow_burst:fan_out | 0.26 | 0.17 | 0.60 | 0.39 | **0.44** | 3.60 | 0.39 / 0.51 |

**Read this as a comparison of families, not of implementations, and the caveats are load
bearing.** The rule spec runs *six rules*; the flow spec runs *one preset*, and `fan_out` is
structurally blind to two of the five recordings by design (ADR-0024 D3 built the pair to be
discriminating). The rules' recall of 1.00 is also partly a property of this corpus: the
generator's port sweep walks ports 1..N, so it touches 23 and
`telnet-outbound-propagation` catches the moment it does. That is a true detection of a real
observable — but it is the rule recognising *one port*, not recognising a sweep.

What the table does support: a rule library spans phenomena that a single configured preset
cannot, its windows are tight (ovl 1.00 against 0.39), and it pays for that in point
precision (0.18 against 0.39). Both ship because both readings are true.

### D9 — Three rules ported, three authored, one deliberately dropped

Ported with only their field addresses changed: `high-upload-volume` (T1041),
`smb-service-activity` (T1021). Re-expressed rather than re-addressed:
`ssh-possible-break-in`, because hers matches `event.activity ==
possible_break_in_attempt` — a semantic classification her OpenSSH normalizer produces and
ours does not — so it matches the message text sshd actually writes. Her MITRE mapping is
unchanged in all three.

Authored here for the parts of the language the ported rules do not exercise:
`telnet-outbound-propagation` (multi-field selection), `inbound-remote-admin` (OR condition,
and the worked example of low confidence), `critical-log-event` (the tier kind, and the
no-MITRE path).

**`ssh_failed_login` (rule-ssh-001) is deliberately not ported**, and she should be told
why. As a *single-event* rule it fires on every failed login; `log_burst:auth_failure`
already collapses twelve of them into one collective detection and one HIGH alert. Shipping
both would produce twelve rule detections plus one burst detection on one asset — the exact
double-counting the integration letter warned about in §4(A)2 ("count-over-time patterns are
already built in"). She applied that correctly to her multi-step correlation rules; this
single-event rule is the same problem in miniature, and neither of us spotted it when the
letter was written.

Her seven label-based rules are correctly excluded and stay in her repo, where they are
useful for her own evaluation. `signal_basis: observable` is enforced at load as defence in
depth.

**The thresholds are examples, not calibrated values.** The 10000 in `high-upload-volume`
comes from the TON_IoT capture it was developed against; on a real network 10 kB is an
ordinary web response. What makes it defensible is that it is *measurable* (D8). Related
limitation: the sweep machinery tunes a detector's registry params, and a rule's threshold
lives inside a YAML file, so `--sweep` cannot reach it. Tuning a rule means editing the rule.

## Consequences

- **`threat_class` is populated for the first time.** Two of five detectors carry authored
  claims; the other three stay honestly unclassified until D6's tier is built.
- **The product is extensible without Python.** Adding a detection is dropping a YAML file
  into the packaged library or into a directory named by `ruleset:`. That is the handoff
  artifact `weeks-3-4.md` §5.6 exists to produce, arriving early.
- **A fourth teammate module is integrated**, and the knowledge stays hers: the rules are
  content she owns and can grow, and her validator ships here as a test.
- **The alert layer now reads inside `evidence`** — a new coupling, deliberately made typed
  so it cannot rot silently.
- **`log_burst`'s severity rank moved** to `detect/_severity.py`, shared with the rule
  detector's tier comparison. Two copies of that table would eventually disagree. (The
  ordering arguably belongs on `Severity` itself in `contracts/enums.py`; it was left in
  `detect/` because contracts are law here and no non-detect consumer exists yet. A test
  pins that every `Severity` member has a rank, which closes the drift that mattered.)
- **`detect/_runs.py` gained `group_by_gap`** — "already-interesting items → time-separated
  episodes", the third episode shape, promoted out of the rule adapter because nothing about
  it is rule-specific and a private method is where the fourth copy comes from.
- **The packaged rule library is memoized.** It cannot change while the process runs, and a
  benchmark constructs a detector per case, so an uncached load re-parsed the shipped YAML
  once per lane per case (6.3 ms each; 32% of a five-case benchmark). A `ruleset:` directory
  is deliberately *not* cached — the Pipeline API is long-lived and an operator who edits a
  rule should not have to restart.
- **The evaluation harness gained a second flow spec**, so `fleet_scan` now measures a
  head-to-head between detector families rather than one detector against nothing.
- **Deferred:** the shape-inference tier (D6); multi-step / sequence rules (her `corr-001`
  shape — a real future feature, and the job of a correlation layer that works differently
  from ours); sweeping a threshold that lives inside a rule file; asset aliasing, still open
  from ADR-0023, which is what would let a hostname-keyed log rule and an IP-keyed flow rule
  corroborate without the sources agreeing on an identifier by hand.
