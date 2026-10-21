"""An organization's data sources: where its data comes from, typed IT or OT (ADR-0041).

The register iotShield kept (``data_sources``: tenant, IT/OT, name, description), carried
over so the two kinds of source — IT (network traffic, logs) and OT (meters, sensors) — are
named once per organization instead of in every operator's head. Like ``accounts``, callers
get frozen records, never ORM rows, and the functions flush but never commit.

Every function takes ``org_id`` as a required keyword, the repository's rule (ADR-0039): an
id belonging to another organization is indistinguishable from one that does not exist.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, get_args
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iotshield.storage.orm import DataSourceRow

SourceType = Literal["IT", "OT"]
"""IT: information technology — network traffic, logs. OT: operational technology — meters,
sensors, the physical process."""
SOURCE_TYPES: tuple[SourceType, ...] = get_args(SourceType)


class DataSourceConflict(ValueError):
    """The organization already has a data source of that name — the API maps this to 409."""


@dataclass(frozen=True)
class DataSource:
    source_id: UUID
    name: str
    type: SourceType
    description: str | None
    normalizer: str | None
    source_format: str | None
    connection: str | None
    created_at: datetime


@dataclass(frozen=True)
class DataSourceFields:
    """What a caller sets on a data source; the id, tenant and creation time are not theirs."""

    name: str
    type: SourceType
    description: str | None = None
    normalizer: str | None = None
    source_format: str | None = None
    connection: str | None = None


def _record(row: DataSourceRow) -> DataSource:
    if row.type not in SOURCE_TYPES:  # pragma: no cover - the check constraint forbids it
        raise ValueError(f"stored data source has unknown type {row.type!r}")
    return DataSource(
        source_id=row.source_id,
        name=row.name,
        type=row.type,
        description=row.description,
        normalizer=row.normalizer,
        source_format=row.source_format,
        connection=row.connection,
        created_at=row.created_at,
    )


def _name_taken(session: Session, name: str, *, org_id: str, other_than: UUID | None) -> bool:
    stmt = select(DataSourceRow.source_id).where(
        DataSourceRow.org_id == org_id, DataSourceRow.name == name
    )
    if other_than is not None:
        stmt = stmt.where(DataSourceRow.source_id != other_than)
    return session.scalar(stmt) is not None


def _conflict(name: str) -> DataSourceConflict:
    return DataSourceConflict(f"a data source named {name!r} already exists")


def list_data_sources(session: Session, *, org_id: str) -> list[DataSource]:
    """This organization's data sources, IT before OT, then by name."""
    rows = session.scalars(
        select(DataSourceRow)
        .where(DataSourceRow.org_id == org_id)
        .order_by(DataSourceRow.type, DataSourceRow.name)
    )
    return [_record(row) for row in rows]


def get_data_source(session: Session, source_id: UUID, *, org_id: str) -> DataSource | None:
    row = session.get(DataSourceRow, source_id)
    return _record(row) if row is not None and row.org_id == org_id else None


def create_data_source(session: Session, fields: DataSourceFields, *, org_id: str) -> DataSource:
    if _name_taken(session, fields.name, org_id=org_id, other_than=None):
        raise _conflict(fields.name)
    row = DataSourceRow(
        source_id=uuid4(),
        org_id=org_id,
        name=fields.name,
        type=fields.type,
        description=fields.description,
        normalizer=fields.normalizer,
        source_format=fields.source_format,
        connection=fields.connection,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:  # a concurrent create of the same name
        raise _conflict(fields.name) from exc
    return _record(row)


def update_data_source(
    session: Session, source_id: UUID, fields: DataSourceFields, *, org_id: str
) -> DataSource | None:
    """Replace a data source's fields; None when this organization has no such source."""
    row = session.get(DataSourceRow, source_id)
    if row is None or row.org_id != org_id:
        return None
    if _name_taken(session, fields.name, org_id=org_id, other_than=source_id):
        raise _conflict(fields.name)
    row.name = fields.name
    row.type = fields.type
    row.description = fields.description
    row.normalizer = fields.normalizer
    row.source_format = fields.source_format
    row.connection = fields.connection
    session.flush()
    return _record(row)


def delete_data_source(session: Session, source_id: UUID, *, org_id: str) -> bool:
    row = session.get(DataSourceRow, source_id)
    if row is None or row.org_id != org_id:
        return False
    session.delete(row)
    session.flush()
    return True
