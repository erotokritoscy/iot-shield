"""Typed persistence functions — the only place the rest of the codebase touches SQL.

Callers pass an explicit Session and speak contract objects exclusively; ORM rows
never escape this module. Repository functions flush (making writes queryable
in-session) but never commit — transaction boundaries belong to the caller.

Time ranges are half-open (``start <= t < end``) with ONE deliberate exception:
``get_events``' upper bound is **closed**, because a point anomaly is a zero-length window
and evidence recovery queries (asset, window_start..window_end) — with ``< end`` such a
detection recovered no events at all. The rule is stated per function rather than once here,
because a blanket claim is what sent a reader to the wrong bound.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar, overload
from uuid import UUID

from sqlalchemy import Insert, Select, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload

from iotshield.contracts import (
    AlertStatus,
    Asset,
    DetectionResult,
    Lane,
    MalformedRecord,
    NormalizedEvent,
    PrioritizedAlert,
    discovered_asset,
)
from iotshield.storage.orm import (
    AlertDetectionRow,
    AlertRow,
    AssetRow,
    DetectionRow,
    EventRow,
    MalformedRow,
)

_Row = TypeVar("_Row")
_Item = TypeVar("_Item")


@dataclass(frozen=True)
class Page(Sequence[_Item]):
    """One page of results, and how many rows the filters matched in total.

    A list alone cannot say whether it is complete, which for a monitoring product is the
    worst kind of wrong answer: a client draws conclusions from a truncated queue with no
    way to know it was truncated. Carrying ``total`` beside the items makes that detectable
    at every layer without each one re-counting (ADR-0033).

    A ``Sequence`` rather than a plain record, because a page *is* its items for every
    existing reader — iteration, ``len``, indexing and unpacking all keep working — while
    ``.total`` is the one thing they could not previously ask.
    """

    items: list[_Item]
    total: int

    def __len__(self) -> int:
        return len(self.items)

    @overload
    def __getitem__(self, index: int) -> _Item: ...

    @overload
    def __getitem__(self, index: slice) -> list[_Item]: ...

    def __getitem__(self, index: int | slice) -> _Item | list[_Item]:
        return self.items[index]

    @property
    def truncated(self) -> bool:
        """Whether rows matched the filters that this page does not carry."""
        return len(self.items) < self.total


def _oldest_first(
    session: Session, stmt: Select[tuple[_Row]], *, limit: int, offset: int = 0
) -> list[_Row]:
    """One page of a DESC-ordered query, reversed to oldest-first.

    The read functions order DESC so the newest rows survive the cap (an ASC + LIMIT would
    hide recent data), then present them oldest-first for the timeline. That reverse lives
    here once, not in each caller.

    ``offset`` pages *backwards in time* as a consequence: page 0 is the newest ``limit``
    rows, page 1 the ``limit`` before those. Safe as keyset paging because every ordering
    here is **total** — each ends in a unique id — so no row can be skipped or repeated
    between pages, which an ordering with ties would silently do.
    """
    return list(session.scalars(stmt.limit(limit).offset(offset)))[::-1]


def _total(session: Session, stmt: Select[tuple[_Row]]) -> int:
    """How many rows the same filters match, ignoring limit/offset.

    Reported so a truncated answer is *detectable*. Without it a capped list is
    indistinguishable from a complete one — and because these queries order DESC and then
    reverse, the rows dropped are the OLDEST, so a client reading "here are my events from
    T" got a list whose front was silently wrong (ADR-0033).
    """
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def _ensure_assets(session: Session, asset_ids: set[str]) -> None:
    """Asset discovery (ADR-0005): stub inventory rows for ids seen in data.

    Only ever fills absence — a stub is inserted when no row exists; existing
    rows (registered or previously discovered) are never touched, so
    registration always beats discovery.

    Every write path calls this unconditionally: detections/alerts usually
    follow an event insert that already discovered their assets, but they can
    also arrive alone (replay, backfill, external detector), so the calls in
    insert_detections/insert_alerts are load-bearing, not redundant.
    """
    if not asset_ids:
        return
    existing = set(
        session.scalars(select(AssetRow.asset_id).where(AssetRow.asset_id.in_(asset_ids)))
    )
    missing = sorted(asset_ids - existing)
    if not missing:
        return
    # ON CONFLICT DO NOTHING, not a bare INSERT. The SELECT above is an optimisation, not a
    # guarantee: two runs can both read "absent" before either writes. ADR-0005 accepted that
    # race because the MVP runner was single-process — no longer true since ADR-0015, where
    # POST /runs is a sync endpoint dispatched on the threadpool, so concurrent runs share one
    # engine. And because PostgresSink wraps a whole run in one transaction, losing the race
    # did not lose an asset row, it rolled back every event, detection and alert of that run.
    # A double-click on the compose page's Run button is enough to trigger it.
    rows = [AssetRow.values_of(discovered_asset(asset_id)) for asset_id in missing]
    session.execute(_insert_ignoring_conflicts(session, rows))
    # No flush of the ORM unit of work is needed for these: the statement above writes them
    # directly, before the FK-dependent rows their callers add.


def _insert_ignoring_conflicts(session: Session, rows: list[dict[str, object]]) -> Insert:
    """An asset insert that yields to whoever won the race, per dialect.

    Postgres and SQLite spell this the same way but through different constructs, and
    neither is reachable from the generic ``sqlalchemy.insert``. Anything else (MySQL, a
    future backend) falls back to the plain insert: it keeps the race, which is what the
    code did everywhere before, rather than failing at import time on a dialect we do not
    ship.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert(AssetRow).values(rows).on_conflict_do_nothing(index_elements=["asset_id"])
    if dialect == "sqlite":
        return (
            sqlite_insert(AssetRow).values(rows).on_conflict_do_nothing(index_elements=["asset_id"])
        )
    return insert(AssetRow).values(rows)


