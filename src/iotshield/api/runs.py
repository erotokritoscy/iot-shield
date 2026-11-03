"""The saved-run routes: list, read, rename and delete an organization's runs (ADR-0046).

Mounted on the **Pipeline API** beside ``POST /runs``, which creates them: renaming and
deleting write, and ADR-0007 keeps the read API write-free. Anyone in the organization may
list, open and rename its runs; only its admins delete one — the same split as its data
sources — because deleting a run deletes every result it stored.

A run's *results* are still read from the read API, filtered by ``run_id`` (ADR-0045). This
router serves the run's own record: its name, who ran it, the chain it ran and its counts.
"""

from collections.abc import Iterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session, sessionmaker

from iotshield.api._auth import AccountDep, AdminDep
from iotshield.storage.runs import (
    MAX_NAME_LENGTH,
    SavedRun,
    clean_name,
    delete_run,
    get_run,
    list_runs,
    rename_run,
)

TOTAL_HEADER = "X-Total-Count"


class RunPullInfo(BaseModel):
    """The data-source pull an input was read from (ADR-0047) — enough to read it again."""

    source_id: str
    start: str
    end: str
    params: dict[str, Any] = {}


class RunSourceInfo(BaseModel):
    normalizer: str
    source_format: str
    origin: str
    pull: RunPullInfo | None = Field(
        default=None, description="Set when this input was a pull; Open in Compose restores it"
    )


class SavedRunInfo(BaseModel):
    run_id: UUID
    name: str
    created_by: str | None
    run_at: str
    # The chain as it ran: a `PipelineSpec` as JSON, which the Compose page reopens.
    spec: dict[str, Any]
    sources: list[RunSourceInfo]
    warnings: list[str]
    events: int
    malformed: int
    detections: int
    alerts: int


class RenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = clean_name(value)
        if cleaned is None:
            raise ValueError("name must not be blank")
        return cleaned


def _info(run: SavedRun) -> SavedRunInfo:
    return SavedRunInfo(
        run_id=run.run_id,
        name=run.name,
        created_by=run.created_by,
        run_at=run.run_at.isoformat(),
        spec=run.spec,
        sources=[RunSourceInfo(**vars(source)) for source in run.sources],
        warnings=run.warnings,
        events=run.events,
        malformed=run.malformed,
        detections=run.detections,
        alerts=run.alerts,
    )


def _session(request: Request) -> Iterator[Session]:
    # Each writing route commits as its last step, so a route that raises commits nothing.
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(_session)]


def create_runs_router() -> APIRouter:
    router = APIRouter(prefix="/runs", tags=["runs"])

    @router.get("")
    def list_saved_runs(
        response: Response,
        account: AccountDep,
        session: SessionDep,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[SavedRunInfo]:
        """This organization's runs, newest first; ``X-Total-Count`` says how many exist."""
        page = list_runs(session, org_id=account.org_id, limit=limit, offset=offset)
        response.headers[TOTAL_HEADER] = str(page.total)
        return [_info(run) for run in page]

    @router.get("/{run_id}")
    def read_run(run_id: UUID, account: AccountDep, session: SessionDep) -> SavedRunInfo:
        run = get_run(session, run_id, org_id=account.org_id)
        # Another organization's run is a 404, exactly like an id that never existed.
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _info(run)

    @router.patch("/{run_id}")
    def rename(
        run_id: UUID, body: RenameRequest, account: AccountDep, session: SessionDep
    ) -> SavedRunInfo:
        run = rename_run(session, run_id, body.name, org_id=account.org_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        session.commit()
        return _info(run)

    @router.delete("/{run_id}", status_code=204)
    def remove(run_id: UUID, account: AdminDep, session: SessionDep) -> None:
        """Delete the run and every event, malformed record, detection and alert it stored."""
        if not delete_run(session, run_id, org_id=account.org_id):
            raise HTTPException(status_code=404, detail="run not found")
        session.commit()

    return router
