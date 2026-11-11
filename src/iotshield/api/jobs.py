"""The saved-job routes (ADR-0050): save a chain of pulls, schedule it, run it again.

Mounted on the **Pipeline API**, which owns runs. Anyone in the organization may list, create,
edit and run its jobs; only its admins delete one, the same split as its runs. A job is checked
when it is saved by the same code that checks a run, without reading anything, so a job that
could never run is refused here rather than failing every night.

Each job may name who is emailed when an execution finishes (``notify_emails``, ADR-0052); the
worker sends it. Omitted on create, it defaults to the creator when their username is an email
address; an empty list means nobody.

"Run now" does not run in the request: it queues an execution for the ``worker`` service, so a
job is never subject to the proxy's timeout and never runs twice at once (409 while one is
queued or running).
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session, sessionmaker

from iotshield.api._auth import AccountDep, AdminDep
from iotshield.jobs.model import JobSource, Schedule
from iotshield.jobs.run import check_job
from iotshield.jobs.worker import utc_now
from iotshield.pipeline.config import PipelineSpec, config_from_spec
from iotshield.storage import jobs as store
from iotshield.storage.accounts import validate_email
from iotshield.storage.jobs import Execution, Job
from iotshield.storage.runs import MAX_NAME_LENGTH, clean_name

TOTAL_HEADER = "X-Total-Count"
MAX_RECIPIENTS = 20


class JobRequest(BaseModel):
    """A job as saved: what it runs, what each pull reads, and when it runs by itself."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    spec: PipelineSpec
    sources: list[JobSource] = Field(
        min_length=1, description="One pull per spec.sources entry (same order)"
    )
    schedule: Schedule | None = Field(
        default=None, description="When it runs by itself (UTC); null = only when asked"
    )
    enabled: bool = Field(default=True, description="A disabled job keeps its schedule but waits")
    notify_emails: list[str] | None = Field(
        default=None,
        max_length=MAX_RECIPIENTS,
        description=(
            "Who is emailed each execution's outcome. Omitted on create: the creator, when "
            "their username is an email address; omitted on edit: unchanged; [] = nobody"
        ),
    )

    @field_validator("notify_emails")
    @classmethod
    def _recipients(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for address in value:
            checked = validate_email(address)
            if checked not in cleaned:  # a duplicate would be one person mailed twice
                cleaned.append(checked)
        return cleaned

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = clean_name(value)
        if cleaned is None:
            raise ValueError("name must not be blank")
        return cleaned


class ExecutionInfo(BaseModel):
    execution_id: UUID
    job_id: UUID
    trigger: str = Field(description="schedule | manual")
    requested_by: str | None
    status: str = Field(description="queued | running | succeeded | failed")
    queued_at: str
    started_at: str | None
    finished_at: str | None
    run_id: UUID | None = Field(description="The saved run it produced, when it succeeded")
    error: str | None


class JobInfo(BaseModel):
    job_id: UUID
    name: str
    created_by: str | None
    created_at: str
    updated_at: str
    spec: dict[str, Any]
    sources: list[JobSource]
    schedule: Schedule | None
    schedule_text: str | None = Field(
        description="The schedule in words, e.g. every day at 02:00 UTC"
    )
    enabled: bool
    next_run_at: str | None
    notify_emails: list[str] = Field(description="Who is emailed each execution's outcome")
    last: ExecutionInfo | None = Field(description="The most recent execution, if any")


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _execution_info(execution: Execution) -> ExecutionInfo:
    return ExecutionInfo(
        execution_id=execution.execution_id,
        job_id=execution.job_id,
        trigger=execution.trigger,
        requested_by=execution.requested_by,
        status=execution.status,
        queued_at=execution.queued_at.isoformat(),
        started_at=_iso(execution.started_at),
        finished_at=_iso(execution.finished_at),
        run_id=execution.run_id,
        error=execution.error,
    )


def _info(job: Job, last: Execution | None) -> JobInfo:
    schedule = Schedule.model_validate(job.schedule) if job.schedule else None
    return JobInfo(
        job_id=job.job_id,
        name=job.name,
        created_by=job.created_by,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        spec=job.spec,
        sources=[JobSource.model_validate(source) for source in job.sources],
        schedule=schedule,
        schedule_text=schedule.describe() if schedule else None,
        enabled=job.enabled,
        next_run_at=_iso(job.next_run_at),
        notify_emails=list(job.notify_emails),
        last=_execution_info(last) if last is not None else None,
    )


def _session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(_session)]


def _now(request: Request) -> datetime:
    # Tests set `app.state.clock` to pin "now"; the server reads the wall clock.
    clock = getattr(request.app.state, "clock", utc_now)
    return clock()


