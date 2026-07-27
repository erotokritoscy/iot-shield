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

import pytest

from iotshield.capabilities import formats_in
from iotshield.contracts import Lane
from iotshield.eval.generate import (
    BEHAVIOURS,
    FAULTS,
    RENDERERS,
    Device,
    Renderer,
    Scenario,
    build,
    lanes_of,
)
from iotshield.eval.generate.port import Behaviour
from iotshield.pipeline.registry import DETECTORS, NORMALIZERS, build_detector, build_normalizer
from tests.eval.generate.conftest import scenario_of

# One device that is legitimate on every lane: an IP inside the default monitored networks, so
# the flow normalizer's orientation rule attributes its records to *it* rather than the peer
# (ADR-0023 D2). Using one device for every lane keeps this suite free of per-lane special
# cases, which is what lets it police a lane nobody has written yet.
DEVICE = Device(asset_id="10.0.0.24", kind="camera", role="round-trip fixture")

_RENDERER_IDS = sorted(RENDERERS)
_BEHAVIOUR_IDS = sorted(BEHAVIOURS)


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


def one_lane_scenario(source_format: str) -> Scenario:
    return scenario_of(DEVICE, formats=(source_format,), name="round-trip", seed=11)


class TestRegistryCompleteness:
    def test_every_registered_renderer_joins_this_suite(self) -> None:
        assert _RENDERER_IDS, "no renderers registered: this whole suite would be vacuous"

    def test_every_registered_behaviour_joins_this_suite(self) -> None:
        assert _BEHAVIOUR_IDS, "no behaviours registered: this whole suite would be vacuous"

    @pytest.mark.parametrize("lane", _BEHAVIOUR_IDS)
    def test_every_behaviours_lane_has_at_least_one_renderer(self, lane: Lane) -> None:
        # A behaviour with no renderer can never reach a bundle: it would model a modality
        # nothing could write down, so a scenario naming it would fail at generation.
        assert [name for name, renderer in RENDERERS.items() if renderer.lane is lane], lane

    @pytest.mark.parametrize("source_format", _RENDERER_IDS)
    def test_every_renderers_lane_has_a_behaviour(self, source_format: str) -> None:
        assert RENDERERS[source_format].lane in BEHAVIOURS, source_format


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
        name = normalizer_for(source_format)
        assert name is not None
        (case,) = build(one_lane_scenario(source_format)).cases
        result = build_normalizer(name).normalize(
            case.inputs[0].text, source_format=source_format, origin="round-trip"
        )

        assert not result.malformed, [reject.error for reject in result.malformed[:3]]
        assert result.events, "the generated text parsed to nothing at all"
        # The lane the renderer declares is the lane the pipeline actually derives.
        assert {event.source_type for event in result.events} == {RENDERERS[source_format].lane}
        # The device the scenario named is the device the pipeline attributes events to. For
        # the flow lane this is a real hazard rather than a formality: orientation picks the
        # asset from `home_networks`, so a badly chosen asset id lands on the peer instead.
        assert {event.asset_id for event in result.events} == {DEVICE.asset_id}
        # And the answer key's clock is exactly the instants the pipeline sees, so point
        # metrics are scored over rows that genuinely exist.
        assert {event.timestamp for event in result.events} == {
            moment for moment, _ in case.truth.assets[0].timeline
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

    @pytest.mark.parametrize("lane", _BEHAVIOUR_IDS)
    def test_every_expressed_fault_is_a_registered_fault(self, lane: Lane) -> None:
        unknown = BEHAVIOURS[lane].expresses - set(FAULTS)
        assert not unknown, f"{lane} expresses unregistered fault(s): {sorted(unknown)}"

    def test_a_fault_no_lane_can_express_would_be_caught(self) -> None:
        # Guard the guard: the loop above is only meaningful while some fault is expressed.
        assert any(behaviour.expresses for behaviour in BEHAVIOURS.values())


def _registered_default(detector: str, param: str, preset: str | None) -> object:
    """The value the detector registry actually offers for a parameter."""
    if preset is None:
        return inspect.signature(DETECTORS[detector].factory).parameters[param].default
    module = sys.modules[type(build_detector(detector, {})).__module__]
    return getattr(module.PRESETS[preset], param)


class TestCalibration:
    """A corpus discriminates only because its magnitudes straddle a detector's boundary.

    That coupling was a comment plus one hand-written test for one constant
    (``generate.THRESHOLD_HINT``). It is now declared per behaviour and checked for all of
    them, so a new lane's calibration cannot ship unpinned — and changing a detector's
    registered default fails loudly instead of quietly dissolving the corpus's whole point.
    """

    @pytest.mark.parametrize("lane", _BEHAVIOUR_IDS)
    def test_every_declared_calibration_matches_the_detector_it_names(self, lane: Lane) -> None:
        behaviour: Behaviour = BEHAVIOURS[lane]
        for calibration in behaviour.calibration:
            assert calibration.detector in DETECTORS, calibration.detector
            actual = _registered_default(
                calibration.detector, calibration.param, calibration.preset
            )
            assert actual == calibration.value, (
                f"{lane}: declared {calibration.param}={calibration.value} but "
                f"{calibration.detector} offers {actual}"
            )

    def test_at_least_one_behaviour_declares_a_calibration(self) -> None:
        # Otherwise the parametrized test above passes while asserting nothing.
        assert any(behaviour.calibration for behaviour in BEHAVIOURS.values())


def test_a_renderer_is_order_preserving() -> None:
    """Ordering belongs to the builder, so a renderer must not re-sort.

    The builder pools every device's events for a lane and sorts them by
    ``(timestamp, asset_id)`` so a multi-host capture is one time-ordered file. A renderer that
    sorted internally would fight that and, worse, would hide a builder that stopped sorting.
    """
    for source_format in _RENDERER_IDS:
        renderer: Renderer = RENDERERS[source_format]
        (case,) = build(one_lane_scenario(source_format)).cases
        name = normalizer_for(source_format)
        assert name is not None
        events = (
            build_normalizer(name)
            .normalize(case.inputs[0].text, source_format=source_format, origin="order")
            .events
        )
        reversed_lines = renderer.render(list(reversed(events)))
        forward_lines = renderer.render(events)
        assert list(reversed(reversed_lines)) == list(forward_lines), source_format
