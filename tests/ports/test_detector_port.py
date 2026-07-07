"""Detector PORT suite: every implementation must pass every test here.

To add an adapter: append a DetectorCase whose ``triggering_events`` are
guaranteed to produce at least one detection for that implementation.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from iotshield.contracts import DetectionResult, Lane, NormalizedEvent
from iotshield.detect import Detector, StubThresholdDetector

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def event(
    *, value: float | None, lane: Lane = Lane.TELEMETRY, offset_s: int = 0
) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id="pump-02",
        source_type=lane,
        source_format="test",
        metric="Pressure",
        value=value,
        raw=f"test-record value={value}",
    )


@dataclass(frozen=True)
class DetectorCase:
    make: Callable[[], Detector]
    triggering_events: Sequence[NormalizedEvent]  # must yield >= 1 detection


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
        ),
        id="stub-threshold",
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


def test_empty_input_yields_nothing(case: DetectorCase) -> None:
    assert case.make().detect([]) == []


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
