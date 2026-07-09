"""Repository query semantics: filters, ordering, half-open ranges, lineage
integrity, and the storage boundary's timestamp guard."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iotshield.contracts import (
    AlertSeverity,
    AlertStatus,
    DetectionResult,
    Lane,
    NormalizedEvent,
    PrioritizedAlert,
)
from iotshield.storage import repository
from iotshield.storage.orm import AssetRow, DetectionRow, EventRow, MalformedRow

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def event(
    *, asset_id: str = "pump-02", lane: Lane = Lane.TELEMETRY, offset_s: int = 0
) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id=asset_id,
        source_type=lane,
        source_format="stub-csv",
        raw=f"r{offset_s}",
    )


def detection(*, asset_id: str = "pump-02", offset_s: int = 0) -> DetectionResult:
    start = T0 + timedelta(seconds=offset_s)
    return DetectionResult(
        asset_id=asset_id,
        window_start=start,
        window_end=start,
        lane=Lane.TELEMETRY,
        method="m",
        score=96.0,
        threshold=80.0,
    )


def alert(
    detections: list[DetectionResult],
    *,
    priority: float,
    status: AlertStatus = AlertStatus.NEW,
) -> PrioritizedAlert:
    return PrioritizedAlert(
        asset_id=detections[0].asset_id,
        window_start=min(d.window_start for d in detections),
        window_end=max(d.window_end for d in detections),
        severity=AlertSeverity.HIGH,
        priority_score=priority,
        rationale="test alert",
        detection_ids=[d.detection_id for d in detections],
        status=status,
    )


class TestEventQueries:
    def test_filters_by_asset_and_lane(self, session: Session) -> None:
        repository.insert_events(
            session,
            [
                event(asset_id="pump-02", lane=Lane.TELEMETRY),
                event(asset_id="pump-02", lane=Lane.LOG, offset_s=1),
                event(asset_id="gw-01", lane=Lane.TELEMETRY, offset_s=2),
            ],
        )
        pump_telemetry = repository.get_events(session, asset_id="pump-02", lane=Lane.TELEMETRY)
        assert [e.asset_id for e in pump_telemetry] == ["pump-02"]
        assert pump_telemetry[0].source_type == Lane.TELEMETRY

    def test_time_range_is_half_open(self, session: Session) -> None:
        repository.insert_events(session, [event(offset_s=s) for s in (0, 10, 20)])
        window = repository.get_events(
            session, start=T0, end=T0 + timedelta(seconds=20)
        )  # [start, end)
        assert [e.timestamp for e in window] == [T0, T0 + timedelta(seconds=10)]

    def test_limit_returns_newest_rows_oldest_first(self, session: Session) -> None:
        repository.insert_events(session, [event(offset_s=s) for s in (20, 0, 10)])
        newest_two = repository.get_events(session, limit=2)
        # The LIMIT keeps the NEWEST rows (offsets 10, 20), not the oldest, and
        # returns them oldest-first for the timeline.
        assert [e.timestamp for e in newest_two] == [
            T0 + timedelta(seconds=10),
            T0 + timedelta(seconds=20),
        ]


class TestDetectionQueries:
    def test_filters_and_ordering(self, session: Session) -> None:
        repository.insert_detections(
            session,
            [detection(offset_s=10), detection(offset_s=0), detection(asset_id="gw-01")],
        )
        pump = repository.get_detections(session, asset_id="pump-02")
        assert [d.window_start for d in pump] == [T0, T0 + timedelta(seconds=10)]

    def test_window_start_range_is_half_open(self, session: Session) -> None:
        repository.insert_detections(session, [detection(offset_s=s) for s in (0, 10, 20)])
        window = repository.get_detections(
            session, start=T0, end=T0 + timedelta(seconds=20)
        )  # [start, end)
        assert [d.window_start for d in window] == [T0, T0 + timedelta(seconds=10)]


class TestAlertQueries:
    def test_ranked_by_priority_and_status_filter(self, session: Session) -> None:
        d1, d2, d3 = detection(offset_s=0), detection(offset_s=1), detection(offset_s=2)
        repository.insert_detections(session, [d1, d2, d3])
        repository.insert_alerts(
            session,
            [
                alert([d1], priority=40.0),
                alert([d2], priority=90.0),
                alert([d3], priority=70.0, status=AlertStatus.CLOSED),
            ],
        )
        ranked = repository.get_alerts(session)
        assert [a.priority_score for a in ranked] == [90.0, 70.0, 40.0]
        new_only = repository.get_alerts(session, status=AlertStatus.NEW)
        assert [a.priority_score for a in new_only] == [90.0, 40.0]

    def test_junction_answers_both_directions(self, session: Session) -> None:
        d1, d2 = detection(offset_s=0), detection(offset_s=1)
        repository.insert_detections(session, [d1, d2])
        stored_alert = alert([d1, d2], priority=80.0)
        repository.insert_alerts(session, [stored_alert])

        # Forward: alert -> its detections.
        (restored,) = repository.get_alerts(session)
        assert set(restored.detection_ids) == {d1.detection_id, d2.detection_id}
        # Reverse: detection -> the alert that covers it.
        (found,) = repository.get_alerts(session, detection_id=d2.detection_id)
        assert found.alert_id == stored_alert.alert_id

    def test_dangling_lineage_rejected_by_foreign_key(self, session: Session) -> None:
        d = detection()
        repository.insert_detections(session, [d])
        ghost = alert([d], priority=50.0).model_copy(update={"detection_ids": [uuid4()]})
        with pytest.raises(IntegrityError):
            repository.insert_alerts(session, [ghost])


def test_free_form_columns_have_no_length_cap() -> None:
    # Contracts don't bound these strings; storage must not either, or a row that
    # passes on SQLite (which ignores VARCHAR limits) truncates on Postgres and
    # rolls back the whole atomic run.
    for column in (
        AssetRow.__table__.c.name,
        EventRow.__table__.c.metric,
        EventRow.__table__.c.source_format,
        MalformedRow.__table__.c.origin,
        DetectionRow.__table__.c.method,
    ):
        assert getattr(column.type, "length", None) is None


def test_storage_boundary_rejects_naive_datetimes(session: Session) -> None:
    # Contracts already forbid naive timestamps; this pins the *storage* guard
    # (UtcDateTime) for writes that bypass contracts, e.g. hand-built rows.
    session.add(
        EventRow(
            event_id=uuid4(),
            timestamp=datetime(2026, 7, 7, 12, 0, 0),  # naive on purpose
            asset_id="pump-02",
            source_type="telemetry",
            source_format="stub-csv",
            severity="info",
            raw="r",
            metadata_={},
        )
    )
    with pytest.raises(Exception, match="timezone-aware"):
        session.flush()
