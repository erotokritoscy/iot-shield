"""Saved runs: every pipeline run, named, listable and reopenable (ADR-0046).

A run was addressable (ADR-0045) — its rows carry a ``run_id`` — but not *listable*: the only
way back to one was the result tiles of the page that ran it. This module is the run's own
record. Like ``data_sources``, callers get frozen records, never ORM rows, the functions flush
but never commit, and every function takes ``org_id`` as a required keyword: another
organization's run is indistinguishable from one that never existed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from iotshield.storage.orm import (
    AlertDetectionRow,
    AlertRow,
    DetectionRow,
    EventRow,
    MalformedRow,
    RunRow,
)
from iotshield.storage.repository import Page

MAX_NAME_LENGTH = 200


@dataclass(frozen=True)
class RunSource:
    normalizer: str
    source_format: str
    origin: str
    # The data-source pull this input was read from, when it was one (ADR-0047):
    # ``{source_id, start, end, params}``. A pull is a reference rather than data, so unlike an
    # upload it can be saved, and Open in Compose restores it rather than asking again.
    pull: dict[str, Any] | None = None


@dataclass(frozen=True)
class SavedRun:
    run_id: UUID
    name: str
    created_by: str | None
    run_at: datetime
    spec: dict[str, Any]
    sources: list[RunSource]
    warnings: list[str]
    events: int
    malformed: int
    detections: int
    alerts: int


def default_run_name(origins: Sequence[str], run_at: datetime) -> str:
    """What a run is called when nobody named it: its inputs, then when it ran (UTC).

    The inputs lead because they are what tells two runs apart in a list — two runs a minute
    apart on different files differ in their first words, not their last.
    """
    inputs = ", ".join(origin for origin in origins if origin) or "run"
    if len(inputs) > 120:
        inputs = inputs[:119] + "…"
    return f"{inputs} — {run_at:%Y-%m-%d %H:%M} UTC"


def clean_name(name: str | None) -> str | None:
    """A caller's name, trimmed; blank means "not named", so the default applies."""
    if name is None:
        return None
    stripped = " ".join(name.split())
    return stripped[:MAX_NAME_LENGTH] or None


def _record(row: RunRow) -> SavedRun:
    return SavedRun(
        run_id=row.run_id,
        name=row.name,
        created_by=row.created_by,
        run_at=row.run_at,
        spec=row.spec,
        sources=[RunSource(**source) for source in row.sources],
        warnings=[str(warning) for warning in row.warnings],
        events=row.events,
        malformed=row.malformed,
        detections=row.detections,
        alerts=row.alerts,
    )


def insert_run(session: Session, run: SavedRun, *, org_id: str) -> None:
    session.add(
        RunRow(
            run_id=run.run_id,
            org_id=org_id,
            name=run.name,
            created_by=run.created_by,
            run_at=run.run_at,
            spec=run.spec,
            sources=[vars(source) for source in run.sources],
            warnings=list(run.warnings),
            events=run.events,
            malformed=run.malformed,
            detections=run.detections,
            alerts=run.alerts,
        )
    )
    session.flush()


def list_runs(
    session: Session, *, org_id: str, limit: int = 100, offset: int = 0
) -> Page[SavedRun]:
    """This organization's runs, newest first (``run_id`` breaks a tie, so paging is total)."""
    stmt = (
        select(RunRow)
        .where(RunRow.org_id == org_id)
        .order_by(RunRow.run_at.desc(), RunRow.run_id.desc())
    )
    rows = session.scalars(stmt.limit(limit).offset(offset))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    return Page([_record(row) for row in rows], total)


def _row(session: Session, run_id: UUID, *, org_id: str) -> RunRow | None:
    row = session.get(RunRow, run_id)
    return row if row is not None and row.org_id == org_id else None


def get_run(session: Session, run_id: UUID, *, org_id: str) -> SavedRun | None:
    row = _row(session, run_id, org_id=org_id)
    return _record(row) if row is not None else None


def rename_run(session: Session, run_id: UUID, name: str, *, org_id: str) -> SavedRun | None:
    """Give a run a new name; ``None`` when the organization has no such run."""
    row = _row(session, run_id, org_id=org_id)
    if row is None:
        return None
    row.name = name
    session.flush()
    return _record(row)


def delete_run(session: Session, run_id: UUID, *, org_id: str) -> bool:
    """Delete a run **and everything it stored**; ``False`` when there is no such run.

    The results go with it because a result with no run is exactly what this table exists to
    end: it would sit in Triage with nothing to say where it came from. Assets stay — a device
    outlives the runs that saw it, and another run may have seen it too.
    """
    if _row(session, run_id, org_id=org_id) is None:
        return False
    alerts = select(AlertRow.alert_id).where(AlertRow.org_id == org_id, AlertRow.run_id == run_id)
    session.execute(delete(AlertDetectionRow).where(AlertDetectionRow.alert_id.in_(alerts)))
    for row_type in (AlertRow, DetectionRow, EventRow, MalformedRow):
        session.execute(
            delete(row_type).where(row_type.org_id == org_id, row_type.run_id == run_id)
        )
    session.execute(delete(RunRow).where(RunRow.run_id == run_id, RunRow.org_id == org_id))
    session.flush()
    return True
