"""FastAPI read-side service — the contracts are literally the wire format.

Endpoints return contract objects; FastAPI serializes them, so the API schema
IS the contract schema with zero translation code (the payoff of ADR-0003).
Read-only v1: the pipeline runner is the only writer (ADR-0007).

``create_app(engine)`` builds the app around an injected engine: tests pass a
SQLite engine and drive the app with TestClient; production passes Postgres.
No CORS on purpose — the dashboard calls the API server-to-server, so no
browser cross-origin request ever occurs in this architecture.

Every route except ``/health`` sits behind the login gate (``_auth.authenticate``, an
app-level dependency), and every query is scoped to the caller's organization (ADR-0039).
With ``IOTSHIELD_AUTH`` off the caller is the single default tenant, as before.
"""

from collections.abc import Callable, Iterator
from typing import Annotated, ParamSpec, TypeVar
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import iotshield
from iotshield.api._auth import AccountDep, auth_enabled, authenticate
from iotshield.contracts import (
    AlertStatus,
    Asset,
    DetectionResult,
    Lane,
    MalformedRecord,
    NormalizedEvent,
    PrioritizedAlert,
    UtcDatetime,
)
from iotshield.storage import make_session_factory, repository

_P = ParamSpec("_P")
_Item = TypeVar("_Item")

# Query-param bounds: generous for an MVP, but never unbounded.
LimitParam = Annotated[int, Query(ge=1, le=10_000)]

OffsetParam = Annotated[
    int,
    Query(
        ge=0,
        description=(
            "How many rows to skip. Every list ordering here is total (each ends in a unique "
            "id), so paging cannot skip or repeat a row. Read X-Total-Count to learn whether "
            "more remain."
        ),
    ),
]

TOTAL_HEADER = "X-Total-Count"
"""How many rows matched, before limit/offset.

A header rather than an envelope so the response BODY stays exactly the contract objects,
which is what lets the generated TypeScript types and their zod schemas keep working
unchanged. Without it a capped list is indistinguishable from a complete one — and since
these queries order newest-first and then reverse, the rows dropped are the OLDEST, so the
FRONT of the list was silently wrong (ADR-0033).
"""


def _paged(
    response: Response, query: Callable[_P, repository.Page[_Item]]
) -> Callable[_P, list[_Item]]:
    """Run a paged repository query, publish its total, return just the items.

    Wrapped once rather than per endpoint so no route can serve a page and forget to say how
    much it left behind.
    """

    def run(*args: _P.args, **kwargs: _P.kwargs) -> list[_Item]:
        page = query(*args, **kwargs)
        response.headers[TOTAL_HEADER] = str(page.total)
        return page.items

    return run


# Query datetimes reuse the contracts' canonical rule (golden rule 4): naive
# input is a 422, aware input is normalized to UTC — the same policy every
# other boundary applies, not a weaker parallel one.
TimeParam = Annotated[UtcDatetime | None, Query()]


def _get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(_get_session)]


def _require_alert(session: Session, alert_id: UUID, *, org_id: str) -> PrioritizedAlert:
    # Another organization's alert is a 404 like any unknown id: a 403 would confirm it exists.
    alert = repository.get_alert_by_id(session, alert_id, org_id=org_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return alert


class Summary(BaseModel):
    """KPI payload for the dashboard header row."""

    counts: dict[str, int]
    top_alert: PrioritizedAlert | None


def create_app(engine: Engine, *, auth: bool | None = None) -> FastAPI:
    """``auth`` overrides ``IOTSHIELD_AUTH`` (tests pass it; services read the env)."""
    # The API is read-only and never creates the schema: the compose init step
    # (iotshield.storage.init) owns it, and the pipeline runner creates it for
    # standalone use. create_app assumes the tables already exist (ADR-0010).
    app = FastAPI(
        title="iotshield API",
        version=iotshield.__version__,
        dependencies=[Depends(authenticate)],
    )
    app.state.session_factory = make_session_factory(engine)
    app.state.auth_enabled = auth_enabled() if auth is None else auth

    @app.get("/health")
    def health(session: SessionDep) -> dict[str, str]:
        session.execute(select(1))  # proves DB connectivity, not just liveness
        return {"status": "ok", "version": iotshield.__version__}

    @app.get("/assets")
    def assets(session: SessionDep, account: AccountDep) -> list[Asset]:
        return repository.list_assets(session, org_id=account.org_id)

    @app.get("/events")
    def events(
        response: Response,
        session: SessionDep,
        account: AccountDep,
        asset_id: str | None = None,
        lane: Lane | None = None,
        start: TimeParam = None,
        end: TimeParam = None,
        limit: LimitParam = 1000,
        offset: OffsetParam = 0,
    ) -> list[NormalizedEvent]:
        """Events oldest-first. `end` is a CLOSED bound (see the note on TimeParam)."""
        return _paged(response, repository.get_events)(
            session,
            org_id=account.org_id,
            asset_id=asset_id,
            lane=lane,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )

    @app.get("/detections")
    def detections(
        response: Response,
        session: SessionDep,
        account: AccountDep,
        asset_id: str | None = None,
        lane: Lane | None = None,
        start: TimeParam = None,
        end: TimeParam = None,
        limit: LimitParam = 1000,
        offset: OffsetParam = 0,
    ) -> list[DetectionResult]:
        """Detections oldest-first. `end` is HALF-OPEN, matched on `window_start`."""
        return _paged(response, repository.get_detections)(
            session,
            org_id=account.org_id,
            asset_id=asset_id,
            lane=lane,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )

    @app.get("/alerts")
    def alerts(
        response: Response,
        session: SessionDep,
        account: AccountDep,
        status: AlertStatus | None = None,
        asset_id: str | None = None,
        start: TimeParam = None,
        end: TimeParam = None,
        limit: LimitParam = 1000,
        offset: OffsetParam = 0,
    ) -> list[PrioritizedAlert]:
        """The ranked triage queue: priority descending. `end` is HALF-OPEN on window_start."""
        return _paged(response, repository.get_alerts)(
            session,
            org_id=account.org_id,
            status=status,
            asset_id=asset_id,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )

    @app.get("/alerts/{alert_id}")
    def alert_by_id(session: SessionDep, account: AccountDep, alert_id: UUID) -> PrioritizedAlert:
        return _require_alert(session, alert_id, org_id=account.org_id)

    @app.get("/alerts/{alert_id}/detections")
    def alert_detections(
        session: SessionDep, account: AccountDep, alert_id: UUID
    ) -> list[DetectionResult]:
        """Drill-down: the evidence behind one alert."""
        alert = _require_alert(session, alert_id, org_id=account.org_id)
        return repository.get_detections_by_ids(session, alert.detection_ids, org_id=account.org_id)

    @app.get("/malformed")
    def malformed(
        response: Response,
        session: SessionDep,
        account: AccountDep,
        limit: LimitParam = 1000,
        offset: OffsetParam = 0,
    ) -> list[MalformedRecord]:
        """The audit view: inputs that failed normalization (never dropped)."""
        return _paged(response, repository.list_malformed)(
            session, org_id=account.org_id, limit=limit, offset=offset
        )

    @app.get("/summary")
    def summary(session: SessionDep, account: AccountDep) -> Summary:
        top = repository.get_alerts(session, org_id=account.org_id, limit=1)
        return Summary(
            counts=repository.count_summary(session, org_id=account.org_id),
            top_alert=top.items[0] if top.items else None,
        )

    # Chart-sized counts for the IT and OT overview pages (ADR-0044). Read-only like the rest.
    from iotshield.api.overview import create_overview_router

    app.include_router(create_overview_router())

    return app
