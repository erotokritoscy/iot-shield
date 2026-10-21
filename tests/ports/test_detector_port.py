"""Detector PORT suite: every implementation must pass every test here.

To add an adapter: append a DetectorCase whose ``triggering_events`` are
guaranteed to produce at least one detection for that implementation.
"""

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import numpy as np
import pytest

from iotshield.capabilities import payloads_in
from iotshield.contracts import DetectionResult, Lane, NormalizedEvent
from iotshield.detect import (
    Detector,
    Fittable,
    FlowBurstDetector,
    LogBurstDetector,
    MeterQualityDetector,
    RuleDetector,
    StubThresholdDetector,
)
from iotshield.detect._fitted import NotFitted, is_fitted
from iotshield.flow import METADATA_KEY, Direction, FlowRecord
from iotshield.pipeline.registry import DETECTORS, build_detector
from tests import meter_fixtures
from tests.team3_fixtures import make_detector


def _learns(registry_name: str) -> bool:
    """Whether the registered adapter carries hyperparameters (ADR-0022).

    Derived from the registry, not from a case field, so that whether THE LAW *applies*
    to an adapter is a fact about the adapter — a learning adapter cannot exempt itself
    from the law by leaving a case field unset (which it previously could).
    """
    return DETECTORS[registry_name].params_type is not None


T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def event(
    *,
    value: float | None,
    lane: Lane = Lane.TELEMETRY,
    offset_s: int = 0,
    message: str | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id="pump-02",
        source_type=lane,
        source_format="test",
        metric="Pressure",
        value=value,
        message=message,
        raw=f"test-record value={value}",
    )


@dataclass(frozen=True)
class DetectorCase:
    make: Callable[[], Detector]
    triggering_events: Sequence[NormalizedEvent]  # must yield >= 1 detection
    # Normal-looking events a Fittable adapter can actually learn a baseline from. Required
    # only for adapters implementing the optional capability: `triggering_events` are chosen
    # to fire, which usually makes them unfit for training (team3 rejects an all-constant
    # frame as degenerate, correctly).
    fit_events: Sequence[NormalizedEvent] | None = None
    # Two REGISTRY param sets differing in one hyperparameter, which must fit differently.
    # The behavioural test of ADR-0022 rule 2 — see the law test below for why this shape.
    distinguishing_params: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
    registry_name: str = ""


# Flat-normal readings with a sustained spike in the middle -> at least one episode.
_TEAM3_VALUES = [10.0] * 8 + [55.0, 57.0, 56.0] + [10.0] * 4

# Six auth failures 10s apart: one burst episode well inside the 5-minute window. They keep
# the contract's default INFO severity (as sshd really logs them), which is also why this
# case would be invisible to a severity-based rule.
_LOG_BURST_EVENTS = [
    event(
        value=None,
        lane=Lane.LOG,
        offset_s=index * 10,
        message=f"Failed password for root from 10.0.0.9 port {52000 + index} ssh2",
    )
    for index in range(6)
]


def flow_event(offset_s: int, peer: str, port: int = 23, bytes_out: int = 44) -> NormalizedEvent:
    record = FlowRecord(
        asset_ip="pump-02",
        peer_ip=peer,
        protocol="tcp",
        direction=Direction.OUTBOUND,
        peer_port=port,
        bytes_out=bytes_out,
    )
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id="pump-02",
        source_type=Lane.FLOW,
        source_format="flow-csv",
        metric="bytes_out",
        value=float(bytes_out),
        message=record.summary(),
        raw=f"flow to {peer}",
        metadata={METADATA_KEY: record.to_metadata()},
    )


# 25 distinct destinations in 25s: one fan-out episode, well past the boundary of 20.
_FLOW_FANOUT_EVENTS = [flow_event(index, f"203.0.113.{index}") for index in range(25)]

# The same sweep, which the packaged `telnet-outbound-propagation` rule matches on every
# flow (port 23, outbound), plus one bulk transfer that trips `high-upload-volume`. Two
# rules firing means the case exercises the per-rule loop, not just a single predicate.
_RULE_FLOW_EVENTS = [
    *_FLOW_FANOUT_EVENTS,
    flow_event(60, "203.0.113.200", port=443, bytes_out=250_000),
]

