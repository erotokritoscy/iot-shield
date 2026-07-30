"""The generator's laws, as meta-tests a future contributor cannot slip past.

Discovered from the registries rather than named by hand, so every one of these covers the
*next* lane automatically — naming them would only guarantee the law for the ones somebody
remembered to add here. Each has a vacuity guard where discovery could silently find nothing.

The centrepiece is ``test_generated_text_round_trips_through_the_real_normalizer``. A renderer
is the inverse of a normalizer, so the way to check one is to compose them: emit the text, run
the *real* parser over it, and confirm we get back exactly what the generator claimed to
write. That single assertion catches a mis-declared lane, an unparseable timestamp, an
off-by-one clock, a device the normalizer re-attributes to a peer, and a timestamp convention
that will silently stop matching the labels on a future date.
"""

import inspect
import sys
from collections.abc import Sequence

import pytest

from iotshield.capabilities import formats_in
from iotshield.contracts import NormalizedEvent
from iotshield.detect import Fittable
from iotshield.eval.bundle import _EXTENSIONS, NOT_STORED
from iotshield.eval.generate import (
    BEHAVIOURS,
    FAULTS,
    RENDERERS,
    Clock,
    Device,
    Renderer,
    Scenario,
    build,
    lanes_of,
)
from iotshield.eval.generate.build import _observe_lane
from iotshield.eval.generate.meter import hours
from iotshield.eval.generate.port import Behaviour, Calibration, Shape, shape_id
from iotshield.meter import METADATA_KEY as METER_PAYLOAD
from iotshield.normalize.port import Normalizer
from iotshield.pipeline.registry import DETECTORS, NORMALIZERS, build_detector, build_normalizer
from tests.eval.generate.conftest import CLOCK, scenario_of

_METER_FIXTURE_HOURS = 200
"""Enough hourly readings for the laws here to have something to measure, and far short of what
``meter_iforest`` needs to *fit* — nothing in this suite fits anything, and the shipped
``meter_fleet`` corpus is what carries the fittable length."""

# One device that is legitimate on every lane: an IP inside the default monitored networks, so
# the flow normalizer's orientation rule attributes its records to *it* rather than the peer
# (ADR-0023 D2). Using one device for every lane keeps this suite free of per-lane special
# cases, which is what lets it police a lane nobody has written yet.
DEVICE = Device(asset_id="10.0.0.24", kind="camera", role="round-trip fixture")

_RENDERER_IDS = sorted(RENDERERS)


# `shape_id` is the generator's own display form, so a pytest id and the error message
# `build._resolve` raises cannot disagree about what a shape is called. It is also the sort key:
# the raw tuples hold `None` beside a payload name on the telemetry lane, and comparing those
# raises rather than ordering them.
_BEHAVIOUR_IDS = sorted(BEHAVIOURS, key=shape_id)


def normalizer_for(source_format: str) -> str | None:
    """The registered normalizer that accepts this format, if any."""
    return next(
        (
            name
            for name, spec in NORMALIZERS.items()
            if source_format in formats_in(spec.capability({}).consumes)
        ),
        None,
    )


def fixture_clock(source_format: str) -> Clock:
    """The cadence a fixture recording of this format has to tick at.

    A metering row IS an hour, so its behaviour refuses a second-ticking clock rather than
    resampling one (which would either fabricate 3,600 readings per row or discard 3,599). Read
    off the renderer's own shape, so a second payload-bearing lane needs nothing here.
    """
    metering = RENDERERS[source_format].payload == METER_PAYLOAD
    return hours(_METER_FIXTURE_HOURS) if metering else CLOCK


def one_lane_scenario(source_format: str) -> Scenario:
    return scenario_of(
        DEVICE,
        formats=(source_format,),
        clock=fixture_clock(source_format),
        name="round-trip",
        seed=11,
    )


