"""Saved jobs and their executions (ADR-0050).

Like ``runs`` and ``data_sources``: callers get frozen records, never ORM rows; functions flush
but never commit; and every function a request can reach takes ``org_id`` as a required keyword,
so another organization's job is indistinguishable from one that never existed.

The worker's functions (``due_jobs``, ``claim_next``, ``fail_stale``…) are the exception: they act
across organizations by design, since one worker serves them all, and each execution they hand
back carries its own ``org_id`` for everything that follows.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from iotshield.storage.orm import JobExecutionRow, JobNotificationRow, JobRow
from iotshield.storage.repository import Page

Status = Literal["queued", "running", "succeeded", "failed"]
Trigger = Literal["schedule", "manual"]
ACTIVE: tuple[Status, ...] = ("queued", "running")


@dataclass(frozen=True)
class Job:
    job_id: UUID
    org_id: str
    name: str
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    spec: dict[str, Any]
    sources: list[dict[str, Any]]
    schedule: dict[str, Any] | None
    enabled: bool
    next_run_at: datetime | None
    notify_emails: tuple[str, ...] = ()  # who is emailed when an execution finishes (ADR-0052)


@dataclass(frozen=True)
class Execution:
    execution_id: UUID
    job_id: UUID
    org_id: str
    trigger: str
    requested_by: str | None
    status: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    run_id: UUID | None
    error: str | None


def _recipients(session: Session, job_id: UUID) -> tuple[str, ...]:
    row = session.get(JobNotificationRow, job_id)
    return tuple(row.recipients) if row is not None else ()


def _set_recipients(
    session: Session, job_id: UUID, org_id: str, recipients: Sequence[str]
) -> tuple[str, ...]:
    row = session.get(JobNotificationRow, job_id)
    if not recipients:
        if row is not None:
            session.delete(row)
        return ()
    if row is None:
        session.add(JobNotificationRow(job_id=job_id, org_id=org_id, recipients=list(recipients)))
    else:
        row.recipients = list(recipients)
    return tuple(recipients)


def _job(row: JobRow, notify_emails: Sequence[str] = ()) -> Job:
    return Job(
        job_id=row.job_id,
        org_id=row.org_id,
        name=row.name,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        spec=row.spec,
        sources=list(row.sources),
        schedule=row.schedule,
        enabled=row.enabled,
        next_run_at=row.next_run_at,
        notify_emails=tuple(notify_emails),
    )


def _execution(row: JobExecutionRow) -> Execution:
    return Execution(
        execution_id=row.execution_id,
        job_id=row.job_id,
        org_id=row.org_id,
        trigger=row.trigger,
        requested_by=row.requested_by,
        status=row.status,
        queued_at=row.queued_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        run_id=row.run_id,
        error=row.error,
    )


def _row(session: Session, job_id: UUID, org_id: str) -> JobRow | None:
    row = session.get(JobRow, job_id)
    return row if row is not None and row.org_id == org_id else None


# --- What a request reaches: always inside one organization -------------------------------


def add_job(
    session: Session,
    *,
    org_id: str,
    name: str,
    created_by: str | None,
    spec: dict[str, Any],
    sources: list[dict[str, Any]],
    schedule: dict[str, Any] | None,
    enabled: bool,
    next_run_at: datetime | None,
    now: datetime,
    notify_emails: Sequence[str] = (),
) -> Job:
    row = JobRow(
        job_id=uuid4(),
        org_id=org_id,
        name=name,
        created_by=created_by,
        created_at=now,
        updated_at=now,
        spec=spec,
        sources=sources,
        schedule=schedule,
        enabled=enabled,
        next_run_at=next_run_at,
    )
    session.add(row)
    session.flush()
    recipients = _set_recipients(session, row.job_id, org_id, notify_emails)
    session.flush()
    return _job(row, recipients)


def update_job(
    session: Session,
    job_id: UUID,
    *,
    org_id: str,
    name: str,
    spec: dict[str, Any],
    sources: list[dict[str, Any]],
    schedule: dict[str, Any] | None,
    enabled: bool,
    next_run_at: datetime | None,
    now: datetime,
    notify_emails: Sequence[str] = (),
) -> Job | None:
    row = _row(session, job_id, org_id)
    if row is None:
        return None
    row.name = name
    row.spec = spec
    row.sources = sources
    row.schedule = schedule
    row.enabled = enabled
    row.next_run_at = next_run_at
    row.updated_at = now
    recipients = _set_recipients(session, job_id, org_id, notify_emails)
    session.flush()
    return _job(row, recipients)


def get_job(session: Session, job_id: UUID, *, org_id: str) -> Job | None:
    row = _row(session, job_id, org_id)
    return _job(row, _recipients(session, job_id)) if row is not None else None


def list_jobs(session: Session, *, org_id: str) -> list[Job]:
    rows = session.scalars(
        select(JobRow).where(JobRow.org_id == org_id).order_by(JobRow.name, JobRow.created_at)
    )
    rows = list(rows)
    found = session.scalars(select(JobNotificationRow).where(JobNotificationRow.org_id == org_id))
    recipients = {row.job_id: row.recipients for row in found}
    return [_job(row, recipients.get(row.job_id, ())) for row in rows]


def delete_job(session: Session, job_id: UUID, *, org_id: str) -> bool:
    """Delete the job and its execution history. The runs it produced are kept."""
    row = _row(session, job_id, org_id)
    if row is None:
        return False
    session.execute(delete(JobExecutionRow).where(JobExecutionRow.job_id == job_id))
    session.execute(delete(JobNotificationRow).where(JobNotificationRow.job_id == job_id))
    session.delete(row)
    session.flush()
    return True


def active_execution(session: Session, job_id: UUID, *, org_id: str) -> Execution | None:
    """The job's queued or running execution, if it has one — a job never runs twice at once."""
    row = session.scalars(
        select(JobExecutionRow)
        .where(
            JobExecutionRow.job_id == job_id,
            JobExecutionRow.org_id == org_id,
            JobExecutionRow.status.in_(ACTIVE),
        )
        .limit(1)
    ).first()
    return _execution(row) if row is not None else None


