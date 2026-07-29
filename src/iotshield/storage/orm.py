"""ORM tables: a lossless projection of the contracts (round-trip tested).

Contracts stay persistence-ignorant; each table carries ``from_contract`` /
``to_contract`` converters so projection logic lives beside the table it serves
(ADR-0005). Enums are stored as their wire-format strings — the contracts, not
the database, police vocabulary. ``metadata`` columns are mapped to ``metadata_``
attributes because SQLAlchemy reserves the ``metadata`` name.
"""

from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import DateTime, Dialect, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator

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


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    Postgres has timestamptz; SQLite stores naive and SQLAlchemy would hand back
    naive datetimes that fail our own contracts on rehydration. This type
    requires aware on the way in and re-attaches UTC on the way out, so stored
    rows always round-trip into valid contract objects (golden rule 4).
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    # Deliberately mirrors contracts.models._ensure_aware_utc: two independent
    # boundaries enforcing the same invariant (this one also catches hand-built
    # rows that never went through a contract). Keep the predicates in sync.
    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("storage requires timezone-aware datetimes")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UtcDateTime(),
        # Plain JSON on SQLite (tests), binary/indexable JSONB on Postgres.
        dict[str, Any]: JSON().with_variant(JSONB(), "postgresql"),
    }


class AssetRow(Base):
    __tablename__ = "assets"

    asset_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    asset_type: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    criticality: Mapped[str] = mapped_column(String(20))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata")

    @classmethod
    def from_contract(cls, asset: Asset) -> "AssetRow":
        return cls(
            asset_id=asset.asset_id,
            name=asset.name,
            asset_type=asset.asset_type,
            location=asset.location,
            criticality=asset.criticality.value,
            metadata_=asset.metadata,
        )

    @staticmethod
    def values_of(asset: Asset) -> dict[str, Any]:
        """Column values for a Core insert, keyed by ATTRIBUTE name.

        Needed because discovery inserts through Core (ON CONFLICT DO NOTHING) rather than
        the ORM unit of work. Written beside ``from_contract`` so the two cannot disagree
        about the column set, and it keeps ``metadata_``'s rename in one place — ``metadata``
        is taken on a declarative class, and Core wants the attribute key, not the column.
        """
        return {
            "asset_id": asset.asset_id,
            "name": asset.name,
            "asset_type": asset.asset_type,
            "location": asset.location,
            "criticality": asset.criticality.value,
            "metadata_": asset.metadata,
        }

    def to_contract(self) -> Asset:
        return Asset(
            asset_id=self.asset_id,
            name=self.name,
            asset_type=self.asset_type,
            location=self.location,
            criticality=Criticality(self.criticality),
            metadata=self.metadata_,
        )


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_asset_ts", "asset_id", "timestamp"),  # per-asset timeline
        Index("ix_events_lane_ts", "source_type", "timestamp"),  # lane feeds
    )

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime]
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"))
    source_type: Mapped[str] = mapped_column(String(20))
    source_format: Mapped[str] = mapped_column(Text)
    metric: Mapped[str | None] = mapped_column(Text)
    value: Mapped[float | None]
    severity: Mapped[str] = mapped_column(String(20))
    message: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata")

    @classmethod
    def from_contract(cls, event: NormalizedEvent) -> "EventRow":
        return cls(
            event_id=event.event_id,
            timestamp=event.timestamp,
            asset_id=event.asset_id,
            source_type=event.source_type.value,
            source_format=event.source_format,
            metric=event.metric,
            value=event.value,
            severity=event.severity.value,
            message=event.message,
            raw=event.raw,
            metadata_=event.metadata,
        )

    def to_contract(self) -> NormalizedEvent:
        return NormalizedEvent(
            event_id=self.event_id,
            timestamp=self.timestamp,
            asset_id=self.asset_id,
            source_type=Lane(self.source_type),
            source_format=self.source_format,
            metric=self.metric,
            value=self.value,
            severity=Severity(self.severity),
            message=self.message,
            raw=self.raw,
            metadata=self.metadata_,
        )


