"""Round-trip tests: contract object -> row -> contract object, lossless.

These are the storage layer's crown jewels — they pin that the ORM projection
preserves every field, through the repository path a real pipeline run takes.
Instances use every optional field so nothing is vacuously 'preserved'.
"""

from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from iotshield.contracts import (
    AlertSeverity,
    AlertStatus,
    Asset,
    Criticality,
    DetectionResult,
    EventKind,
    Lane,
    MalformedRecord,
    NormalizedEvent,
    PrioritizedAlert,
    Severity,
)
from iotshield.storage import repository

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def test_asset_round_trip(session: Session) -> None:
    asset = Asset(
        asset_id="pump-02",
        name="Feed pump 2",
        asset_type="pump",
        location="line-1",
        criticality=Criticality.HIGH,
        metadata={"vendor": "SKF", "commissioned": "2024-02-08"},
    )
    repository.upsert_assets(session, [asset])
    assert repository.list_assets(session) == [asset]


def test_event_round_trip(session: Session) -> None:
    event = NormalizedEvent(
        timestamp=T0,
        asset_id="pump-02",
        source_type=Lane.TELEMETRY,
        source_format="stub-csv",
        metric="Pressure",
        value=0.38,
        severity=Severity.WARNING,
        message="pressure reading",
        raw="2026-07-07T12:00:00+00:00,pump-02,Pressure,0.38",
        metadata={"line_number": 42},
    )
    repository.insert_events(session, [event])
    assert list(repository.get_events(session)) == [event]


def test_event_timestamp_normalized_to_utc_across_round_trip(session: Session) -> None:
    athens = timezone(timedelta(hours=3))
    event = NormalizedEvent(
        timestamp=datetime(2026, 7, 7, 15, 0, 0, tzinfo=athens),  # same instant as T0
        asset_id="pump-02",
        source_type=Lane.TELEMETRY,
        source_format="stub-csv",
        raw="r",
    )
    repository.insert_events(session, [event])
    (restored,) = repository.get_events(session)
    assert restored.timestamp == T0
    assert restored.timestamp.tzinfo == UTC


def test_malformed_round_trip(session: Session) -> None:
    record = MalformedRecord(
        raw="garbage;;;line",
        error="expected 4 fields, got 1",
        source_format="stub-csv",
        origin="file://demo.csv",
    )
    repository.insert_malformed(session, [record])
    assert list(repository.list_malformed(session)) == [record]


def test_detection_round_trip(session: Session) -> None:
    detection = DetectionResult(
        asset_id="pump-02",
        window_start=T0,
        window_end=T0 + timedelta(minutes=15),
        lane=Lane.TELEMETRY,
        method="lstm_ae",
        score=0.91,
        threshold=0.42,
        event_kind=EventKind.COLLECTIVE,
        evidence={"top_channel": "Pressure", "channel_error": 0.8},
        metadata={"artifact": "artifacts/skab/lstm_ae.pt"},
    )
    repository.insert_detections(session, [detection])
    assert list(repository.get_detections(session)) == [detection]


def test_alert_round_trip(session: Session) -> None:
    detections = [
        DetectionResult(
            asset_id="pump-02",
            window_start=T0,
            window_end=T0,
            lane=lane,
            method="m",
            score=96.0,
            threshold=80.0,
        )
        for lane in (Lane.TELEMETRY, Lane.LOG)
    ]
    repository.insert_detections(session, detections)
    alert = PrioritizedAlert(
        asset_id="pump-02",
        window_start=T0,
        window_end=T0 + timedelta(minutes=5),
        severity=AlertSeverity.CRITICAL,
        priority_score=87.5,
        threat_class="possible_fault_or_short_transient",
        rationale="2 detections across 2 lanes on Feed pump 2.",
        score_components={"base": 60.0, "corroboration": 20.0},
        detection_ids=[d.detection_id for d in detections],
        recommended_action="Inspect pump-02.",
        status=AlertStatus.ACKNOWLEDGED,
    )
    repository.insert_alerts(session, [alert])
    (restored,) = repository.get_alerts(session)
    # Lineage is set-like: the junction table does not preserve list order.
    assert set(restored.detection_ids) == set(alert.detection_ids)
    assert restored.model_dump(exclude={"detection_ids"}) == alert.model_dump(
        exclude={"detection_ids"}
    )
