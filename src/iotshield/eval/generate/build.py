"""Turning a scenario into a bundle: simulate, render, and derive the answer key.

The step that makes the whole approach work is the last one. **Nothing here annotates
anything.** The faults were declared in the world, the behaviours reported *which instants
they actually perturbed*, and the labels are read off those instants — so the answer key
cannot disagree with the data, because it is made of the data.

That last clause used to be an aspiration. The behaviours reported a bare fault id, set when a
handler was dispatched rather than when the output changed, and the interval was recomputed
here from the declared ``FaultSpec`` window — a second computation, with different clamping,
that never touched a byte. Carrying the instants collapses the two statements into one; see
``_incidents``.

And it now holds *absolutely* rather than up to a convex hull. ``_incidents`` splits a fault's
perturbed instants into contiguous runs of the lane's own clock, so a quiet stretch in the
middle of a fault is no longer labelled (ADR-0028 D3), and it emits one incident per lane, so
no interval describes a union that no single modality showed (D2).

Ordering lives here rather than in a renderer: every device's events for one lane are pooled
and sorted by ``(timestamp, asset_id)`` before rendering, so a multi-host capture is one
time-ordered file the way a real one is. Renderers stay order-preserving maps.

Four invariants are checked while building, and each exists because breaking it would produce
a corpus that *looks* fine and scores wrong (see ``world.GenerationError``).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from functools import partial

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.detect import merge_runs
from iotshield.eval.corpus import Case, CaseInput, Corpus
from iotshield.eval.generate.port import BEHAVIOURS, RENDERERS, Behaviour, Renderer
from iotshield.eval.generate.rng import stream
from iotshield.eval.generate.world import (
    FaultSpec,
    GenerationError,
    Recording,
    Scenario,
)
from iotshield.eval.truth import AssetTruth, GroundTruth, Incident
from iotshield.pipeline.registry import build_normalizer, lookup

_NORMALIZER_FOR = {
    "stub-csv": "stub",
    "syslog": "syslog",
    "flow-csv": "flow",
    "zeek-conn": "flow",
}
"""Which normalizer reads each renderable format back — the inverse of ``RENDERERS``.

