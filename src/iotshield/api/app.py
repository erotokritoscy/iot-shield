"""FastAPI read-side service — the contracts are literally the wire format.

Endpoints return contract objects; FastAPI serializes them, so the API schema
IS the contract schema with zero translation code (the payoff of ADR-0003).
Read-only v1: the pipeline runner is the only writer (ADR-0007).

``create_app(engine)`` builds the app around an injected engine: tests pass a
SQLite engine and drive the app with TestClient; production passes Postgres.
No CORS on purpose — the dashboard calls the API server-to-server, so no
browser cross-origin request ever occurs in this architecture.
"""

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import iotshield
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

# Query-param bounds: generous for an MVP, but never unbounded.
LimitParam = Annotated[int, Query(ge=1, le=10_000)]

# Query datetimes reuse the contracts' canonical rule (golden rule 4): naive
# input is a 422, aware input is normalized to UTC — the same policy every
# other boundary applies, not a weaker parallel one.
TimeParam = Annotated[UtcDatetime | None, Query()]


def _get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(_get_session)]


def _require_alert(session: Session, alert_id: UUID) -> PrioritizedAlert:
    alert = repository.get_alert_by_id(session, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return alert


class Summary(BaseModel):
    """KPI payload for the dashboard header row."""

    counts: dict[str, int]
    top_alert: PrioritizedAlert | None


def create_app(engine: Engine) -> FastAPI:
    # The API is read-only and never creates the schema: the compose init step
    # (iotshield.storage.init) owns it, and the pipeline runner creates it for
    # standalone use. create_app assumes the tables already exist (ADR-0010).
    app = FastAPI(title="iotshield API", version=iotshield.__version__)
    app.state.session_factory = make_session_factory(engine)

    @app.get("/health")
    def health(session: SessionDep) -> dict[str, str]:
        session.execute(select(1))  # proves DB connectivity, not just liveness
        return {"status": "ok", "version": iotshield.__version__}

    @app.get("/assets")
    def assets(session: SessionDep) -> list[Asset]:
        return repository.list_assets(session)

    @app.get("/events")
    def events(
        session: SessionDep,
        asset_id: str | None = None,
        lane: Lane | None = None,
        start: TimeParam = None,
        end: TimeParam = None,
        limit: LimitParam = 1000,
    ) -> list[NormalizedEvent]:
        return repository.get_events(
            session, asset_id=asset_id, lane=lane, start=start, end=end, limit=limit
        )

    @app.get("/detections")
    def detections(
        session: SessionDep,
        asset_id: str | None = None,
        lane: Lane | None = None,
        start: TimeParam = None,
        end: TimeParam = None,
        limit: LimitParam = 1000,
    ) -> list[DetectionResult]:
        return repository.get_detections(
            session, asset_id=asset_id, lane=lane, start=start, end=end, limit=limit
        )

    @app.get("/alerts")
    def alerts(
        session: SessionDep,
        status: AlertStatus | None = None,
        limit: LimitParam = 1000,
    ) -> list[PrioritizedAlert]:
        """The ranked triage queue: priority descending."""
        return repository.get_alerts(session, status=status, limit=limit)

    @app.get("/alerts/{alert_id}")
    def alert_by_id(session: SessionDep, alert_id: UUID) -> PrioritizedAlert:
        return _require_alert(session, alert_id)

    @app.get("/alerts/{alert_id}/detections")
    def alert_detections(session: SessionDep, alert_id: UUID) -> list[DetectionResult]:
        """Drill-down: the evidence behind one alert."""
        alert = _require_alert(session, alert_id)
        return repository.get_detections_by_ids(session, alert.detection_ids)

    @app.get("/malformed")
    def malformed(session: SessionDep, limit: LimitParam = 1000) -> list[MalformedRecord]:
        """The audit view: inputs that failed normalization (never dropped)."""
        return repository.list_malformed(session, limit=limit)

    @app.get("/summary")
    def summary(session: SessionDep) -> Summary:
        top = repository.get_alerts(session, limit=1)
        return Summary(
            counts=repository.count_summary(session),
            top_alert=top[0] if top else None,
        )

    return app