class TestRegistryCompleteness:
    def test_every_registered_renderer_joins_this_suite(self) -> None:
        assert _RENDERER_IDS, "no renderers registered: this whole suite would be vacuous"

    def test_every_registered_behaviour_joins_this_suite(self) -> None:
        assert _BEHAVIOUR_IDS, "no behaviours registered: this whole suite would be vacuous"

    @pytest.mark.parametrize("shape", _BEHAVIOUR_IDS, ids=shape_id)
    def test_every_behaviours_shape_has_at_least_one_renderer(self, shape: Shape) -> None:
        # A behaviour with no renderer can never reach a bundle: it would model something
        # nothing could write down, so a scenario naming it would fail at generation.
        #
        # Both axes, since ADR-0035 reached the generator: matching on lane alone, the metering
        # behaviour was satisfied by `stub-csv` — a renderer that writes the same lane and
        # cannot express a `MeterReading` at all.
        written = [
            name
            for name, renderer in RENDERERS.items()
            if (renderer.lane, renderer.payload) == shape
        ]
        assert written, shape_id(shape)

    @pytest.mark.parametrize("source_format", _RENDERER_IDS)
    def test_every_renderers_shape_has_a_behaviour(self, source_format: str) -> None:
        renderer = RENDERERS[source_format]
        assert (renderer.lane, renderer.payload) in BEHAVIOURS, source_format

    def test_every_benchmarkable_format_can_be_generated(self) -> None:
        """The other direction, and the one nothing asked.

        A format a normalizer consumes is a format a bundle may declare, so it is a format the
        product can be benchmarked on — and without a renderer, one it can never be benchmarked
        on *with a generated corpus*. ``zeek-conn`` sat in that hole: an accepted bundle format
        with an ``_EXTENSIONS`` entry, exercised by the flagship demo, and impossible to
        generate. Nothing said so, because every meta-test here iterated the renderers and so
        could only police the formats that already had one.
        """
        consumable = {
            source_format
            for spec in NORMALIZERS.values()
            for source_format in formats_in(spec.capability({}).consumes)
        }
        assert consumable, "no normalizer declares a format: this test would be vacuous"

        # Every consumable format must be in exactly one of two sets: storable in a bundle
        # (`_EXTENSIONS`) or deliberately not (`NOT_STORED`, with a reason at each entry).
        #
        # This half is new, and it is what closes the hole this test used to have. Restricting
        # to `consumable & _EXTENSIONS` meant a format absent from that table was *invisible*
        # here rather than reported — so it caught `zeek-conn`, which was in the table, and
        # silently ignored both meter dialects and `normalized-jsonl`, which were not. A guard
        # that polices an intersection cannot see something missing from either side of it.
        unclassified = sorted(consumable - set(_EXTENSIONS) - NOT_STORED)
        assert not unclassified, (
            f"neither storable nor declared unstorable: {unclassified}; add an `_EXTENSIONS` "
            "entry (and a renderer) or say in `NOT_STORED` why a bundle never holds it"
        )
        assert not NOT_STORED & set(_EXTENSIONS), "a format cannot be both"

        missing = sorted(
            source_format
            for source_format in consumable & set(_EXTENSIONS)
            if source_format not in RENDERERS
        )
        assert not missing, f"benchmarkable but ungeneratable: {missing}"


_BOOKKEEPING = frozenset(
    {
        # The normalizer's own record-keeping, which a renderer neither knows nor should
        # reproduce: the source line it parsed, a fresh identity, and where the text came from.
        "raw",
        "event_id",
        "origin",
        "ingested_at",
        # Refined on the way in rather than echoed: the syslog reader consumes `syslog` and
        # stamps the *dialect* it recognised (`syslog-rfc5424`), so the two sides legitimately
        # disagree. The lane is asserted separately, which is the routing claim that matters.
        "source_format",
    }
)