# A day of hourly SensiPro-style electricity readings with a three-hour silence (heartbeat)
# and one 50 kWh hour (high): both of meter_quality's rules fire.
_METER_QUALITY_EVENTS = [
    meter_fixtures.meter_event(
        T0 + timedelta(hours=hour), 50.0 if hour == 20 else 1.0, 100.0 + hour
    )
    for hour in range(24)
    if hour not in (10, 11, 12)
]

CASES = [
    pytest.param(
        DetectorCase(
            make=lambda: StubThresholdDetector(threshold=80.0),
            triggering_events=[
                event(value=10.0, offset_s=0),
                event(value=99.0, offset_s=1),
                event(value=120.0, offset_s=2),
                event(value=None, offset_s=3),
                event(value=500.0, lane=Lane.LOG, offset_s=4),  # foreign lane
            ],
            registry_name="stub_threshold",
        ),
        id="stub-threshold",
    ),
    pytest.param(
        DetectorCase(
            make=lambda: make_detector(["Pressure"], window=3),
            triggering_events=[event(value=v, offset_s=i) for i, v in enumerate(_TEAM3_VALUES)],
            fit_events=[
                event(value=float(v), offset_s=i)
                for i, v in enumerate(np.random.default_rng(1).normal(10.0, 0.5, 200))
            ],
            distinguishing_params=(
                {"window_size": 4, "lag_steps": [1]},
                {"window_size": 30, "lag_steps": [1]},
            ),
            registry_name="team3_zscore",
        ),
        id="team3-zscore",
    ),
    pytest.param(
        DetectorCase(
            make=lambda: LogBurstDetector(preset="auth_failure", window_minutes=5.0, min_count=5),
            triggering_events=_LOG_BURST_EVENTS,
            registry_name="log_burst",
        ),
        id="log-burst",
    ),
    pytest.param(
        DetectorCase(
            make=lambda: FlowBurstDetector(preset="fan_out"),
            triggering_events=_FLOW_FANOUT_EVENTS,
            # No distinguishing_params: this detector learns nothing, so it declares
            # neither Fittable nor hyperparameters and skips the ADR-0022 law tests —
            # the same position stub_threshold and log_burst are in.
            registry_name="flow_burst",
        ),
        id="flow-burst",
    ),
    pytest.param(
        DetectorCase(
            make=meter_fixtures.make_detector,
            triggering_events=meter_fixtures.leaking_events(),
            fit_events=meter_fixtures.clean_events(),
            # n_estimators is the one hyperparameter that changes the fitted forest without
            # changing the feature schema, so the two fits stay comparable while having to
            # differ. The rest are the fixtures' small-and-fast settings (see
            # tests/meter_fixtures.py) and are identical in both.
            distinguishing_params=(
                {**meter_fixtures.FIXTURE_PARAMS, "n_estimators": 30},
                {**meter_fixtures.FIXTURE_PARAMS, "n_estimators": 250},
            ),
            registry_name="meter_iforest",
        ),
        id="meter-iforest",
    ),
    pytest.param(
        DetectorCase(
            make=MeterQualityDetector,
            triggering_events=_METER_QUALITY_EVENTS,
            # Written rules, like `rules`: nothing is fitted, so no ADR-0022 law applies.
            registry_name="meter_quality",
        ),
        id="meter-quality",
    ),
    pytest.param(
        DetectorCase(
            make=lambda: RuleDetector(lane=Lane.FLOW),
            triggering_events=_RULE_FLOW_EVENTS,
            # No distinguishing_params: a rule learns nothing from data — its thresholds are
            # written down by a human — so it declares neither Fittable nor hyperparameters
            # and skips the ADR-0022 law tests, the same position log_burst is in.
            registry_name="rules",
        ),
        id="rules",
    ),
]


@pytest.fixture(params=CASES)
def case(request: pytest.FixtureRequest) -> DetectorCase:
    return request.param


def test_produces_detections_on_triggering_input(case: DetectorCase) -> None:
    assert case.make().detect(case.triggering_events)


def test_lane_and_method_stamped(case: DetectorCase) -> None:
    detector = case.make()
    for detection in detector.detect(case.triggering_events):
        assert detection.lane == detector.lane
        assert detection.method == detector.method
        assert detection.method


