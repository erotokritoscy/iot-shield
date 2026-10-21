"""The IT and OT overviews: counts over stored results, for the charts (ADR-0044).

The iotShield prototype's two dashboards, rebuilt over iotshield's own results — what this
system's detectors found, never another system's labels:

- **IT** — the flow lane: how many flows a detection covered, and detections per day.
- **OT** — SensiPro's six meter data-quality types (ADR-0043), which live in two places:
  ``meter_quality`` detections (heartbeat, high) and tagged meter rejects (valueError, zero,
  negative, small). ``quality_records`` gathers both into one list, which is what every OT
  chart counts from.

Counting happens here rather than in the browser because the list endpoints are capped at
10,000 rows and a week of SensiPro meters is 136,000 events; a chart drawn from a capped
list would be a chart of the cap. Like the other repository modules, every function takes
``org_id`` as a required keyword.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from iotshield.contracts import Lane
from iotshield.meter import QualityIssue, reject_issue, reject_subject
from iotshield.storage.orm import DetectionRow, EventRow, MalformedRow

METER_QUALITY = "meter_quality"
"""The detector that reports heartbeat and high (``detect/meter_quality.METHOD``). Spelled
here rather than imported: storage does not depend on the detectors, and a test pins the two."""

METER_FORMATS = ("sensipro-hourly-csv", "eoa-meter-csv")
"""The formats whose rejects can carry a quality tag (the meter normalizer's dialects)."""


@dataclass(frozen=True)
class FlowOverview:
    flows: int
    flagged: int
    """Flows inside the window of a flow-lane detection on the same device."""
    detections_per_day: dict[str, int]
    detections_per_method: dict[str, int]
    series: list[tuple[str, int, int, int]]
    """``(bucket, flows, flagged, detections)`` per day or hour that had any, oldest first — so
    the dashboard's replay can play the range forward (ADR-0044 amendment)."""


@dataclass(frozen=True)
class QualityRecord:
    """One of SensiPro's anomaly types, found by a detection or a tagged reject."""

    issue: QualityIssue
    source: Literal["detection", "reject"]
    asset_id: str | None
    at: datetime | None
    """When: a detection's window start, a reject's reading time. None for a reject whose row
    never yielded a meter and a time (an unparseable line)."""
    detail: str


BUCKET_FORMAT: dict[str, str] = {"day": "%Y-%m-%d", "hour": "%Y-%m-%dT%H:00:00+00:00"}
"""How a UTC instant is named as a bucket — the spelling both overview series use."""


def bucket_of(at: datetime, bucket: Literal["day", "hour"]) -> str:
    return (
        (at if at.tzinfo is not None else at.replace(tzinfo=UTC))
        .astimezone(UTC)
        .strftime(BUCKET_FORMAT[bucket])
    )


def flow_overview(
    session: Session,
    *,
    org_id: str,
    start: datetime,
    end: datetime,
    bucket: Literal["day", "hour"] = "day",
) -> FlowOverview:
    """The IT overview of ``[start, end)``."""
    in_range = and_(
        EventRow.org_id == org_id,
        EventRow.source_type == Lane.FLOW.value,
        EventRow.timestamp >= start,
        EventRow.timestamp < end,
    )
    covered = exists().where(
        DetectionRow.org_id == EventRow.org_id,
        DetectionRow.asset_id == EventRow.asset_id,
        DetectionRow.lane == Lane.FLOW.value,
        DetectionRow.window_start <= EventRow.timestamp,
        DetectionRow.window_end >= EventRow.timestamp,
    )
    flow_times = session.scalars(select(EventRow.timestamp).where(in_range)).all()
    flagged_times = session.scalars(select(EventRow.timestamp).where(in_range, covered)).all()
    flows, flagged = len(flow_times), len(flagged_times)
    detections = session.execute(
        select(DetectionRow.window_start, DetectionRow.method).where(
            DetectionRow.org_id == org_id,
            DetectionRow.lane == Lane.FLOW.value,
            DetectionRow.window_start >= start,
            DetectionRow.window_start < end,
        )
    ).all()
    return FlowOverview(
        flows=flows,
        flagged=flagged,
        detections_per_day=dict(
            sorted(Counter(f"{when:%Y-%m-%d}" for when, _ in detections).items())
        ),
        detections_per_method=dict(Counter(method for _, method in detections).most_common()),
        series=_flow_series(flow_times, flagged_times, [when for when, _ in detections], bucket),
    )


def _flow_series(
    flows: Sequence[datetime],
    flagged: Sequence[datetime],
    detections: Sequence[datetime],
    bucket: Literal["day", "hour"],
) -> list[tuple[str, int, int, int]]:
    by_flow = Counter(bucket_of(at, bucket) for at in flows)
    by_flagged = Counter(bucket_of(at, bucket) for at in flagged)
    by_detection = Counter(bucket_of(at, bucket) for at in detections)
    keys = sorted(set(by_flow) | set(by_detection))
    return [(key, by_flow[key], by_flagged[key], by_detection[key]) for key in keys]


def latest_day(session: Session, *, org_id: str, kind: Literal["it", "ot"]) -> str | None:
    """The most recent UTC day holding flow events (IT) or meter events (OT), if any.

    So a page can open on the data rather than on "this week", which for a historical
    export (SensiPro's is 2023) would be an empty chart and no hint where to look.
    """
    stmt = select(func.max(EventRow.timestamp)).where(EventRow.org_id == org_id)
    if kind == "it":
        stmt = stmt.where(EventRow.source_type == Lane.FLOW.value)
    else:
        stmt = stmt.where(EventRow.source_format.in_(METER_FORMATS))
    latest = session.scalar(stmt)
    if latest is None:
        return None
    if latest.tzinfo is None:  # SQLite returns an aggregate without the column's type
        latest = latest.replace(tzinfo=UTC)
    return f"{latest.astimezone(UTC):%Y-%m-%d}"


def quality_records(
    session: Session,
    *,
    org_id: str,
    start: datetime,
    end: datetime,
    asset_id: str | None = None,
) -> list[QualityRecord]:
    """Every SensiPro anomaly in ``[start, end)``, oldest first, unplaced rejects last.

    A reject is placed by the reading it names (``reject_subject``); one that names none is
    placed by when it was ingested, and kept with ``at=None`` so a count by type still
    includes it while a chart by day or by meter honestly cannot.
    """
    records: list[QualityRecord] = []
    detections = select(DetectionRow).where(
        DetectionRow.org_id == org_id,
        DetectionRow.method == METER_QUALITY,
        DetectionRow.window_start >= start,
        DetectionRow.window_start < end,
    )
    if asset_id is not None:
        detections = detections.where(DetectionRow.asset_id == asset_id)
    for row in session.scalars(detections):
        try:
            issue = QualityIssue(row.evidence.get("quality"))
        except ValueError:
            continue
        records.append(
            QualityRecord(
                issue=issue,
                source="detection",
                asset_id=row.asset_id,
                at=row.window_start,
                detail=str(row.evidence.get("explanation", "")),
            )
        )

    rejects = select(MalformedRow.error, MalformedRow.created_at).where(
        MalformedRow.org_id == org_id,
        MalformedRow.source_format.in_(METER_FORMATS),
        MalformedRow.error.like("[%"),
    )
    for error, created_at in session.execute(rejects):
        issue = reject_issue(error)
        if issue is None:
            continue
        subject = reject_subject(error)
        if subject is None:
            if asset_id is not None or not start <= created_at < end:
                continue
            records.append(QualityRecord(issue, "reject", None, None, error))
            continue
        meter, at = subject
        if not start <= at < end or (asset_id is not None and meter != asset_id):
            continue
        records.append(QualityRecord(issue, "reject", meter, at, error))

    return sorted(
        records, key=lambda record: (record.at is None, record.at or start, record.asset_id or "")
    )