def _payloads(events: Sequence[NormalizedEvent]) -> list[str]:
    """Every meaningful field of each event, as comparable text.

    Whole events, deliberately. Comparing the three routing fields left an event's entire
    *content* unpinned — value, metric, severity, message, the flow record — so a renderer
    could write the wrong magnitude and the law would still pass.
    """
    fields = [name for name in NormalizedEvent.model_fields if name not in _BOOKKEEPING]
    return [repr(tuple(getattr(event, name) for name in fields)) for event in events]


def _generated(source_format: str) -> Sequence[NormalizedEvent]:
    """What the behaviour claimed to write, before any renderer touched it.

    The private builder is the right thing to reach for here: this is a meta-test *of* the
    generator, and the whole question is whether what goes in comes back out.
    """
    scenario = one_lane_scenario(source_format)
    (recording,) = scenario.recordings
    _, events, _, _ = _observe_lane(scenario, recording, source_format)
    return events


class TestTheInverseOfANormalizer:
    @pytest.mark.parametrize("source_format", _RENDERER_IDS)
    def test_the_format_is_parsed_by_a_registered_normalizer(self, source_format: str) -> None:
        """ADR-0021 D5's "readings stay real text" law, made enforceable.

        A bundle holds the bytes a run would actually ingest, so a benchmark exercises
        normalization too and measures the *pipeline*. A renderer inventing a format nothing
        reads would quietly break that: every line would quarantine, the detector would see
        nothing, and the report would read exactly like a bad detector.
        """
        assert normalizer_for(source_format) is not None, (
            f"{source_format!r} is parsed by no registered normalizer"
        )

    @pytest.mark.parametrize("source_format", _RENDERER_IDS)
    def test_generated_text_round_trips_through_the_real_normalizer(
        self, source_format: str
    ) -> None:
        """The generator's central law: a renderer is the **inverse** of a normalizer.

        Every payload field is compared, not just the routing ones. This used to check the
        *sets* of lane, asset id and timestamp and nothing else, which left the entire content
        of an event unpinned — a renderer that wrote the wrong metric value, dropped a channel,
        swapped ``src_bytes`` and ``dst_bytes`` or rounded a float passed cleanly. The corpus
        would then label a fault whose magnitude no longer matched what the detector sees, and
        the recall drop would be blamed on the detector.
        """
        name = normalizer_for(source_format)
        assert name is not None
        (case,) = build(one_lane_scenario(source_format)).cases
        result = build_normalizer(name).normalize(
            case.inputs[0].text, source_format=source_format, origin="round-trip"
        )

        assert not result.malformed, [reject.error for reject in result.malformed[:3]]
        assert result.events, "the generated text parsed to nothing at all"

        # Compared as sorted lists rather than sets: a row emitted twice and parsed once is a
        # real defect that set equality forgives.
        assert sorted(_payloads(result.events)) == sorted(_payloads(_generated(source_format)))

        # The device the scenario named is the device the pipeline attributes events to. For
        # the flow lane this is a real hazard rather than a formality: orientation picks the
        # asset from `home_networks`, so a badly chosen asset id lands on the peer instead.
        assert {event.asset_id for event in result.events} == {DEVICE.asset_id}
        # And the answer key's clock is exactly the instants the pipeline sees, so point
        # metrics are scored over rows that genuinely exist.
        lane = RENDERERS[source_format].lane
        assert {event.timestamp for event in result.events} == {
            moment for moment, _ in case.truth.assets[0].timelines[lane]
        }

    @pytest.mark.parametrize("source_format", _RENDERER_IDS)
    def test_a_header_is_written_once_however_many_devices_contribute(
        self, source_format: str
    ) -> None:
        # Header lines are schema, not data — the same distinction `normalize.flow` makes on
        # the reading side. A per-device header would be a data row to every parser.
        renderer = RENDERERS[source_format]
        header = renderer.header()
        if not header:
            pytest.skip(f"{source_format} has no header")
        fleet = scenario_of(
            DEVICE,
            Device(asset_id="10.0.0.25", kind=DEVICE.kind),
            formats=(source_format,),
            clock=fixture_clock(source_format),
            name="round-trip",
            seed=11,
        )
        text = build(fleet).cases[0].inputs[0].text
        for line in header:
            assert text.count(line) == 1, line