def test_asset_ids_come_from_input(case: DetectorCase) -> None:
    detector = case.make()
    known_assets = {e.asset_id for e in case.triggering_events}
    for detection in detector.detect(case.triggering_events):
        assert detection.asset_id in known_assets


def test_windows_within_input_time_range(case: DetectorCase) -> None:
    detector = case.make()
    lane_times = [e.timestamp for e in case.triggering_events if e.source_type == detector.lane]
    for detection in detector.detect(case.triggering_events):
        assert min(lane_times) <= detection.window_start
        assert detection.window_end <= max(lane_times)


def test_score_exceeds_threshold(case: DetectorCase) -> None:
    for detection in case.make().detect(case.triggering_events):
        assert detection.score > detection.threshold


def test_foreign_lane_events_are_ignored(case: DetectorCase) -> None:
    detector = case.make()
    foreign = next(lane for lane in Lane if lane != detector.lane)
    foreign_only = [e.model_copy(update={"source_type": foreign}) for e in case.triggering_events]
    assert detector.detect(foreign_only) == []


def test_a_declared_payload_is_actually_required(case: DetectorCase) -> None:
    """The payload axis's port law, matching the foreign-lane law above (ADR-0035 D2).

    A detector that declares ``payload:meter`` is promising the guard something: that an
    event on its lane *without* that vocabulary is one it declines rather than crashes on.
    The compatibility guard reports the mismatch as a WARNING on exactly that basis — nothing
    breaks, the detector simply judges nothing — and until now no test required it. Every
    adapter happened to defend itself (`_is_meter_reading`, `FlowRecord.from_event -> None`),
    so the guarantee was true by habit; a future adapter indexing ``metadata["meter"]``
    directly would crash on a config the guard deliberately allows, which would make the
    severity choice retroactively wrong rather than the adapter wrong.

    Derived from the registry like ``_learns``, so a new declaration is covered on arrival.
    """
    if not case.registry_name:
        return
    declared = payloads_in(DETECTORS[case.registry_name].capability({}).consumes)
    if not declared:
        return
    stripped = [
        item.model_copy(
            update={"metadata": {k: v for k, v in item.metadata.items() if k not in declared}}
        )
        for item in case.triggering_events
    ]
    assert case.make().detect(stripped) == [], (
        f"{case.registry_name} declares payload(s) {sorted(declared)} but still judged events "
        f"that carry none — the guard's WARNING (rather than ERROR) assumes it will not"
    )


def test_empty_input_yields_nothing(case: DetectorCase) -> None:
    assert case.make().detect([]) == []


def test_every_detection_explains_itself(case: DetectorCase) -> None:
    """ADR-0032 D7: one analyst-readable sentence per detection, from every adapter.

    Enforced here rather than asserted in prose because that is the difference between a
    convention and a law — CLAUDE.md and the ADR both say "every detector", and four of six
    did until this test existed. The supporting detail stays per-detector (a z-score, a
    feature attribution and a peer count are genuinely different things); only the headline
    is universal, because it is the one thing every consumer can render.
    """
    for detection in case.make().detect(case.triggering_events):
        explanation = detection.evidence.get("explanation")
        assert isinstance(explanation, str) and explanation.strip(), (
            f"{detection.method} emitted a detection with no 'explanation' in evidence"
        )


def test_fitting_yields_a_detector_that_still_obeys_every_law(case: DetectorCase) -> None:
    """A fitted detector is still a detector — fitting must not let one escape the port.

    ``Fittable`` is optional (ADR-0021 D3), so adapters that do not implement it skip this.
    Those that do must return something that stamps its lane/method, keeps windows in range,
    clears its threshold, and does not mutate the original — otherwise fitting would be a
    hole in the suite that every other law is checked through.
    """
    detector = case.make()
    if not isinstance(detector, Fittable):
        pytest.skip("adapter does not implement the optional Fittable capability")
    assert case.fit_events is not None, "a Fittable adapter's case must supply fit_events"

    fitted = detector.fit(case.fit_events)
    assert fitted is not detector  # a new object, so no hidden fitted state

    lane_times = [e.timestamp for e in case.triggering_events if e.source_type == fitted.lane]
    for detection in fitted.detect(case.triggering_events):
        assert detection.lane == fitted.lane
        assert detection.method == fitted.method
        assert detection.score > detection.threshold
        assert min(lane_times) <= detection.window_start
        assert detection.window_end <= max(lane_times)


