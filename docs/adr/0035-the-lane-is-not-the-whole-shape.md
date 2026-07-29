# ADR-0035: The lane is not the whole shape

Status: accepted (2026-07-29)

## Context

Benchmarking `meter_iforest` against `skab_valve1` produced sixteen rows of zeros. Under
`--fit-protocol prefix` the report said `fit refused (fit needs telemetry meter events
carrying a consumption value and a meter payload; got none)`. Under `none`, with a valid
artifact, it said **nothing at all** — a clean row of zeros that reads exactly like a bad
detector.

The pairing had been offered by the Evaluate page, which pre-selected it: `/eval/detectors`
sorts alphabetically, `meter_iforest` sorts first among the telemetry detectors, and
eligibility was computed on lanes alone. So the page opened on it, blocked with *"no artifact
is named"*, and the way out — the free-text artifact box — had no way to tell the operator
that `demo_meter` exists. Following the page's own two suggestions in order (name an
artifact, or fit from scratch and pick `prefix`) arrives at a run that cannot measure
anything.

Three defects, one cause and two consequences of it:

1. An **artifact id was free text over a closed vocabulary nobody published**. The product
   ships exactly two artifacts. Nothing enumerated them; the Evaluate page carried a
   hardcoded table naming one, whose own comment pointed at the fix.
2. **`Lane` was the only compatibility axis**, and `TELEMETRY` means two different things: a
   bare numeric reading, and a meter reading carrying a `MeterReading`. `meter_iforest` needs
   the second and could only declare the first.
3. **`why_silent` could not explain the resulting silence.** It answers "which meters have no
   model", filtering through `_is_meter_reading` first — so when *no* event is a meter
   reading the set is empty and it reports no reservation, in the one case that most needed
   one.

## Decisions

### D1 — An adapter publishes the cached fits it ships

`detect/_artifacts.py: packaged_artifact_ids` lists a package's artifact ids;
`AdapterSpec.artifacts` declares the provider beside the existing `artifact_param`, which
names the *field* while this names the **values**. `AdapterSpec.artifact_ids()` is total, so
the two API catalogues cannot disagree about what "no provider" means.

They travel as **`ParamInfo.choices`**, not as a field of their own. The ids are a closed
vocabulary the registry knows and the factory signature cannot express — `artifact: str |
None` is the only writable annotation, because the members are files found at import — so
this is ADR-0023 D9's fact with a different provenance, and giving it a second representation
on the wire would create two things to keep in step. The consequence is that every form which
already renders a closed vocabulary as a `<select>` renders this one too: the compose page
needed no change at all.

Rejected: keeping free text with a placeholder listing the ids. The namespace is genuinely
closed *today* — `load_artifact` resolves package resources and nothing else — and a control
that enumerates a finite known set is the accurate representation of that, not a convenience.
It also makes a second fact visible that an empty box hides: `meter_iforest` ships one
artifact covering one meter, which a one-item dropdown says immediately.

### D2 — `PayloadToken`: the typed lane vocabulary is its own axis

A **lane** is the correlation axis — which events pool together, which detector is handed
them, and what counts as independent agreement about one device. A **payload** is a format
axis *within* a lane: which typed record (`flow.FlowRecord`, `meter.MeterReading`) rides in
`metadata`. `Capability.consumes/produces` were already `frozenset[Token]` over a union, so
this is a fourth member and not a contract change.

The token's value **is** the vocabulary module's `METADATA_KEY`, pinned by a meta-test, so a
declaration cannot drift into a string no writer produces. A second meta-test asserts every
payload a detector consumes is produced by some normalizer, which closes the loop a
string-valued token would otherwise leave open: a typo would make a detector permanently
unreachable and look exactly like one nobody can use.

The guard's check is a **line-for-line mirror of the idle-lane check** and, like it, a
**WARNING**: nothing crashes, the detector is simply handed events it discards. It also fires
only when the lane matched, so one unmet joint produces one message.

