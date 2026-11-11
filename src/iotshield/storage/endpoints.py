"""The HTTP API behind a data source, as stored (ADR-0049): settings JSON + sealed credentials.

The repository keeps it opaque on purpose — ``config`` is a JSON object and ``credentials`` a
Fernet token, neither interpreted here — so storage does not depend on the connector's model
and a change to the endpoint's fields is not a schema change. ``api/data_sources.py`` parses
the one and seals or unseals the other.

Same rules as every repository here: ``org_id`` is a required keyword, and a source belonging
to another organization reads exactly like one that has no endpoint.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from iotshield.storage.orm import DataSourceEndpointRow


@dataclass(frozen=True)
class StoredEndpoint:
    source_id: UUID
    config: dict[str, Any]
    credentials: str | None
    """The sealed token, or None when the endpoint needs none."""


def _record(row: DataSourceEndpointRow) -> StoredEndpoint:
    return StoredEndpoint(
        source_id=row.source_id, config=json.loads(row.config), credentials=row.credentials
    )


def get_endpoint(session: Session, source_id: UUID, *, org_id: str) -> StoredEndpoint | None:
    row = session.get(DataSourceEndpointRow, source_id)
    return _record(row) if row is not None and row.org_id == org_id else None


def endpoints_of(session: Session, *, org_id: str) -> dict[UUID, StoredEndpoint]:
    rows = session.scalars(
        select(DataSourceEndpointRow).where(DataSourceEndpointRow.org_id == org_id)
    )
    return {row.source_id: _record(row) for row in rows}


def set_endpoint(
    session: Session,
    source_id: UUID,
    config: dict[str, Any],
    credentials: str | None,
    *,
    org_id: str,
) -> StoredEndpoint:
    """Create or replace the endpoint of a source this organization owns (the caller checked)."""
    row = session.get(DataSourceEndpointRow, source_id)
    if row is not None and row.org_id != org_id:  # pragma: no cover - ids are uuid4, per source
        raise ValueError("data source belongs to another organization")
    if row is None:
        row = DataSourceEndpointRow(source_id=source_id, org_id=org_id)
        session.add(row)
    row.config = json.dumps(config, sort_keys=True)
    row.credentials = credentials
    row.updated_at = datetime.now(UTC)
    session.flush()
    return _record(row)


def delete_endpoint(session: Session, source_id: UUID, *, org_id: str) -> bool:
    row = session.get(DataSourceEndpointRow, source_id)
    if row is None or row.org_id != org_id:
        return False
    session.delete(row)
    session.flush()
    return True