class MalformedRow(Base):
    __tablename__ = "malformed_records"

    record_id: Mapped[UUID] = mapped_column(primary_key=True)
    raw: Mapped[str] = mapped_column(Text)
    error: Mapped[str] = mapped_column(Text)
    source_format: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]

    @classmethod
    def from_contract(cls, record: MalformedRecord) -> "MalformedRow":
        return cls(
            record_id=record.record_id,
            raw=record.raw,
            error=record.error,
            source_format=record.source_format,
            origin=record.origin,
            created_at=record.created_at,
        )

    def to_contract(self) -> MalformedRecord:
        return MalformedRecord(
            record_id=self.record_id,
            raw=self.raw,
            error=self.error,
            source_format=self.source_format,
            origin=self.origin,
            created_at=self.created_at,
        )


class DetectionRow(Base):
    __tablename__ = "detections"
    __table_args__ = (
        Index("ix_detections_asset_ws", "asset_id", "window_start"),  # correlation + drill-down
    )

    detection_id: Mapped[UUID] = mapped_column(primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"))
    window_start: Mapped[datetime]
    window_end: Mapped[datetime]
    lane: Mapped[str] = mapped_column(String(20))
    method: Mapped[str] = mapped_column(Text)
    score: Mapped[float]
    threshold: Mapped[float]
    event_kind: Mapped[str | None] = mapped_column(String(20))
    evidence: Mapped[dict[str, Any]]
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata")

    @classmethod
    def from_contract(cls, detection: DetectionResult) -> "DetectionRow":
        return cls(
            detection_id=detection.detection_id,
            asset_id=detection.asset_id,
            window_start=detection.window_start,
            window_end=detection.window_end,
            lane=detection.lane.value,
            method=detection.method,
            score=detection.score,
            threshold=detection.threshold,
            event_kind=detection.event_kind.value if detection.event_kind else None,
            evidence=detection.evidence,
            metadata_=detection.metadata,
        )

    def to_contract(self) -> DetectionResult:
        return DetectionResult(
            detection_id=self.detection_id,
            asset_id=self.asset_id,
            window_start=self.window_start,
            window_end=self.window_end,
            lane=Lane(self.lane),
            method=self.method,
            score=self.score,
            threshold=self.threshold,
            event_kind=EventKind(self.event_kind) if self.event_kind else None,
            evidence=self.evidence,
            metadata=self.metadata_,
        )


class AlertDetectionRow(Base):
    """Junction: an alert's lineage list, queryable in both directions."""

    __tablename__ = "alert_detections"

    alert_id: Mapped[UUID] = mapped_column(ForeignKey("alerts.alert_id"), primary_key=True)
    detection_id: Mapped[UUID] = mapped_column(
        ForeignKey("detections.detection_id"), primary_key=True
    )


class AlertRow(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_status_priority", "status", "priority_score"),  # triage queue
        Index("ix_alerts_created_at", "created_at"),
    )

    alert_id: Mapped[UUID] = mapped_column(primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"))
    window_start: Mapped[datetime]
    window_end: Mapped[datetime]
    severity: Mapped[str] = mapped_column(String(20))
    priority_score: Mapped[float]
    threat_class: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    score_components: Mapped[dict[str, Any]]
    recommended_action: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime]

    detection_links: Mapped[list[AlertDetectionRow]] = relationship(
        cascade="all, delete-orphan",
        order_by=AlertDetectionRow.detection_id,  # deterministic retrieval order
    )

    @classmethod
    def from_contract(cls, alert: PrioritizedAlert) -> "AlertRow":
        return cls(
            alert_id=alert.alert_id,
            asset_id=alert.asset_id,
            window_start=alert.window_start,
            window_end=alert.window_end,
            severity=alert.severity.value,
            priority_score=alert.priority_score,
            threat_class=alert.threat_class,
            rationale=alert.rationale,
            score_components=alert.score_components,
            recommended_action=alert.recommended_action,
            status=alert.status.value,
            created_at=alert.created_at,
            detection_links=[
                AlertDetectionRow(detection_id=detection_id) for detection_id in alert.detection_ids
            ],
        )

    def to_contract(self) -> PrioritizedAlert:
        return PrioritizedAlert(
            alert_id=self.alert_id,
            asset_id=self.asset_id,
            window_start=self.window_start,
            window_end=self.window_end,
            severity=AlertSeverity(self.severity),
            priority_score=self.priority_score,
            threat_class=self.threat_class,
            rationale=self.rationale,
            score_components=self.score_components,
            detection_ids=[link.detection_id for link in self.detection_links],
            recommended_action=self.recommended_action,
            status=AlertStatus(self.status),
            created_at=self.created_at,
        )
