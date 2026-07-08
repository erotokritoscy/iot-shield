"""Typed persistence functions — the only place the rest of the codebase touches SQL.

Callers pass an explicit Session and speak contract objects exclusively; ORM rows
never escape this module. Repository functions flush (making writes queryable
in-session) but never commit — transaction boundaries belong to the caller.

Time ranges are half-open: ``start <= t < end``.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from iotshield.contracts import (
    AlertStatus,
    Asset,
    Criticality,
    DetectionResult,
    Lane,
    MalformedRecord,
    NormalizedEvent,
    PrioritizedAlert,
)
from iotshield.storage.orm import (
    AlertDetectionRow,
    AlertRow,
    AssetRow,
    DetectionRow,
    EventRow,
    MalformedRow,
)


def _ensure_assets(session: Session, asset_ids: set[str]) -> None:
    """Asset discovery (ADR-0005): stub inventory rows for ids seen in data.

    Only ever fills absence — a stub is inserted when no row exists; existing
    rows (registered or previously discovered) are never touched, so
    registration always beats discovery.

    Every write path calls this unconditionally: detections/alerts usually
    follow an event insert that already discovered their assets, but they can
    also arrive alone (replay, backfill, external detector), so the calls in
    insert_detections/insert_alerts are load-bearing, not redundant.
    """
    if not asset_ids:
        return
    existing = set(
        session.scalars(select(AssetRow.asset_id).where(AssetRow.asset_id.in_(asset_ids)))
    )
    for asset_id in sorted(asset_ids - existing):
        stub = Asset(
            asset_id=asset_id,
            name=asset_id,
            asset_type="unknown",
            criticality=Criticality.MEDIUM,  # neutral policy default, stated explicitly
            metadata={"auto_discovered": True},
        )
        session.add(AssetRow.from_contract(stub))
    # No flush here: callers flush, and the unit of work orders stub inserts
    # before their FK-dependent rows within that single flush.


def upsert_assets(session: Session, assets: Sequence[Asset]) -> None:
    """Registration: insert or update — human knowledge overwrites discovery."""
    for asset in assets:
        session.merge(AssetRow.from_contract(asset))
    session.flush()


def insert_events(session: Session, events: Sequence[NormalizedEvent]) -> None:
    _ensure_assets(session, {event.asset_id for event in events})
    session.add_all(EventRow.from_contract(event) for event in events)
    session.flush()


def insert_malformed(session: Session, records: Sequence[MalformedRecord]) -> None:
    session.add_all(MalformedRow.from_contract(record) for record in records)
    session.flush()


def insert_detections(session: Session, detections: Sequence[DetectionResult]) -> None:
    _ensure_assets(session, {detection.asset_id for detection in detections})
    session.add_all(DetectionRow.from_contract(detection) for detection in detections)
    session.flush()


def insert_alerts(session: Session, alerts: Sequence[PrioritizedAlert]) -> None:
    _ensure_assets(session, {alert.asset_id for alert in alerts})
    session.add_all(AlertRow.from_contract(alert) for alert in alerts)
    session.flush()


def list_assets(session: Session) -> list[Asset]:
    rows = session.scalars(select(AssetRow).order_by(AssetRow.asset_id))
    return [row.to_contract() for row in rows]


def get_events(
    session: Session,
    *,
    asset_id: str | None = None,
    lane: Lane | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[NormalizedEvent]:
    stmt = select(EventRow).order_by(EventRow.timestamp, EventRow.event_id).limit(limit)
    if asset_id is not None:
        stmt = stmt.where(EventRow.asset_id == asset_id)
    if lane is not None:
        stmt = stmt.where(EventRow.source_type == lane.value)
    if start is not None:
        stmt = stmt.where(EventRow.timestamp >= start)
    if end is not None:
        stmt = stmt.where(EventRow.timestamp < end)
    return [row.to_contract() for row in session.scalars(stmt)]


def list_malformed(session: Session, *, limit: int = 1000) -> list[MalformedRecord]:
    stmt = select(MalformedRow).order_by(MalformedRow.created_at, MalformedRow.record_id)
    return [row.to_contract() for row in session.scalars(stmt.limit(limit))]


def get_detections(
    session: Session,
    *,
    asset_id: str | None = None,
    lane: Lane | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
) -> list[DetectionResult]:
    stmt = (
        select(DetectionRow)
        .order_by(DetectionRow.window_start, DetectionRow.detection_id)
        .limit(limit)
    )
    if asset_id is not None:
        stmt = stmt.where(DetectionRow.asset_id == asset_id)
    if lane is not None:
        stmt = stmt.where(DetectionRow.lane == lane.value)
    if start is not None:
        stmt = stmt.where(DetectionRow.window_start >= start)
    if end is not None:
        stmt = stmt.where(DetectionRow.window_start < end)
    return [row.to_contract() for row in session.scalars(stmt)]


def get_alerts(
    session: Session,
    *,
    status: AlertStatus | None = None,
    detection_id: UUID | None = None,
    limit: int = 1000,
) -> list[PrioritizedAlert]:
    """Alerts in triage order: priority descending (the ranked queue IS the product)."""
    stmt = (
        select(AlertRow)
        # Eager-load lineage in one batched query: to_contract() reads
        # detection_links, which would otherwise lazy-load per alert (N+1).
        .options(selectinload(AlertRow.detection_links))
        .order_by(AlertRow.priority_score.desc(), AlertRow.created_at, AlertRow.alert_id)
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(AlertRow.status == status.value)
    if detection_id is not None:
        # The junction's reverse direction: which alert covers this detection?
        stmt = stmt.join(AlertDetectionRow).where(AlertDetectionRow.detection_id == detection_id)
    return [row.to_contract() for row in session.scalars(stmt)]


def get_alert_by_id(session: Session, alert_id: UUID) -> PrioritizedAlert | None:
    stmt = (
        select(AlertRow)
        .options(selectinload(AlertRow.detection_links))
        .where(AlertRow.alert_id == alert_id)
    )
    row = session.scalars(stmt).one_or_none()
    return row.to_contract() if row is not None else None


def get_detections_by_ids(session: Session, detection_ids: Sequence[UUID]) -> list[DetectionResult]:
    """Resolve an alert's lineage list into its detections (drill-down)."""
    if not detection_ids:
        return []
    stmt = (
        select(DetectionRow)
        .where(DetectionRow.detection_id.in_(detection_ids))
        .order_by(DetectionRow.window_start, DetectionRow.detection_id)
    )
    return [row.to_contract() for row in session.scalars(stmt)]


def count_summary(session: Session) -> dict[str, int]:
    """Per-table row counts (the /summary endpoint's KPI numbers).

    One round-trip via scalar subqueries: /summary is the dashboard's polled
    endpoint, so this is the one place recurring round-trips would add up.
    """
    tables = {
        "assets": AssetRow,
        "events": EventRow,
        "malformed_records": MalformedRow,
        "detections": DetectionRow,
        "alerts": AlertRow,
    }
    stmt = select(
        *(
            select(func.count()).select_from(row_type).scalar_subquery().label(name)
            for name, row_type in tables.items()
        )
    )
    counts = session.execute(stmt).one()
    return {name: int(count) for name, count in zip(tables, counts, strict=True)}
