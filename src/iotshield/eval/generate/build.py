"""Turning a scenario into a bundle: simulate, render, and derive the answer key.

The step that makes the whole approach work is the last one. **Nothing here annotates
anything.** The faults were declared in the world, the behaviours reported which of them they
actually perturbed, and the labels are read off both — so the answer key cannot disagree with
the data, because it is the same statement written twice.

Ordering lives here rather than in a renderer: every device's events for one lane are pooled
and sorted by ``(timestamp, asset_id)`` before rendering, so a multi-host capture is one
time-ordered file the way a real one is. Renderers stay order-preserving maps.

Four invariants are checked while building, and each exists because breaking it would produce
a corpus that *looks* fine and scores wrong (see ``world.GenerationError``).
"""

from collections.abc import Mapping
from datetime import datetime
from functools import partial

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.eval.corpus import Case, CaseInput, Corpus
from iotshield.eval.generate.port import BEHAVIOURS, RENDERERS, Behaviour, Renderer
from iotshield.eval.generate.rng import stream
from iotshield.eval.generate.world import (
    Clock,
    FaultSpec,
    GenerationError,
    Recording,
    Scenario,
)
from iotshield.eval.truth import AssetTruth, GroundTruth, Incident
from iotshield.pipeline.registry import lookup


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


def _incident(fault: FaultSpec, clock: Clock, lanes: frozenset[Lane]) -> Incident:
    start, end = clock.span(fault.start_step, fault.length_steps)
    return Incident(
        asset_id=fault.asset_id,
        start=start,
        end=end,
        kind=fault.kind,
        fault_id=fault.fault_id,
        lanes=lanes,
    )


def _observe_lane(
    scenario: Scenario, recording: Recording, source_format: str
) -> tuple[CaseInput, list[NormalizedEvent], Lane, frozenset[str]]:
    """Every device's events for one lane, rendered into one file's text.

    Streams are scoped by the renderer's **lane**, not by its format. Keying on the format
    would make a behaviour's draws depend on which dialect happens to render them — the same
    scenario rendered as Zeek would describe different traffic than one rendered as CSV,
    which contradicts what ``rng`` promises and makes a dialect-parity test impossible to
    write. The world is what it is; the dialect only decides how it is written down.
    """
    renderer, behaviour = _resolve(source_format)
    pooled: list[NormalizedEvent] = []
    expressed: set[str] = set()

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
        expressed |= observed.expressed

    # Time order across devices, with the asset id as the tie-break so a recording is
    # deterministic even when two devices are observed at the same instant.
    pooled.sort(key=lambda event: (event.timestamp, event.asset_id))
    lines = (*renderer.header(), *renderer.render(pooled))
    text = "\n".join(lines) + "\n" if lines else ""
    return CaseInput(source_format, text), pooled, renderer.lane, frozenset(expressed)


def _validate(
    scenario: Scenario,
    recording: Recording,
    clocks: Mapping[str, set[datetime]],
    expressed: Mapping[str, set[Lane]],
) -> None:
    """Refuse a recording that would ship as a corpus scoring wrongly but looking fine."""
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
    unexpressed = [
        fault.fault_id for fault in recording.faults if not expressed.get(fault.fault_id)
    ]
    if unexpressed:
        raise GenerationError(
            f"case {recording.name!r}: no lane in {list(scenario.formats)} expressed "
            f"{unexpressed}. A label nothing can see is unfindable by construction, and would "
            "drag recall down for a reason no reader could diagnose"
        )


def _case(scenario: Scenario, recording: Recording) -> Case:
    inputs: list[CaseInput] = []
    clocks: dict[str, set[datetime]] = {}
    expressed: dict[str, set[Lane]] = {}

    for source_format in scenario.formats:
        case_input, events, lane, expressed_here = _observe_lane(scenario, recording, source_format)
        inputs.append(case_input)
        for event in events:
            clocks.setdefault(event.asset_id, set()).add(event.timestamp)
        for fault_id in expressed_here:
            expressed.setdefault(fault_id, set()).add(lane)

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
                    sorted(instants),
                    [
                        _incident(fault, recording.clock, frozenset(expressed[fault.fault_id]))
                        for fault in recording.faults_on(asset_id)
                    ],
                )
                for asset_id, instants in clocks.items()
            ]
        ),
    )


def build(scenario: Scenario) -> Corpus:
    """Build one scenario in memory. Deterministic: same scenario, same bytes."""
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