Written out rather than derived, so a renderer added without a reader fails the round-trip
check loudly instead of skipping it into invisibility.
"""


def _resolve(source_format: str) -> tuple[Renderer, Behaviour]:
    renderer = lookup("renderer", RENDERERS, source_format)
    behaviour = BEHAVIOURS.get(renderer.lane)
    if behaviour is None:
        # Not a `lookup`: this is a wiring inconsistency between two registries, not a name
        # a caller mistyped, so the message has to name both sides.
        raise GenerationError(
            f"renderer {source_format!r} produces {renderer.lane} events but no behaviour "
            f"models that lane; known: {', '.join(sorted(BEHAVIOURS))}"
        )
    return renderer, behaviour


def _incidents(
    fault: FaultSpec,
    perturbed: Mapping[Lane, frozenset[datetime]],
    clocks: Mapping[Lane, Sequence[datetime]],
) -> list[Incident]:
    """One fault's labels: **one per lane, split at the gaps** (ADR-0028 D2, D3).

    Nothing here is read off ``fault.start_step``/``length_steps``. Those are *intent*, and
    reading the label off them made the answer key a third statement rather than the same one
    written twice: the declared window was computed here unclamped while the telemetry handler
    clamped its damage, so a fault aimed past the end of the clock was labelled past the last
    instant — an unwinnable recall miss no detector could ever score against — and a fault
    scaled to nothing by ``intensity=0.0`` shipped labelled over bytes identical to a clean
    recording.

    Two things changed from the single-interval predecessor, and they are one change seen from
    either end.

    **One incident per lane**, rather than one spanning the union with a *set* of lanes
    attached. A compromised camera is one cause with three projections of different extents;
    the union was a fourth interval no lane actually showed, and the set-valued ``lanes`` had
    to be read two contradictory ways downstream (see ``truth.Incident``).

    **Split at the gaps**, rather than the convex hull of the perturbed instants. The hull
    labelled every quiet instant between two injections. Audited across the shipped corpora
    that cost zero over-labelled instants — but by coincidence rather than by construction: the
    exfiltration fault injects every ~6.7 s into a camera whose normal flows are every 20 s, so
    every normal flow inside the hull landed on a second an injection had already perturbed and
    vanished into the instant set. Move either number and the over-labelling appears.

    The split walks the lane's **own** clock with ``merge_runs`` — the same function a
    row-scoring detector uses to form its judgments, and the one
    ``AssetTruth.from_timeline`` uses on a converted corpus. So truth and detections are formed
    by one rule at both ends, which is what makes the event comparison apples-to-apples.
    Per-lane incidents are what allow it to be the lane's own clock: a union interval would
    have had to walk the asset's *pooled* clock and split on gaps no single lane saw.

    ## The runs stay *inside* one incident (ADR-0029)

    D3's split originally emitted one incident per run, and that made the fragment count a
    denominator. It is an accident: on an additive lane the fault injects records *between* the
    device's own routine ones, so whether a heartbeat lands in a gap or coincides with an
    injection decides how many pieces the label has. Measured, two identical error storms on one
    device produced two incidents and one, moving event recall from 0.50 to 0.60.

    So this returns one incident per **lane**, carrying every run as a span. The gaps survive
    for point scoring, and the event level counts projections — which is what the sentence
    "one incident per (fault, lane)" said all along.
    """
    incidents: list[Incident] = []
    for lane, instants in sorted(perturbed.items()):
        clock = clocks[lane]
        flags = [moment in instants for moment in clock]
        spans = tuple((clock[start], clock[end]) for start, end in merge_runs(flags))
        if not spans:
            # Only reachable if a behaviour reported instants that are not on its own clock,
            # which `Observed` forbids; an empty extent would be a denominator nothing can
            # catch, so it is dropped rather than constructed.
            continue
        incidents.append(
            Incident(
                asset_id=fault.asset_id,
                lane=lane,
                spans=spans,
                kind=fault.kind,
                fault_id=fault.fault_id,
            )
        )
    return incidents


def _observe_lane(
    scenario: Scenario, recording: Recording, source_format: str
) -> tuple[CaseInput, list[NormalizedEvent], Lane, dict[FaultSpec, frozenset[datetime]]]:
    """Every device's events for one lane, rendered into one file's text.

    Streams are scoped by the renderer's **lane**, not by its format. Keying on the format
    would make a behaviour's draws depend on which dialect happens to render them — the same
    scenario rendered as Zeek would describe different traffic than one rendered as CSV,
    which contradicts what ``rng`` promises and makes a dialect-parity test impossible to
    write. The world is what it is; the dialect only decides how it is written down.
    """
    renderer, behaviour = _resolve(source_format)
    pooled: list[NormalizedEvent] = []
    expressed: dict[FaultSpec, frozenset[datetime]] = {}

    for device in recording.devices:
        observed = behaviour.observe(
            device,
            recording.faults_on(device.asset_id),
            recording.clock,
            # `partial` binds eagerly, so this cannot capture the loop variable by reference
            # the way a closure would.
            partial(
                stream, scenario.seed, scenario.name, recording.name, device.asset_id, renderer.lane
            ),
            source_format,
        )
        stray = {event.asset_id for event in observed.events} - {device.asset_id}
        if stray:
            raise GenerationError(
                f"{type(behaviour).__name__} produced events for {sorted(stray)} while "
                f"observing {device.asset_id!r}: a behaviour speaks only for its own device"
            )
        pooled.extend(observed.events)
        # A plain update cannot lose instants because a fault belongs to exactly one asset id
        # and an asset id names exactly one device — enforced by `Recording.__post_init__`,
        # which is what this line used to assert in a comment instead. When two devices shared
        # an id they were handed the *same* FaultSpec, both reported under it, and this kept
        # only the last writer's instants.
        expressed |= observed.expressed

    # Time order across devices, with the asset id as the tie-break so a recording is
    # deterministic even when two devices are observed at the same instant.
    pooled.sort(key=lambda event: (event.timestamp, event.asset_id))
    lines = (*renderer.header(), *renderer.render(pooled))
    text = "\n".join(lines) + "\n" if lines else ""
    return CaseInput(source_format, text), pooled, renderer.lane, expressed


def _validate(
    scenario: Scenario,
    recording: Recording,
    clocks: Mapping[str, dict[Lane, set[datetime]]],
    expressed: Mapping[FaultSpec, dict[Lane, frozenset[datetime]]],
) -> None:
    """Refuse a recording that would ship as a corpus scoring wrongly but looking fine.

    The ``unexpressed`` rule below only became load-bearing once ``expressed`` started
    carrying instants. While it held fault ids set on handler *dispatch*, it could not
    distinguish "this lane models this fault" from "this lane's output changed", so a fault
    that perturbed nothing passed it. Now an empty entry is precisely the fault that left no
    trace, and this refuses it at generation time instead of shipping a label with no data
    behind it.
    """
    declared = {device.asset_id for device in recording.devices}
    aimed = {fault.asset_id for fault in recording.faults}
    if not aimed <= declared:
        raise GenerationError(
            f"case {recording.name!r} aims faults at {sorted(aimed - declared)}, which is not "
            "in the recording; the answer key would name a device with no readings"
        )
    silent = declared - set(clocks)
    if silent:
        raise GenerationError(
            f"case {recording.name!r}: {sorted(silent)} produced no events on any of "
            f"{list(scenario.formats)}, so they cannot be in the roster — findings on them "
            "would be unscoreable rather than false alarms"
        )
    unexpressed = [fault.fault_id for fault in recording.faults if not expressed.get(fault)]
    if unexpressed:
        raise GenerationError(
            f"case {recording.name!r}: no lane in {list(scenario.formats)} expressed "
            f"{unexpressed}. A label nothing can see is unfindable by construction, and would "
            "drag recall down for a reason no reader could diagnose"
        )


def _check_round_trip(recording: Recording, case_input: CaseInput) -> None:
    """Refuse bytes a normalizer reads back as a different device — or not at all.

    ``Device.asset_id``'s docstring claims "the builder checks this rather than trusting it".
    It did not: ``_validate``'s stray check inspects the GENERATOR's events, never what a
    normalizer parses out of the rendered text. The flow lane was protected only by accident
    (an asset id must be an IP in the monitored networks, so a bad one raises), and telemetry
    and log were not protected at all.

    The damage is silent and total. ``Device(asset_id="Pump 3")`` on syslog renders and
    validates cleanly, and the pipeline attributes those events to ``"Pump"`` — because the
    HOST field is space-delimited — so the roster and every ``fault_id`` name a device that
    appears nowhere in the data, recall on it is structurally zero forever, and every
    detection lands in ``unlabelled_detections`` with nothing saying why.

    Round-tripping through the ACTUAL normalizer is what makes this unable to rot: a
    character policy per renderer would have to be re-derived whenever a dialect changed a
    delimiter, while this tests the thing itself (ADR-0033).
    """
    normalizer_name = _NORMALIZER_FOR.get(case_input.source_format)
    if normalizer_name is None:  # pragma: no cover - a format with no reader cannot be scored
        return
    normalizer = build_normalizer(normalizer_name, {})
    result = normalizer.normalize(
        case_input.text, source_format=case_input.source_format, origin=recording.name
    )
    if result.malformed:
        raise GenerationError(
            f"recording {recording.name!r} rendered {len(result.malformed)} record(s) as "
            f"{case_input.source_format} that the matching normalizer cannot read back; the "
            f"first says: {result.malformed[0].error}"
        )
    declared = {device.asset_id for device in recording.devices}
    parsed = {event.asset_id for event in result.events}
    if parsed - declared:
        raise GenerationError(
            f"recording {recording.name!r} rendered as {case_input.source_format} reads back as "
            f"device(s) {sorted(parsed - declared)}, which it never declared — an asset id must "
            f"survive its own renderer, or the answer key names a device absent from the data"
        )


def _case(scenario: Scenario, recording: Recording) -> Case:
    inputs: list[CaseInput] = []
    # Per (asset, lane), not per asset: a device's modalities sample at wildly different
    # rates, and pooling them lets the fastest one own the clock every lane is scored on
    # (``truth.AssetTruth``).
    clocks: dict[str, dict[Lane, set[datetime]]] = {}
    expressed: dict[FaultSpec, dict[Lane, frozenset[datetime]]] = {}

    for source_format in scenario.formats:
        case_input, events, lane, expressed_here = _observe_lane(scenario, recording, source_format)
        _check_round_trip(recording, case_input)
        inputs.append(case_input)
        for event in events:
            clocks.setdefault(event.asset_id, {}).setdefault(lane, set()).add(event.timestamp)
        for fault, instants in expressed_here.items():
            expressed.setdefault(fault, {})[lane] = instants

    _validate(scenario, recording, clocks, expressed)

    # Every device that was observed goes in the roster — which is what makes "this one should
    # have stayed quiet" a measurable claim rather than an assumption (ADR-0026).
    return Case(
        name=recording.name,
        inputs=inputs,
        truth=GroundTruth.from_assets(
            [
                AssetTruth.from_incidents(
                    asset_id,
                    ordered,
                    # The asset's own per-lane clocks are passed through, because a fault's label
                    # is now split against the clock of the lane that showed it. Sorted once per
                    # asset rather than once per fault, since `_incidents` reads the same lists.
                    [
                        incident
                        for fault in recording.faults_on(asset_id)
                        for incident in _incidents(fault, expressed[fault], ordered)
                    ],
                )
                for asset_id, lanes in clocks.items()
                if (ordered := {lane: sorted(instants) for lane, instants in lanes.items()})
            ]
        ),
    )


def _check_lanes(scenario: Scenario) -> None:
    """One lane per scenario, because a run **pools** its sources rather than choosing between them.

    ``Scenario.formats`` is a list of wire formats, and two of them may legitimately name the same
    lane — ``flow-csv`` and ``zeek-conn`` are two dialects of one modality, which is the whole
    reason a renderer is a separate object from a behaviour. Positionally the corpus is still
    valid: a spec declares one source per input, in order, and both would parse.

    What breaks is one joint later. A run routes every source to its normalizer and **pools** the
    results into lanes (ADR-0019), so two flow inputs mean every flow event reaches the detector
    twice: peer counts, byte totals and burst counts all silently double. The answer key does not
    double — a clock is a set of instants, so the duplicate timestamps collapse — which is what
    makes it invisible. The corpus looks fine and every volumetric detector is fed a world with
    twice the traffic in it.

    Refused rather than deduplicated: comparing two dialects is a real thing to want, and the
    shape that expresses it is two corpora (or two cases), not two sources in one run where they
    are additive.
    """
    lanes: dict[Lane, str] = {}
    for source_format in scenario.formats:
        renderer, _ = _resolve(source_format)
        first = lanes.setdefault(renderer.lane, source_format)
        if first != source_format or scenario.formats.count(source_format) > 1:
            raise GenerationError(
                f"scenario {scenario.name!r} records {first!r} and {source_format!r}, which are "
                f"both {renderer.lane} formats. A run pools its sources into lanes, so every "
                f"{renderer.lane} event would reach the detectors twice while the answer key "
                "counted it once; compare dialects as two corpora instead"
            )


def build(scenario: Scenario) -> Corpus:
    """Build one scenario in memory. Deterministic: same scenario, same bytes."""
    _check_lanes(scenario)
    return Corpus(
        name=scenario.name,
        # The size is appended rather than written into the description: hand-typed counts
        # drift the moment a recording is added, and one of the three shipped scenarios was
        # already claiming a device count a recording in it contradicted.
        description=f"{scenario.description} ({scenario.counts})",
        cases=[_case(scenario, recording) for recording in scenario.recordings],
        provenance=scenario.provenance,
        licence=scenario.licence,
    )
