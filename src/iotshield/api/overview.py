"""The IT and OT overview routes of the read API: chart-sized counts (ADR-0044).

Read-only like everything on this app; mounted beside the list routes. Each answer is small
whatever the date range, because the counting is done in ``storage.overview`` — the list
routes are capped at 10,000 rows, and a chart drawn from a capped list would be a chart of
the cap.

Ranges are whole **UTC days**, ``start`` and ``end`` both inclusive, at most a year.
"""

from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from iotshield.api._auth import AccountDep
from iotshield.meter import QualityIssue
from iotshield.storage import overview

MAX_DAYS = 366
TOP_METERS = 20
TOTAL_HEADER = "X-Total-Count"


class DayCount(BaseModel):
    day: str
    count: int


class MethodCount(BaseModel):
    method: str
    count: int


class ItPoint(BaseModel):
    bucket: str
    flows: int
    flagged: int
    detections: int


class ItOverview(BaseModel):
    flows: int
    flagged: int
    """Flows inside the window of a flow-lane detection on the same device."""
    per_day: list[DayCount]
    per_method: list[MethodCount]
    latest_day: str | None
    """The most recent UTC day with any flow at all, whatever the range asked for."""
    series: list[ItPoint]
    """Per bucket (``bucket`` param) that had any flow or detection — what the replay plays."""


class IssueCount(BaseModel):
    issue: str
    category: int
    subcategory: int
    count: int


class SeriesPoint(BaseModel):
    bucket: str
    """``YYYY-MM-DD`` or, by hour, ``YYYY-MM-DDTHH:00:00+00:00``."""
    issue: str
    count: int


class MeterCount(BaseModel):
    asset_id: str
    count: int


class MeterPoint(BaseModel):
    bucket: str
    asset_id: str
    count: int


class OtOverview(BaseModel):
    total: int
    unplaced: int
    """Tagged rejects whose line never yielded a meter and a time: in ``per_type``, not in
    ``series`` or ``per_meter``."""
    per_type: list[IssueCount]
    """All six types, zeros included, in SensiPro's code order."""
    series: list[SeriesPoint]
    per_meter: list[MeterCount]
    """The meters with the most anomalies, at most twenty."""
    meters: int
    """How many meters had any anomaly."""
    meter_series: list[MeterPoint]
    """``per_meter``'s meters only, per bucket — so the replay can grow their bars."""
    latest_day: str | None
    """The most recent UTC day with any meter reading at all, whatever the range asked for."""


class QualityRecordInfo(BaseModel):
    issue: str
    source: Literal["detection", "reject"]
    asset_id: str | None
    at: datetime | None
    detail: str


def _session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(_session)]


def _window(start: date, end: date) -> tuple[datetime, datetime]:
    if end < start:
        raise HTTPException(status_code=422, detail="the end date is before the start date")
    if (end - start).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"at most {MAX_DAYS} days at a time")
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
    )


Start = Annotated[date, Query(description="First UTC day, inclusive")]
End = Annotated[date, Query(description="Last UTC day, inclusive")]
Asset = Annotated[str | None, Query(description="One meter only")]
_ORDER = sorted(QualityIssue, key=lambda issue: issue.sensipro_code)


def create_overview_router() -> APIRouter:
    router = APIRouter(prefix="/overview", tags=["overview"])

    @router.get("/it")
    def it_overview(
        start: Start,
        end: End,
        account: AccountDep,
        session: SessionDep,
        bucket: Annotated[Literal["day", "hour"], Query()] = "day",
    ) -> ItOverview:
        """Flows and flow-lane detections over a range of days."""
        low, high = _window(start, end)
        found = overview.flow_overview(
            session, org_id=account.org_id, start=low, end=high, bucket=bucket
        )
        return ItOverview(
            flows=found.flows,
            flagged=found.flagged,
            per_day=[DayCount(day=d, count=c) for d, c in found.detections_per_day.items()],
            per_method=[
                MethodCount(method=m, count=c) for m, c in found.detections_per_method.items()
            ],
            latest_day=overview.latest_day(session, org_id=account.org_id, kind="it"),
            series=[
                ItPoint(bucket=key, flows=flows, flagged=flagged, detections=detections)
                for key, flows, flagged, detections in found.series
            ],
        )

    @router.get("/ot")
    def ot_overview(
        start: Start,
        end: End,
        account: AccountDep,
        session: SessionDep,
        asset_id: Asset = None,
        bucket: Annotated[Literal["day", "hour"], Query()] = "day",
    ) -> OtOverview:
        """SensiPro's six meter anomaly types over a range of days (ADR-0043/0044)."""
        low, high = _window(start, end)
        records = overview.quality_records(
            session, org_id=account.org_id, start=low, end=high, asset_id=asset_id
        )
        placed = [record for record in records if record.at is not None and record.asset_id]
        by_type = Counter(record.issue for record in records)
        series = Counter(
            (overview.bucket_of(record.at, bucket), record.issue)
            for record in placed
            if record.at is not None
        )
        per_meter = Counter(record.asset_id for record in placed if record.asset_id)
        top = [meter for meter, _ in per_meter.most_common(TOP_METERS)]
        meter_series = Counter(
            (overview.bucket_of(record.at, bucket), record.asset_id)
            for record in placed
            if record.at is not None and record.asset_id in top
        )
        return OtOverview(
            total=len(records),
            unplaced=len(records) - len(placed),
            per_type=[
                IssueCount(
                    issue=issue.value,
                    category=issue.sensipro_code[0],
                    subcategory=issue.sensipro_code[1],
                    count=by_type[issue],
                )
                for issue in _ORDER
            ],
            series=[
                SeriesPoint(bucket=when, issue=issue.value, count=count)
                for (when, issue), count in sorted(series.items())
            ],
            per_meter=[
                MeterCount(asset_id=meter, count=count)
                for meter, count in per_meter.most_common(TOP_METERS)
            ],
            meters=len(per_meter),
            meter_series=[
                MeterPoint(bucket=when, asset_id=meter, count=count)
                for (when, meter), count in sorted(meter_series.items())
            ],
            latest_day=overview.latest_day(session, org_id=account.org_id, kind="ot"),
        )

    @router.get("/ot/records")
    def ot_records(
        start: Start,
        end: End,
        account: AccountDep,
        session: SessionDep,
        response: Response,
        asset_id: Asset = None,
        issue: Annotated[QualityIssue | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[QualityRecordInfo]:
        """The anomalies themselves, oldest first; ``X-Total-Count`` says how many matched."""
        low, high = _window(start, end)
        records = overview.quality_records(
            session, org_id=account.org_id, start=low, end=high, asset_id=asset_id
        )
        if issue is not None:
            records = [record for record in records if record.issue is issue]
        response.headers[TOTAL_HEADER] = str(len(records))
        return [
            QualityRecordInfo(
                issue=record.issue.value,
                source=record.source,
                asset_id=record.asset_id,
                at=record.at,
                detail=record.detail,
            )
            for record in records[offset : offset + limit]
        ]

    return router