class TestFaultDeclarations:
    def test_every_registered_fault_is_expressed_by_some_lane(self) -> None:
        """A fault no behaviour implements is a label nothing could ever produce.

        This is what remains to be checked now that ``lanes_of`` derives the lane set from the
        behaviours instead of storing it: the two can no longer disagree, so the only real
        question is whether a fault is implemented at all.
        """
        orphans = [kind for kind in FAULTS if not lanes_of(kind)]
        assert not orphans, f"registered but expressed by no lane: {orphans}"

    @pytest.mark.parametrize("shape", _BEHAVIOUR_IDS, ids=shape_id)
    def test_every_expressed_fault_is_a_registered_fault(self, shape: Shape) -> None:
        unknown = BEHAVIOURS[shape].expresses - set(FAULTS)
        assert not unknown, f"{shape_id(shape)} expresses unregistered fault(s): {sorted(unknown)}"

    def test_a_fault_no_lane_can_express_would_be_caught(self) -> None:
        # Guard the guard: the loop above is only meaningful while some fault is expressed.
        assert any(behaviour.expresses for behaviour in BEHAVIOURS.values())


def _registered_default(detector: str, param: str, preset: str | None) -> object:
    """The value the detector registry actually offers for a parameter."""
    if preset is None:
        return inspect.signature(DETECTORS[detector].factory).parameters[param].default
    module = sys.modules[type(build_detector(detector, {})).__module__]
    return getattr(module.PRESETS[preset], param)


def _assert_boundary_is_checkable(calibration: Calibration, where: str) -> None:
    """One boundary against the registry, or against the narrow reason it cannot be.

    ``Calibration.param`` is ``None`` only when the detector *learns* its alarm line, so there
    is no registered default to compare against. That escape hatch has to be narrow or it
    becomes a way to opt out of ADR-0028 D6 entirely, so the substitute law is that the detector
    must actually be ``Fittable`` — the property that makes "its threshold is fitted, not
    configured" true rather than merely claimed.
    """
    assert calibration.detector in DETECTORS, f"{where}: {calibration.detector}"
    if calibration.param is None:
        detector = build_detector(calibration.detector, {})
        assert isinstance(detector, Fittable), (
            f"{where}: {calibration.detector} declares no param for its boundary, which is only "
            "honest for a detector that learns its threshold — this one does not fit, so quote "
            "the boundary against the registry default it does have"
        )
        return
    actual = _registered_default(calibration.detector, calibration.param, calibration.preset)
    assert actual == calibration.value, (
        f"{where}: declared {calibration.param}={calibration.value} but "
        f"{calibration.detector} offers {actual}"
    )


