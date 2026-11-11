"""Checking a job when it is saved, and running one execution of it (ADR-0050).

Both go through ``api/execution.py``, the code ``POST /runs`` uses, so a job is refused at save
for exactly what would refuse its run, and a job's run is stored exactly as a clicked one is.
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session, sessionmaker

from iotshield.api.execution import (
    PullInput,
    SourceInput,
    checked_pull,
    failure_message,
    perform_run,
    refused,
)
from iotshield.jobs.model import JobSource
from iotshield.pipeline.compat import check_compatibility, has_errors
from iotshield.pipeline.config import PipelineConfig, PipelineSpec, config_from_spec
from iotshield.storage.accounts import get_organization
from iotshield.storage.jobs import Execution, Job, job_row


def job_inputs(sources: Sequence[JobSource], now: datetime) -> list[SourceInput]:
    """The run's inputs at ``now``: each pull with its window worked out (rolling ones move)."""
    inputs: list[SourceInput] = []
    for source in sources:
        start, end = source.window.resolve(now)
        inputs.append(
            SourceInput(
                pull=PullInput(
                    source_id=source.source_id, start=start, end=end, params=source.params
                )
            )
        )
    return inputs


def check_job(
    config: PipelineConfig,
    sources: Sequence[JobSource],
    session_factory: sessionmaker[Session],
    *,
    org_id: str,
    now: datetime,
) -> None:
    """Refuse (``HTTPException``) a job whose run would be refused — without reading anything."""
    issues = check_compatibility(config)
    if has_errors(issues):
        raise refused("incompatible pipeline configuration", issues)
    if len(sources) != len(config.sources):
        raise refused(
            f"expected {len(config.sources)} pull(s) to match spec.sources, got {len(sources)}"
        )
    with session_factory() as session:
        for index, (item, source) in enumerate(
            zip(job_inputs(sources, now), config.sources, strict=True)
        ):
            assert item.pull is not None  # job_inputs builds only pulls
            try:
                checked_pull(index, source, item.pull, session, org_id=org_id)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                # The *job* exists; its source does not (or is another organization's).
                raise refused(f"source {index + 1}: data source not found") from exc


def run_name(job: Job, now: datetime) -> str:
    return f"{job.name} · {now:%Y-%m-%d %H:%M} UTC"


def run_execution(
    execution: Execution,
    *,
    session_factory: sessionmaker[Session],
    database_url: str,
    output_dir: Path,
    now: datetime,
) -> tuple[UUID | None, str | None]:
    """Run one execution: ``(run_id, None)`` when it stored a run, ``(None, why)`` when not."""
    with session_factory() as session:
        job = job_row(session, execution.job_id)
        organization = get_organization(session, execution.org_id)
    if job is None or job.org_id != execution.org_id:
        return None, "the job was deleted"
    if organization is None or organization.suspended:
        return None, "the organization is suspended"
    try:
        spec = PipelineSpec.model_validate(job.spec)
        sources = [JobSource.model_validate(source) for source in job.sources]
    except ValueError as exc:  # pragma: no cover - a job is validated when saved
        return None, f"the job's saved chain is no longer valid: {exc}"
    config = config_from_spec(spec, database_url)
    try:
        report = perform_run(
            config,
            job_inputs(sources, now),
            session_factory=session_factory,
            output_dir=output_dir,
            org_id=execution.org_id,
            run_name=run_name(job, now),
            run_by=execution.requested_by or job.created_by,
        )
    except HTTPException as exc:
        return None, failure_message(exc)
    return report.run_id, None
