"""AlertEngine PORT suite: every implementation must pass every test here.

To add an adapter: append an AlertEngineCase. The corroboration-monotonicity
test encodes the product's core correlation promise: corroborating detections
boost priority, they never gate detection.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from iotshield.alert import AlertEngine, StubAlertEngine
from iotshield.contracts import Asset, Criticality, DetectionResult, Lane

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)

ASSETS = {
    "pump-02": Asset(
        asset_id="pump-02", name="Feed pump 2", asset_type="pump", criticality=Criticality.HIGH
    ),
    "gw-01": Asset(asset_id="gw-01", name="Gateway 1", asset_type="gateway"),
}


def detection(
    *,
    asset_id: str = "pump-02",
    lane: Lane = Lane.TELEMETRY,
    offset_s: int = 0,
    duration_s: int = 30,
    score: float = 96.0,
    threshold: float = 80.0,
    method: str = "test_method",
) -> DetectionResult:
    start = T0 + timedelta(seconds=offset_s)
    return DetectionResult(
        asset_id=asset_id,
        window_start=start,
        window_end=start + timedelta(seconds=duration_s),
        lane=lane,
        method=method,
        score=score,
        threshold=threshold,
    )


@dataclass(frozen=True)
class AlertEngineCase:
    make: Callable[[], AlertEngine]


CASES = [pytest.param(AlertEngineCase(make=StubAlertEngine), id="stub-alert-engine")]


@pytest.fixture(params=CASES)
def case(request: pytest.FixtureRequest) -> AlertEngineCase:
    return request.param


def test_every_detection_covered_by_exactly_one_alert(case: AlertEngineCase) -> None:
    detections = [
        detection(offset_s=0),
        detection(offset_s=10, lane=Lane.LOG),
        detection(asset_id="gw-01", offset_s=0),
        detection(offset_s=7200),  # far-away window, same asset
    ]
    alerts = case.make().build_alerts(detections, ASSETS)
    covered = [d_id for alert in alerts for d_id in alert.detection_ids]
    assert sorted(covered) == sorted(d.detection_id for d in detections)  # D3: no hidden gate


def test_asset_and_window_lineage_consistent(case: AlertEngineCase) -> None:
    detections = [detection(offset_s=0), detection(offset_s=10, lane=Lane.LOG)]
    by_id = {d.detection_id: d for d in detections}
    for alert in case.make().build_alerts(detections, ASSETS):
        for d_id in alert.detection_ids:
            source = by_id[d_id]
            assert source.asset_id == alert.asset_id
            assert alert.window_start <= source.window_start
            assert source.window_end <= alert.window_end


def test_corroboration_never_lowers_priority(case: AlertEngineCase) -> None:
    engine = case.make()
    lone = detection(offset_s=0, lane=Lane.TELEMETRY)
    corroborating = detection(offset_s=10, lane=Lane.LOG)
    (single,) = engine.build_alerts([lone], ASSETS)
    (corroborated,) = engine.build_alerts([lone, corroborating], ASSETS)
    assert corroborated.priority_score >= single.priority_score


def test_unknown_asset_degrades_gracefully(case: AlertEngineCase) -> None:
    alerts = case.make().build_alerts([detection(asset_id="never-registered")], ASSETS)
    assert len(alerts) == 1
    assert alerts[0].asset_id == "never-registered"


def test_empty_input_yields_nothing(case: AlertEngineCase) -> None:
    assert case.make().build_alerts([], ASSETS) == []


def test_alerts_ranked_by_priority(case: AlertEngineCase) -> None:
    detections = [
        detection(score=81.0),  # barely above threshold
        detection(asset_id="gw-01", score=160.0),  # strong margin
        detection(asset_id="gw-01", offset_s=7200, score=81.0, lane=Lane.LOG),
    ]
    alerts = case.make().build_alerts(detections, ASSETS)
    scores = [alert.priority_score for alert in alerts]
    assert scores == sorted(scores, reverse=True)