class TestCalibration:
    """A corpus discriminates only because its magnitudes straddle a detector's boundary.

    That coupling was a comment plus one hand-written test for one constant
    (``generate.THRESHOLD_HINT``). It is now declared per behaviour and checked for all of
    them, so a new lane's calibration cannot ship unpinned — and changing a detector's
    registered default fails loudly instead of quietly dissolving the corpus's whole point.
    """

    @pytest.mark.parametrize("shape", _BEHAVIOUR_IDS, ids=shape_id)
    def test_every_declared_calibration_matches_the_detector_it_names(self, shape: Shape) -> None:
        behaviour: Behaviour = BEHAVIOURS[shape]
        for calibration in behaviour.calibration:
            _assert_boundary_is_checkable(calibration, shape_id(shape))

    def test_at_least_one_behaviour_declares_a_calibration(self) -> None:
        # Otherwise the parametrized test above passes while asserting nothing.
        assert any(behaviour.calibration for behaviour in BEHAVIOURS.values())

    @pytest.mark.parametrize("shape", _BEHAVIOUR_IDS, ids=shape_id)
    def test_every_expressible_fault_has_a_boundary(self, shape: Shape) -> None:
        """The law that makes ``intensity`` dimensionless (ADR-0028 D6).

        A fault magnitude is now quoted in multiples of the detector boundary its behaviour
        declares for that kind. A shape added without one would fall back to whatever its handler
        hard-coded — which is exactly the state that let the trimodal compromise reach 316.7
        degrees against a threshold of 80 while its docstring called it modest.
        """
        behaviour: Behaviour = BEHAVIOURS[shape]
        for kind in sorted(behaviour.expresses):
            _assert_boundary_is_checkable(behaviour.boundary(kind), f"{shape_id(shape)}/{kind}")

    # There is deliberately no "calibration matches the boundaries" test any more.
    # `port.LaneBehaviour` derives `calibration` and `expresses` from the one boundary table
    # each lane declares, so the two cannot disagree — and a test whose whole job is to
    # re-establish a law the code could make true by construction is a sign the mechanism sat
    # one level too low (ADR-0028 D6).


def _recovered(renderer: Renderer, normalizer: Normalizer, lines: tuple[str, ...]) -> list[str]:
    """The events a rendered file parses back to, whatever order it was rendered in."""
    text = "\n".join((*renderer.header(), *lines)) + "\n"
    parsed = normalizer.normalize(text, source_format=renderer.source_format, origin="order")
    return sorted(_payloads(parsed.events))


def test_a_renderer_is_order_preserving() -> None:
    """Ordering belongs to the builder, so a renderer must not re-sort.

    The builder pools every device's events for a lane and sorts them by
    ``(timestamp, asset_id)`` so a multi-host capture is one time-ordered file. A renderer that
    sorted internally would fight that and, worse, would hide a builder that stopped sorting.

    ## Why this is no longer the mirror property

    It used to assert ``reversed(render(reversed(events))) == render(events)``, which is exactly
    "one line per event" in disguise: with *k* lines per event, reversing the output also
    reverses the lines *within* each event, so the mirror can only hold when k is 1. That was
    true of the first four dialects and is not a property of renderers — the meter normalizer
    differences a cumulative counter, so its inverse writes N+1 rows for N events and can never
    satisfy it.

    What is actually being protected is that the output *depends* on input order (a renderer
    that sorted internally would be order-blind) and that it loses nothing whichever order it is
    handed. Both hold for any k, and together they catch the same defect: an internal sort makes
    the first assertion fail, because the two orders would render identically.
    """
    ordered: list[str] = []
    for source_format in _RENDERER_IDS:
        renderer: Renderer = RENDERERS[source_format]
        (case,) = build(one_lane_scenario(source_format)).cases
        name = normalizer_for(source_format)
        assert name is not None
        normalizer = build_normalizer(name)
        events = normalizer.normalize(
            case.inputs[0].text, source_format=source_format, origin="order"
        ).events
        assert events, f"{source_format}: nothing to render"

        forward = renderer.render(events)
        backward = renderer.render(list(reversed(events)))

        # Nothing is lost whichever order it was handed: the reversed file still parses back to
        # the events it was given. Compared against the *input* rather than against the forward
        # render, which is both stronger (the forward render is already pinned by the round-trip
        # law above) and one normalize cheaper.
        assert _recovered(renderer, normalizer, backward) == sorted(_payloads(events)), (
            source_format
        )

        if len(events) > 1:
            # The order-blindness check proper. One event cannot demonstrate an ordering, and a
            # short fixture on a slow-sampling lane legitimately yields one.
            assert forward != backward, f"{source_format} renders one file for either order"
            ordered.append(source_format)

    # Guard the guard: if every fixture shrank to a single event the loop above would assert
    # nothing about ordering at all.
    assert ordered, "no format produced enough events to demonstrate order-sensitivity"