def _checked(request: Request, body: JobRequest, org_id: str, now: datetime) -> dict[str, Any]:
    """The job's fields as stored, once the run it describes is known to be possible."""
    config = config_from_spec(body.spec, request.app.state.database_url)
    check_job(config, body.sources, request.app.state.session_factory, org_id=org_id, now=now)
    return {
        "name": body.name,
        "spec": body.spec.model_dump(mode="json"),
        "sources": [source.model_dump(mode="json") for source in body.sources],
        "schedule": body.schedule.model_dump(mode="json") if body.schedule else None,
        "enabled": body.enabled,
        "next_run_at": (
            body.schedule.next_after(now) if body.schedule is not None and body.enabled else None
        ),
        "now": now,
    }


def _default_recipients(username: str) -> list[str]:
    """The creator, when their username is an address (a pre-ADR-0052 account may not be)."""
    try:
        return [validate_email(username)]
    except ValueError:
        return []


def _found(job: Job | None) -> Job:
    # Another organization's job is a 404, exactly like an id that never existed.
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def create_jobs_router() -> APIRouter:
    router = APIRouter(prefix="/jobs", tags=["jobs"])

    @router.get("")
    def list_saved_jobs(account: AccountDep, session: SessionDep) -> list[JobInfo]:
        """This organization's jobs by name, each with its most recent execution."""
        latest = store.latest_executions(session, org_id=account.org_id)
        return [
            _info(job, latest.get(job.job_id))
            for job in store.list_jobs(session, org_id=account.org_id)
        ]

    @router.post("", status_code=201)
    def create_job(
        body: JobRequest, request: Request, account: AccountDep, session: SessionDep
    ) -> JobInfo:
        """Save a job; 422 with the run's own reasons when it could never run."""
        fields = _checked(request, body, account.org_id, _now(request))
        recipients = body.notify_emails
        if recipients is None:
            recipients = _default_recipients(account.user.username)
        job = store.add_job(
            session,
            org_id=account.org_id,
            created_by=account.user.username,
            notify_emails=recipients,
            **fields,
        )
        session.commit()
        return _info(job, None)

    @router.get("/{job_id}")
    def read_job(job_id: UUID, account: AccountDep, session: SessionDep) -> JobInfo:
        job = _found(store.get_job(session, job_id, org_id=account.org_id))
        return _info(job, store.latest_executions(session, org_id=account.org_id).get(job_id))

    @router.put("/{job_id}")
    def change_job(
        job_id: UUID, body: JobRequest, request: Request, account: AccountDep, session: SessionDep
    ) -> JobInfo:
        """Replace the job's chain, pulls and schedule; its history is kept."""
        current = _found(store.get_job(session, job_id, org_id=account.org_id))
        fields = _checked(request, body, account.org_id, _now(request))
        recipients = (
            list(current.notify_emails) if body.notify_emails is None else body.notify_emails
        )
        job = _found(
            store.update_job(
                session, job_id, org_id=account.org_id, notify_emails=recipients, **fields
            )
        )
        session.commit()
        return _info(job, store.latest_executions(session, org_id=account.org_id).get(job_id))

    @router.delete("/{job_id}", status_code=204)
    def remove_job(job_id: UUID, account: AdminDep, session: SessionDep) -> None:
        """Delete the job and its history. The runs it produced stay on the Runs page."""
        if not store.delete_job(session, job_id, org_id=account.org_id):
            raise HTTPException(status_code=404, detail="job not found")
        session.commit()

    @router.post("/{job_id}/run", status_code=202)
    def run_job(
        job_id: UUID, request: Request, account: AccountDep, session: SessionDep
    ) -> ExecutionInfo:
        """Queue the job for the worker now. 409 while it is already queued or running."""
        _found(store.get_job(session, job_id, org_id=account.org_id))
        if store.active_execution(session, job_id, org_id=account.org_id) is not None:
            raise HTTPException(status_code=409, detail="this job is already queued or running")
        execution = store.queue_execution(
            session,
            job_id,
            org_id=account.org_id,
            trigger="manual",
            requested_by=account.user.username,
            now=_now(request),
        )
        session.commit()
        return _execution_info(execution)

    @router.get("/{job_id}/executions")
    def job_executions(
        job_id: UUID,
        response: Response,
        account: AccountDep,
        session: SessionDep,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[ExecutionInfo]:
        """The job's executions, newest first; ``X-Total-Count`` says how many exist."""
        _found(store.get_job(session, job_id, org_id=account.org_id))
        page = store.list_executions(
            session, job_id, org_id=account.org_id, limit=limit, offset=offset
        )
        response.headers[TOTAL_HEADER] = str(page.total)
        return [_execution_info(execution) for execution in page]

    return router