def upsert_assets(session: Session, assets: Sequence[Asset]) -> None:
    """Registration: insert or update — human knowledge overwrites discovery."""
    for asset in assets:
        session.merge(AssetRow.from_contract(asset))
    session.flush()


def insert_events(session: Session, events: Sequence[NormalizedEvent]) -> None:
    _ensure_assets(session, {event.asset_id for event in events})
    session.add_all(EventRow.from_contract(event) for event in events)
    session.flush()


def insert_malformed(session: Session, records: Sequence[MalformedRecord]) -> None:
    session.add_all(MalformedRow.from_contract(record) for record in records)
    session.flush()


def insert_detections(session: Session, detections: Sequence[DetectionResult]) -> None:
    _ensure_assets(session, {detection.asset_id for detection in detections})
    session.add_all(DetectionRow.from_contract(detection) for detection in detections)
    session.flush()


def insert_alerts(session: Session, alerts: Sequence[PrioritizedAlert]) -> None:
    _ensure_assets(session, {alert.asset_id for alert in alerts})
    session.add_all(AlertRow.from_contract(alert) for alert in alerts)
    session.flush()


def list_assets(session: Session) -> list[Asset]:
    rows = session.scalars(select(AssetRow).order_by(AssetRow.asset_id))
    return [row.to_contract() for row in rows]