def _fingerprint(detections: list[DetectionResult]) -> list[tuple[Any, ...]]:
    return [(d.window_start, d.window_end, d.score, d.threshold) for d in detections]


def test_hyperparameters_survive_fitting(case: DetectorCase) -> None:
    """THE LAW (ADR-0022 rule 2): ``fit()`` uses CONSTRUCTION, never prior fitted state.

    Checked behaviourally rather than by introspection, which is what makes it general.
    Build the adapter twice through the registry, differing in exactly one hyperparameter;
    fit both on the same data. If ``fit()`` honoured construction the two fitted detectors
    must differ. If it instead reached into the artifact it was loaded from — the bug this
    rule exists to forbid, and the one team3 actually had — both fits would be identical
    and this fails.

    Whether the law *applies* is derived from the registry (does the adapter carry
    hyperparameters), not from the case: a learning adapter that supplied no
    ``distinguishing_params`` used to skip silently, so it could exempt itself from the one
    law the project calls THE LAW. Now it fails instead.
    """
    if not _learns(case.registry_name):
        pytest.skip("adapter carries no hyperparameters, so the law does not apply")
    assert case.distinguishing_params is not None, (
        f"{case.registry_name} carries hyperparameters but its case supplies no "
        "distinguishing_params to check THE LAW with"
    )
    assert case.fit_events is not None, "a distinguishing case must supply fit_events"

    first_params, second_params = case.distinguishing_params
    first = build_detector(case.registry_name, first_params)
    second = build_detector(case.registry_name, second_params)
    assert isinstance(first, Fittable) and isinstance(second, Fittable)

    fitted_first = first.fit(case.fit_events)
    fitted_second = second.fit(case.fit_events)
    assert _fingerprint(fitted_first.detect(case.triggering_events)) != _fingerprint(
        fitted_second.detect(case.triggering_events)
    )


def test_a_detector_built_without_a_cached_fit_says_so(case: DetectorCase) -> None:
    """Untrained is a *reportable state*, not an empty result (ADR-0022).

    Only adapters that can be constructed from hyperparameters alone have this state; the
    rest are always ready, which ``is_fitted`` reports by absence.
    """
    if not _learns(case.registry_name):
        pytest.skip("adapter has no untrained state")
    assert case.distinguishing_params is not None, (
        f"{case.registry_name} carries hyperparameters but its case supplies none"
    )
    assert case.fit_events is not None

    untrained = build_detector(case.registry_name, case.distinguishing_params[0])
    assert is_fitted(untrained) is False
    with pytest.raises(NotFitted):
        untrained.detect(case.triggering_events)

    assert isinstance(untrained, Fittable)
    assert is_fitted(untrained.fit(case.fit_events)) is True


def test_deterministic_apart_from_generated_ids(case: DetectorCase) -> None:
    detector = case.make()

    def fingerprint(
        detections: list[DetectionResult],
    ) -> list[tuple[str, datetime, datetime, str, float, float]]:
        return [
            (d.asset_id, d.window_start, d.window_end, d.method, d.score, d.threshold)
            for d in detections
        ]

    assert fingerprint(detector.detect(case.triggering_events)) == fingerprint(
        detector.detect(case.triggering_events)
    )


def test_output_is_independent_of_input_order(case: DetectorCase) -> None:
    """The port's law 4 in full: the set of judgments must not depend on arrival order.

    ``group_by_asset`` owns this for every adapter (foreign-lane isolation, asset sort, and
    time sort within an asset). A windowed detector fed unordered events does not fail
    loudly, it over-fires, so this is the test that keeps a new adapter from shipping that.
    """
    detector = case.make()
    shuffled = list(case.triggering_events)
    random.Random(0).shuffle(shuffled)
    assert sorted(_fingerprint(detector.detect(shuffled))) == sorted(
        _fingerprint(detector.detect(case.triggering_events))
    )


def test_every_registered_detector_joins_this_suite() -> None:
    """CLAUDE.md golden rule 2 made enforceable: a registered adapter with no port case
    would otherwise ship with zero port-law coverage and a green gate."""
    covered = {cast(DetectorCase, param.values[0]).registry_name for param in CASES}
    assert covered == set(DETECTORS), f"detectors missing a port case: {set(DETECTORS) - covered}"
