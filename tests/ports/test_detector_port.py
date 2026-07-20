"""Detector PORT suite: every implementation must pass every test here.

To add an adapter: append a DetectorCase whose ``triggering_events`` are
guaranteed to produce at least one detection for that implementation.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from iotshield.contracts import DetectionResult, Lane, NormalizedEvent
from iotshield.detect import (
    Detector,
    Fittable,
    FlowBurstDetector,
    LogBurstDetector,
    StubThresholdDetector,
    Team3ZScoreDetector,
)
from iotshield.detect.team3.train import train_artifact
from iotshield.flow import METADATA_KEY, Direction, FlowRecord

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


# A hermetic team3 artifact: trained on a synthetic single-channel normal series
# (tiny window so the case stays small), with no committed file involved.
_TEAM3_ARTIFACT = train_artifact(
    pd.DataFrame({"Pressure": np.random.default_rng(0).normal(10.0, 0.5, 300)}),
    channels=["Pressure"],
    window_size=3,
    lag_steps=[1],
    fft_enabled=False,
    percentile=99.0,
)
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
    pytest.param(
        DetectorCase(
            make=lambda: Team3ZScoreDetector(_TEAM3_ARTIFACT),
            triggering_events=[event(value=v, offset_s=i) for i, v in enumerate(_TEAM3_VALUES)],
            fit_events=[
                event(value=float(v), offset_s=i)
                for i, v in enumerate(np.random.default_rng(1).normal(10.0, 0.5, 200))
            ],
        ),
        id="team3-zscore",
    ),
    pytest.param(
        DetectorCase(
            make=lambda: LogBurstDetector(preset="auth_failure", window_minutes=5.0, min_count=5),
            triggering_events=_LOG_BURST_EVENTS,
        ),
        id="log-burst",
    ),
    pytest.param(
        DetectorCase(
            make=lambda: FlowBurstDetector(preset="fan_out"),
            triggering_events=_FLOW_FANOUT_EVENTS,
        ),
        id="flow-burst",
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
