"""ORM tables: a lossless projection of the contracts (round-trip tested).

Contracts stay persistence-ignorant; each table carries ``from_contract`` /
``to_contract`` converters so projection logic lives beside the table it serves
(ADR-0005). Enums are stored as their wire-format strings — the contracts, not
the database, police vocabulary. ``metadata`` columns are mapped to ``metadata_``
attributes because SQLAlchemy reserves the ``metadata`` name.

Every data row also carries ``org_id``, the organization it belongs to (ADR-0039). It is a
*storage* fact, not a contract field: the pipeline computes a run without knowing whose it
is, and the store stamps the tenant on the way in. So ``from_contract`` takes it as an
argument and ``to_contract`` drops it — the projection stays lossless for the contract,
and no adapter can see, forge or forget a tenant. An asset is keyed by ``(org_id,
asset_id)`` because two organizations may each own a device called ``10.0.0.24``.

``run_id`` (ADR-0045) is a storage fact of the same kind: the run that wrote the row, so a
run's results can be read back as a set. It is nullable because rows can arrive outside a
run (a replay, a backfill, a test writing through the repository directly), and an honest
"no run" beats a made-up one. Assets carry none — a device outlives the runs that saw it.

The account tables (organizations, users, sessions) are not projections of any contract:
they are the access-control layer the pipeline never sees. Neither is ``data_sources``: an
organization's register of where its data comes from (ADR-0041), which the pipeline never
reads either.
"""

from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Dialect,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
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


class OrganizationRow(Base):
    """A tenant: every data row and every user belongs to exactly one (ADR-0039)."""

    __tablename__ = "organizations"

    org_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # URL-safe slug
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    # Suspended by the super admin (ADR-0040): its users cannot log in, its data is kept.
    suspended: Mapped[bool] = mapped_column(default=False)


class UserRow(Base):
    """A login. ``username`` is unique across organizations, so login needs no org field.

    A **super admin** (ADR-0040) manages every organization. It *may* have a home
    organization, whose data it then sees like that organization's admin (the ADR's second
    amendment); with none it sees no organization's data. Every other user must belong to
    one — the check constraint makes that an invariant of the table, not of the code.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role = 'superadmin' OR org_id IS NOT NULL",
            name="ck_users_org_required_unless_superadmin",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)  # stored lowercased
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))  # "admin" | "member" | "superadmin"
    created_at: Mapped[datetime]


class SessionRow(Base):
    """A login session. Only the token's SHA-256 is stored, so a DB leak yields no sessions."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]


class DataSourceRow(Base):
    """One place an organization's data comes from, typed IT or OT (ADR-0041).

    A register, not a pipeline input: a run still names its normalizer and format itself.
    ``normalizer``/``source_format`` record how this source's data is read, so a form can
    fill them in; both may be blank for a source whose reader is not decided yet.
    """

    __tablename__ = "data_sources"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_data_sources_org_name"),
        CheckConstraint("type IN ('IT', 'OT')", name="ck_data_sources_type"),
    )

    source_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(2))  # "IT" | "OT"
    description: Mapped[str | None] = mapped_column(Text)
    normalizer: Mapped[str | None] = mapped_column(String(64))
    source_format: Mapped[str | None] = mapped_column(String(64))
    # A connection declared in config/connections.yaml, by name (ADR-0042); never a host.
    connection: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime]