Rejected: **`Lane.METER`**. It would work, and it would make a meter anomaly and a
temperature spike on the same device stop corroborating each other — a real product
regression accepted to fix a declaration problem, on a frozen contract enum every consumer
moves with.

Rejected: **special-casing `meter_iforest`** in the guard or the picker. That is a fact about
an adapter living outside the adapter, which drifts the first time someone adds a second
telemetry payload, and it discards ADR-0014's defining property — that the guard reads a
stage's shape *from the stage*, without constructing it.

`rules` is the clearest evidence the axis is per-*configuration* rather than per-adapter:
`rules(lane: flow)` reads every flow field through `FlowRecord` and needs the payload;
`rules(lane: log)` reads the contract's own fields and needs none. `passthrough` declares
both lane and payload as **params**, for the reason ADR-0025 already gave for the lane: it
envelopes what a partner already emits, so its shape is a claim the operator makes, and the
parser cannot discover a vocabulary it does not inspect.

Two surfaces ask "can these connect?", and they ask it differently on purpose:

- The **compose** pages match a configured chain, so they compare rendered tokens directly
  and require **every** token: `feeds` changed from an intersection to a subset test, which
  is simply what `consumes` always meant — a conjunction — and was indistinguishable from an
  intersection while every detector consumed exactly one token. Adding an axis costs nothing
  here, which is why no new field was needed.
- The **evaluate** pages match a *corpus*, which is a union across its source positions, so a
  multi-lane bundle legitimately feeds a single-lane detector: **any** of the lanes, **all**
  of the payloads. That predicate is `api.evaluation.can_judge`, called by the Streamlit page
  and reimplemented once in TypeScript, where the language boundary forces a copy.

Those are different questions, not two spellings of one. Stating it as a single rule was the
first draft of this ADR and it was wrong in one direction or the other in every phrasing.

### D3 — `why_silent` explains the silence it was built for

The check now asks the emptier question first: if no event on the lane carries a meter
payload, say so, naming the gate rather than only the outcome. Guarded on there being
telemetry at all, because a run with none already draws the guard's idle-lane warning and one
fact belongs in one place.

## Consequences

- **`meter_iforest` is no longer offered for any shipped corpus**, because none is in a meter
  format. That is the honest state: it was previously offered by five of six and could
  succeed on none.
- **An offered-but-silent detector is still offered.** The line is whether the answer can
  vary with the data. `severity_spike` on a burst of INFO lines evaluates every event and
  correctly finds nothing — a measurement, and ADR-0027 D8 is right that withholding it would
  prevent making it. `meter_iforest` on `stub-csv` cannot read an event whatever it says. The
  test that asserted the opposite has been rewritten to state this distinction.
- **`_lanes_fed_by` became `_shape_fed_by`**, returning both axes from one walk so they cannot
  disagree about which normalizer reads a format.
- **The Streamlit page's optional closed vocabulary was fixed on the way through.** It
  preselected `choices[0]` and submitted it, the defect ADR-0033 fixed on the React side and
  this page never had applied — live for `meter.meter_type` (default null, choices
  water/electricity), where picking a source dialect silently pinned the utility.
- **Two proposals from the review were dropped after checking**, and are recorded here so
  nobody re-derives them: porting `ComposePage`'s `ready()` to the Evaluate page is redundant
  once D1 preselects a shipped artifact and D2 removes the bad pairing; and warning when
  `prefix` cannot satisfy `min_train_hours` is already covered, because `fit` raises with the
  requirement named and `prepare_detector` reports that message verbatim.
- **Still open:** the artifact namespace is closed only because `load_artifact` reads package
  resources. ADR-0030 anticipates deployment-fitted meters, which are data we may not commit;
  when a path loader arrives the dropdown becomes the *shipped* half of a wider answer, and
  the control becomes a combobox.
