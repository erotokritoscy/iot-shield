"""Stub-specific behavior (trigger logic, bucketing, severity mapping).

Port-level invariants live in tests/ports/ — these tests cover only what is
particular to the stub implementations themselves.
"""

from datetime import UTC, datetime, timedelta

from iotshield.alert import StubAlertEngine
from iotshield.contracts import DetectionResult, Lane, NormalizedEvent
from iotshield.detect import StubThresholdDetector
from iotshield.normalize import STUB_SOURCE_FORMAT, StubCsvNormalizer

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def telemetry_event(value: float) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0,
        asset_id="pump-02",
        source_type=Lane.TELEMETRY,
        source_format="test",
        metric="Pressure",
        value=value,
        raw=f"value={value}",
    )


def detection_at(offset: timedelta) -> DetectionResult:
    return DetectionResult(
        asset_id="pump-02",
        window_start=T0 + offset,
        window_end=T0 + offset,
        lane=Lane.TELEMETRY,
        method="test_method",
        score=96.0,
        threshold=80.0,
    )


class TestStubCsvNormalizer:
    def test_parses_fields_into_event(self) -> None:
        line = "2026-07-07T12:00:00+00:00,pump-02,Pressure,0.38"
        result = StubCsvNormalizer().normalize(
            line, source_format=STUB_SOURCE_FORMAT, origin="test://x"
        )
        (event,) = result.events
        assert event.asset_id == "pump-02"
        assert event.metric == "Pressure"
        assert event.value == 0.38
        assert event.source_type == Lane.TELEMETRY
        assert event.raw == line

    def test_contract_violation_becomes_malformed_not_crash(self) -> None:
        # Empty asset_id violates the contract; the stub converts it to a reject.
        result = StubCsvNormalizer().normalize(
            "2026-07-07T12:00:00+00:00,,Pressure,0.38",
            source_format=STUB_SOURCE_FORMAT,
            origin="test://x",
        )
        assert result.events == []
        assert len(result.malformed) == 1

    def test_non_finite_value_becomes_malformed(self) -> None:
        # 'nan'/'inf' parse as floats but poison JSON/storage; the contract
        # rejects them, so the stub captures them as rejects rather than events.
        result = StubCsvNormalizer().normalize(
            "2026-07-07T12:00:00+00:00,pump-02,Pressure,nan",
            source_format=STUB_SOURCE_FORMAT,
            origin="test://x",
        )
        assert result.events == []
        assert len(result.malformed) == 1

    def test_epoch_number_timestamp_becomes_malformed(self) -> None:
        # A column-shifted line whose first field is a number would otherwise
        # become a silent 1970-dated event; it must land in the audit view.
        result = StubCsvNormalizer().normalize(
            "1720000000,pump-02,Pressure,0.38",
            source_format=STUB_SOURCE_FORMAT,
            origin="test://x",
        )
        assert result.events == []
        assert len(result.malformed) == 1


class TestStubThresholdDetector:
    def test_fires_strictly_above_threshold_only(self) -> None:
        detector = StubThresholdDetector(threshold=80.0)
        events = [telemetry_event(79.9), telemetry_event(80.0), telemetry_event(80.1)]
        detections = detector.detect(events)
        assert [d.score for d in detections] == [80.1]

    def test_detection_is_point_shaped(self) -> None:
        detector = StubThresholdDetector(threshold=80.0)
        (det,) = detector.detect([telemetry_event(99.0)])
        assert det.window_start == det.window_end == T0


class TestStubAlertEngine:
    def test_nearby_detections_share_one_alert(self) -> None:
        engine = StubAlertEngine(bucket=timedelta(minutes=5))
        detections = [detection_at(timedelta(0)), detection_at(timedelta(minutes=1))]
        assert len(engine.build_alerts(detections, {})) == 1

    def test_distant_detections_get_separate_alerts(self) -> None:
        engine = StubAlertEngine(bucket=timedelta(minutes=5))
        detections = [detection_at(timedelta(0)), detection_at(timedelta(hours=2))]
        assert len(engine.build_alerts(detections, {})) == 2

    def test_score_components_sum_to_priority(self) -> None:
        (alert,) = StubAlertEngine().build_alerts([detection_at(timedelta(0))], {})
        assert alert.priority_score == min(sum(alert.score_components.values()), 100.0)