def queue_execution(
    session: Session,
    job_id: UUID,
    *,
    org_id: str,
    trigger: Trigger,
    requested_by: str | None,
    now: datetime,
) -> Execution:
    row = JobExecutionRow(
        execution_id=uuid4(),
        job_id=job_id,
        org_id=org_id,
        trigger=trigger,
        requested_by=requested_by,
        status="queued",
        queued_at=now,
    )
    session.add(row)
    session.flush()
    return _execution(row)


def list_executions(
    session: Session, job_id: UUID, *, org_id: str, limit: int, offset: int = 0
) -> Page[Execution]:
    where = (JobExecutionRow.job_id == job_id, JobExecutionRow.org_id == org_id)
    total = session.scalar(select(func.count()).select_from(JobExecutionRow).where(*where)) or 0
    rows = session.scalars(
        select(JobExecutionRow)
        .where(*where)
        .order_by(JobExecutionRow.queued_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return Page([_execution(row) for row in rows], total)


def latest_executions(session: Session, *, org_id: str) -> dict[UUID, Execution]:
    """Each of the organization's jobs' most recent execution, for the jobs list."""
    newest = (
        select(JobExecutionRow.job_id, func.max(JobExecutionRow.queued_at).label("at"))
        .where(JobExecutionRow.org_id == org_id)
        .group_by(JobExecutionRow.job_id)
        .subquery()
    )
    rows = session.scalars(
        select(JobExecutionRow).join(
            newest,
            (JobExecutionRow.job_id == newest.c.job_id)
            & (JobExecutionRow.queued_at == newest.c.at),
        )
    )
    return {row.job_id: _execution(row) for row in rows}


# --- What the worker reaches: across organizations, one execution at a time --------------


def due_jobs(session: Session, *, now: datetime) -> Sequence[JobRow]:
    """Enabled jobs whose next run time has come, locked so a second worker skips them.

    Returns ORM rows because the caller moves ``next_run_at`` on them in the same transaction.
    """
    return session.scalars(
        select(JobRow)
        .where(JobRow.enabled.is_(True), JobRow.next_run_at.is_not(None), JobRow.next_run_at <= now)
        .order_by(JobRow.next_run_at)
        .with_for_update(skip_locked=True)
    ).all()


def claim_next(session: Session, *, now: datetime) -> Execution | None:
    """Take the oldest queued execution: mark it running and start its heartbeat."""
    row = session.scalars(
        select(JobExecutionRow)
        .where(JobExecutionRow.status == "queued")
        .order_by(JobExecutionRow.queued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if row is None:
        return None
    row.status = "running"
    row.started_at = now
    row.heartbeat_at = now
    session.flush()
    return _execution(row)


def beat(session: Session, execution_id: UUID, *, now: datetime) -> None:
    session.execute(
        update(JobExecutionRow)
        .where(JobExecutionRow.execution_id == execution_id, JobExecutionRow.status == "running")
        .values(heartbeat_at=now)
    )


def finish(
    session: Session,
    execution_id: UUID,
    *,
    now: datetime,
    run_id: UUID | None = None,
    error: str | None = None,
) -> None:
    """Record the outcome: succeeded with the run it produced, or failed with why."""
    row = session.get(JobExecutionRow, execution_id)
    if row is None:  # pragma: no cover - the job was deleted while it ran
        return
    row.status = "failed" if error is not None else "succeeded"
    row.finished_at = now
    row.run_id = run_id
    row.error = error
    session.flush()


def fail_stale(session: Session, *, now: datetime, stale_after: timedelta) -> int:
    """Fail every running execution whose heartbeat stopped: its worker is gone."""
    result = session.execute(
        update(JobExecutionRow)
        .where(
            JobExecutionRow.status == "running",
            JobExecutionRow.heartbeat_at < now - stale_after,
        )
        .values(
            status="failed",
            finished_at=now,
            error="the worker stopped before this run finished; nothing it read was stored",
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


def job_row(session: Session, job_id: UUID) -> Job | None:
    """A job by id alone, for the worker, which already holds the execution's ``org_id``."""
    row = session.get(JobRow, job_id)
    return _job(row, _recipients(session, job_id)) if row is not None else None