class RunRow(Base):
    """One saved pipeline run: its name, who ran it, the chain it ran, and what it found (ADR-0046).

    Written by the Postgres sink in the same transaction as the run's rows, so a run that is
    listed always has its results and a run whose write failed is not listed. The data rows
    point back by ``run_id`` with **no foreign key**: the column predates this table
    (ADR-0045), and ``create_all`` adds a missing table but never alters an existing one, so
    a constraint here would exist on a fresh database and not on an upgraded one. Deleting a
    run deletes its rows explicitly instead (``runs.delete_run``).

    ``spec`` is the composed chain exactly as it ran — the ``PipelineSpec`` fields only, so
    never the deployment's ``database_url``. The raw inputs are *not* kept: every line of
    them is already stored, on an event's or a malformed record's ``raw``.
    """

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_org_ran_at", "org_id", "run_at"),)

    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"))
    name: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))  # a username; None from the CLI
    run_at: Mapped[datetime]
    spec: Mapped[dict[str, Any]]
    sources: Mapped[list[Any]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    warnings: Mapped[list[Any]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    events: Mapped[int]
    malformed: Mapped[int]
    detections: Mapped[int]
    alerts: Mapped[int]


def _asset_fk() -> ForeignKeyConstraint:
    # Composite: a row may only name an asset of its OWN organization. A plain FK on
    # asset_id alone would let org A's detection point at org B's device of the same id.
    return ForeignKeyConstraint(["org_id", "asset_id"], ["assets.org_id", "assets.asset_id"])


class AssetRow(Base):
    __tablename__ = "assets"

    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    asset_type: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    criticality: Mapped[str] = mapped_column(String(20))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata")

    @classmethod
    def from_contract(cls, asset: Asset, *, org_id: str) -> "AssetRow":
        return cls(
            org_id=org_id,
            asset_id=asset.asset_id,
            name=asset.name,
            asset_type=asset.asset_type,
            location=asset.location,
            criticality=asset.criticality.value,
            metadata_=asset.metadata,
        )

    @staticmethod
    def values_of(asset: Asset, *, org_id: str) -> dict[str, Any]:
        """Column values for a Core insert, keyed by ATTRIBUTE name.

        Needed because discovery inserts through Core (ON CONFLICT DO NOTHING) rather than
        the ORM unit of work. Written beside ``from_contract`` so the two cannot disagree
        about the column set, and it keeps ``metadata_``'s rename in one place — ``metadata``
        is taken on a declarative class, and Core wants the attribute key, not the column.
        """
        return {
            "org_id": org_id,
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
        Index("ix_events_asset_ts", "org_id", "asset_id", "timestamp"),  # per-asset timeline
        Index("ix_events_lane_ts", "org_id", "source_type", "timestamp"),  # lane feeds
        _asset_fk(),
    )

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[UUID | None] = mapped_column(index=True)
    timestamp: Mapped[datetime]
    asset_id: Mapped[str] = mapped_column(String)
    source_type: Mapped[str] = mapped_column(String(20))
    source_format: Mapped[str] = mapped_column(Text)
    metric: Mapped[str | None] = mapped_column(Text)
    value: Mapped[float | None]
    severity: Mapped[str] = mapped_column(String(20))
    message: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata")

    @classmethod
    def from_contract(
        cls, event: NormalizedEvent, *, org_id: str, run_id: UUID | None = None
    ) -> "EventRow":
        return cls(
            event_id=event.event_id,
            org_id=org_id,
            run_id=run_id,
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
    __table_args__ = (Index("ix_malformed_org_created", "org_id", "created_at"),)

    record_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.org_id"))
    run_id: Mapped[UUID | None] = mapped_column(index=True)
    raw: Mapped[str] = mapped_column(Text)
    error: Mapped[str] = mapped_column(Text)
    source_format: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]

    @classmethod
    def from_contract(
        cls, record: MalformedRecord, *, org_id: str, run_id: UUID | None = None
    ) -> "MalformedRow":
        return cls(
            record_id=record.record_id,
            org_id=org_id,
            run_id=run_id,
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
        # correlation + drill-down
        Index("ix_detections_asset_ws", "org_id", "asset_id", "window_start"),
        _asset_fk(),
    )

    detection_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[UUID | None] = mapped_column(index=True)
    asset_id: Mapped[str] = mapped_column(String)
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
    def from_contract(
        cls, detection: DetectionResult, *, org_id: str, run_id: UUID | None = None
    ) -> "DetectionRow":
        return cls(
            detection_id=detection.detection_id,
            org_id=org_id,
            run_id=run_id,
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
        Index("ix_alerts_status_priority", "org_id", "status", "priority_score"),  # triage
        Index("ix_alerts_created_at", "org_id", "created_at"),
        _asset_fk(),
    )

    alert_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[UUID | None] = mapped_column(index=True)
    asset_id: Mapped[str] = mapped_column(String)
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
    def from_contract(
        cls, alert: PrioritizedAlert, *, org_id: str, run_id: UUID | None = None
    ) -> "AlertRow":
        return cls(
            alert_id=alert.alert_id,
            org_id=org_id,
            run_id=run_id,
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