def get_events(
    session: Session,
    *,
    asset_id: str | None = None,
    lane: Lane | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> Page[NormalizedEvent]:
    stmt = select(EventRow).order_by(EventRow.timestamp.desc(), EventRow.event_id.desc())
    if asset_id is not None:
        stmt = stmt.where(EventRow.asset_id == asset_id)
    if lane is not None:
        stmt = stmt.where(EventRow.source_type == lane.value)
    if start is not None:
        stmt = stmt.where(EventRow.timestamp >= start)
    if end is not None:
        # Closed upper bound, not half-open: the contract recovers a detection's evidence by
        # querying (asset, window_start..window_end, lane), and a point anomaly is a
        # zero-length window (start == end). With `< end` such a detection recovered no
        # events at all (models.py DetectionResult).
        stmt = stmt.where(EventRow.timestamp <= end)
    rows = _oldest_first(session, stmt, limit=limit, offset=offset)
    return Page([row.to_contract() for row in rows], _total(session, stmt))


def list_malformed(
    session: Session, *, limit: int = 1000, offset: int = 0
) -> Page[MalformedRecord]:
    stmt = select(MalformedRow).order_by(
        MalformedRow.created_at.desc(), MalformedRow.record_id.desc()
    )
    rows = _oldest_first(session, stmt, limit=limit, offset=offset)
    return Page([row.to_contract() for row in rows], _total(session, stmt))


def get_detections(
    session: Session,
    *,
    asset_id: str | None = None,
    lane: Lane | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> Page[DetectionResult]:
    stmt = select(DetectionRow).order_by(
        DetectionRow.window_start.desc(), DetectionRow.detection_id.desc()
    )
    if asset_id is not None:
        stmt = stmt.where(DetectionRow.asset_id == asset_id)
    if lane is not None:
        stmt = stmt.where(DetectionRow.lane == lane.value)
    if start is not None:
        stmt = stmt.where(DetectionRow.window_start >= start)
    if end is not None:
        stmt = stmt.where(DetectionRow.window_start < end)
    rows = _oldest_first(session, stmt, limit=limit, offset=offset)
    return Page([row.to_contract() for row in rows], _total(session, stmt))


def get_alerts(
    session: Session,
    *,
    status: AlertStatus | None = None,
    detection_id: UUID | None = None,
    asset_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> Page[PrioritizedAlert]:
    """Alerts in triage order: priority descending (the ranked queue IS the product).

    ``asset_id`` and the time bounds mirror ``get_detections``: the ranked queue is the
    headline output, and "which alerts fired on this device last week" was unanswerable
    without pulling the whole queue and filtering client-side. Time is matched on
    ``window_start``, half-open, exactly as detections are.
    """
    stmt = (
        select(AlertRow)
        # Eager-load lineage in one batched query: to_contract() reads
        # detection_links, which would otherwise lazy-load per alert (N+1).
        .options(selectinload(AlertRow.detection_links))
        .order_by(AlertRow.priority_score.desc(), AlertRow.created_at, AlertRow.alert_id)
    )
    if status is not None:
        stmt = stmt.where(AlertRow.status == status.value)
    if asset_id is not None:
        stmt = stmt.where(AlertRow.asset_id == asset_id)
    if start is not None:
        stmt = stmt.where(AlertRow.window_start >= start)
    if end is not None:
        stmt = stmt.where(AlertRow.window_start < end)
    if detection_id is not None:
        # The junction's reverse direction: which alert covers this detection?
        stmt = stmt.join(AlertDetectionRow).where(AlertDetectionRow.detection_id == detection_id)
    # Not _oldest_first: the triage queue is ranked, not chronological, so its page order is
    # the ranking itself and reversing it would put the least urgent alert on top.
    rows = list(session.scalars(stmt.limit(limit).offset(offset)))
    return Page([row.to_contract() for row in rows], _total(session, stmt))


def get_alert_by_id(session: Session, alert_id: UUID) -> PrioritizedAlert | None:
    stmt = (
        select(AlertRow)
        .options(selectinload(AlertRow.detection_links))
        .where(AlertRow.alert_id == alert_id)
    )
    row = session.scalars(stmt).one_or_none()
    return row.to_contract() if row is not None else None


def get_detections_by_ids(session: Session, detection_ids: Sequence[UUID]) -> list[DetectionResult]:
    """Resolve an alert's lineage list into its detections (drill-down)."""
    if not detection_ids:
        return []
    stmt = (
        select(DetectionRow)
        .where(DetectionRow.detection_id.in_(detection_ids))
        .order_by(DetectionRow.window_start, DetectionRow.detection_id)
    )
    return [row.to_contract() for row in session.scalars(stmt)]


def count_summary(session: Session) -> dict[str, int]:
    """Per-table row counts (the /summary endpoint's KPI numbers).

    One round-trip via scalar subqueries: /summary is the dashboard's polled
    endpoint, so this is the one place recurring round-trips would add up.
    """
    tables = {
        "assets": AssetRow,
        "events": EventRow,
        "malformed_records": MalformedRow,
        "detections": DetectionRow,
        "alerts": AlertRow,
    }
    stmt = select(
        *(
            select(func.count()).select_from(row_type).scalar_subquery().label(name)
            for name, row_type in tables.items()
        )
    )
    counts = session.execute(stmt).one()
    return {name: int(count) for name, count in zip(tables, counts, strict=True)}
