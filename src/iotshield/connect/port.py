"""The Connector port: a time range of one known schema, as text a normalizer reads (ADR-0042).

A connector is three functions over an engine — ``stream`` the rows of a window as lines of
an existing format, ``count`` them, and optionally list the ``devices`` a pull can be narrowed
to — plus a declaration of that format and the pydantic model of its pull parameters. This
module owns everything they share: the window rules, cutting the stream into chunks a
normalizer can read on their own, and turning a database failure into a message that names the
connection and nothing about where it is.

**A pull is streamed, never held (ADR-0047).** It used to be fetched whole, sent to the browser
and posted back with the run, which is what the 100,000-row ceiling protected; now a run reads
the window itself, chunk by chunk (``pull_chunks``), and the browser is shown a count and a
sample (``pull_summary``). There is no row ceiling: memory is bounded by the run's batching,
not by refusing a large window. There is no limit on the window either (ADR-0053): a long one
is read as consecutive queries of at most ``SLICE_DAYS`` each (``time_slices``), so every query
stays the size the connection's 30-second statement timeout was measured against, however many
days the pull covers.

The window is **calendar days in the source's own clock** — ``start`` inclusive, ``end``
inclusive — because both shipped schemas store naive timestamps (SensiPro in Cyprus local
time, Biology in UTC) and the normalizer, not the connector, is what places them on the UTC
timeline. Converting here as well would apply the offset twice.
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import islice
from typing import Any, Final

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from iotshield.connect.connections import Connection, ConnectionUnavailable

log = logging.getLogger(__name__)

SLICE_DAYS: Final = 92
"""Most days one *query* covers (ADR-0053). A pull may ask for any number of days; a connector
reads a longer window as consecutive slices of this many (``time_slices``) — or, where its
output must stay ordered by something other than time, shrinks another dimension in proportion
(SensiPro's meters per query) — so no single query scans more than a quarter of a fleet's
history. It used to be the longest window a pull could ask for, which refused a year outright
to protect a query that slicing protects as well."""


class PullRefused(ValueError):
    """The pull as asked cannot be served: bad parameters, or more than the ceilings allow."""


@dataclass(frozen=True)
class Device:
    """One thing a pull can be narrowed to: the id that goes in the request, and a label."""

    id: str
    label: str


CHUNK_LINES: Final = 20_000
"""About how many lines one chunk of a streamed pull holds. A chunk is normalized on its own,
so it is never cut inside a group (see ``Stream``), which is why this is "about"."""

SAMPLE_LINES: Final = 200
"""How many data lines a pull summary carries: enough for the compose page to check the format
and show what arrived, never the window itself."""

Stream = Callable[[Engine, datetime, datetime, Any], Iterator[tuple[str | None, str]]]
"""``(engine, start, end_exclusive, params) -> (group, line), ...`` in the order to normalize.

``group`` names the run of lines a chunk must not be cut inside, because the normalizer reads
them together — a SensiPro meter, whose counter the meter normalizer differences row to row,
so a meter split across two chunks would lose the hour at the cut. ``None`` means a line stands
alone (a flow record) and a chunk may end after it. Lines of one group must be consecutive."""

Count = Callable[[Engine, datetime, datetime, Any], int]
"""``(engine, start, end_exclusive, params) -> rows`` in the window, before any are dropped."""


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    description: str
    normalizer: str
    source_format: str
    header: str
    params_type: type[BaseModel]
    stream: Stream
    count: Count
    devices: Callable[[Engine, Any], list[Device]] | None = None


@dataclass(frozen=True)
class Pulled:
    content: str
    normalizer: str
    source_format: str
    rows: int


@dataclass(frozen=True)
class PullSummary:
    """What a pull would read, without reading it: the row count and the first lines."""

    rows: int
    sample: str
    normalizer: str
    source_format: str


def params_for(spec: ConnectorSpec, raw: Mapping[str, Any]) -> BaseModel:
    try:
        return spec.params_type.model_validate(dict(raw))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'params'}: {error['msg']}"
            for error in exc.errors()
        )
        raise PullRefused(f"invalid pull parameters for {spec.name}: {problems}") from exc


def pull_window(start: date, end: date) -> tuple[datetime, datetime]:
    """``[start 00:00, end + 1 day 00:00)``, or ``PullRefused`` past the window rules."""
    return _window(start, end)


def _window(start: date, end: date) -> tuple[datetime, datetime]:
    if end < start:
        raise PullRefused("the end date is before the start date")
    return datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)


def time_slices(
    start: datetime, end: datetime, *, days: int | None = None
) -> list[tuple[datetime, datetime]]:
    """``[start, end)`` as consecutive half-open slices of at most ``days`` days, in order.

    Each boundary is the next slice's start, so a row stamped exactly on one is read once, and
    reading the slices in order yields the window in time order.
    """
    step = timedelta(days=days or SLICE_DAYS)
    slices: list[tuple[datetime, datetime]] = []
    at = start
    while at < end:
        slices.append((at, min(at + step, end)))
        at += step
    return slices


def day_slices(start: date, end: date, *, days: int | None = None) -> list[tuple[date, date]]:
    """``start``..``end`` (both inclusive) as consecutive inclusive spans of at most ``days``
    (``SLICE_DAYS`` unless given)."""
    span = timedelta(days=(days or SLICE_DAYS) - 1)
    slices: list[tuple[date, date]] = []
    at = start
    while at <= end:
        last = min(at + span, end)
        slices.append((at, last))
        at = last + timedelta(days=1)
    return slices


def window_days(start: datetime, end: datetime) -> int:
    """Whole days ``[start, end)`` touches — what a connector sizes its queries by."""
    return max(1, -((start - end) // timedelta(days=1)))


def _unavailable(connection: Connection, exc: Exception) -> ConnectionUnavailable:
    # The driver's message can carry the host, the user and the server's own diagnostics, so
    # it goes to the server log and the caller gets the connection's name alone.
    log.warning("pull through connection %r failed: %s", connection.name, exc)
    return ConnectionUnavailable(f"could not read from connection {connection.name!r}")


def check_pull(
    spec: ConnectorSpec, start: date, end: date, params: Mapping[str, Any]
) -> tuple[BaseModel, datetime, datetime]:
    """The pull's parsed params and window, or ``PullRefused`` — without touching the database.

    A run calls this for every pulled source before it reads anything, so a bad window on the
    third source is refused before the first is streamed.
    """
    return params_for(spec, params), *_window(start, end)


def pull_chunks(
    spec: ConnectorSpec,
    connection: Connection,
    start: date,
    end: date,
    params: Mapping[str, Any],
    *,
    chunk_lines: int = CHUNK_LINES,
) -> Iterator[str]:
    """Stream one window as texts its normalizer reads on their own: the header, then lines.

    Each chunk is cut only where the connector's ``group`` changes, so no group straddles two
    chunks. Lazy: nothing is queried until the first chunk is asked for, and a database failure
    mid-stream surfaces as ``ConnectionUnavailable`` like one at the start.
    """
    parsed, window_start, window_end = check_pull(spec, start, end, params)
    try:
        yield from chunked(
            spec.header,
            spec.stream(connection.engine(), window_start, window_end, parsed),
            chunk_lines=chunk_lines,
        )
    except SQLAlchemyError as exc:
        raise _unavailable(connection, exc) from exc


def render(header: str, lines: Sequence[str]) -> str:
    """A header (if any) and lines, as one text a normalizer reads."""
    body = "".join(line + "\n" for line in lines)
    return header + "\n" + body if header else body


def chunked(
    header: str, stream: Iterator[tuple[str | None, str]], *, chunk_lines: int = CHUNK_LINES
) -> Iterator[str]:
    """``stream`` cut into texts of about ``chunk_lines`` lines, never inside a group.

    Shared by every kind of pull (a database's connector, an HTTP API), so the chunking rule
    that keeps a meter's counter in one chunk is written once.
    """
    lines: list[str] = []
    last: str | None = None
    for group, line in stream:
        if len(lines) >= chunk_lines and (group is None or group != last):
            yield render(header, lines)
            lines = []
        lines.append(line)
        last = group
    if lines:
        yield render(header, lines)


def pull_summary(
    spec: ConnectorSpec,
    connection: Connection,
    start: date,
    end: date,
    params: Mapping[str, Any],
    *,
    sample_lines: int = SAMPLE_LINES,
) -> PullSummary:
    """How many rows the window holds, and its first lines — what the compose page shows.

    The sample is real connector output, so checking it with the normalizer checks the format
    the run will read; the count is of rows *read*, before a connector drops any (SensiPro's
    filler hours), so it can exceed the lines a run receives.
    """
    parsed, window_start, window_end = check_pull(spec, start, end, params)
    engine = connection.engine()
    try:
        rows = spec.count(engine, window_start, window_end, parsed)
        stream = spec.stream(engine, window_start, window_end, parsed)
        try:
            first = [line for _, line in islice(stream, sample_lines)]
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()  # ends the query rather than leaving it for the garbage collector
    except SQLAlchemyError as exc:
        raise _unavailable(connection, exc) from exc
    return PullSummary(
        rows=rows,
        sample=spec.header + "\n" + "".join(line + "\n" for line in first),
        normalizer=spec.normalizer,
        source_format=spec.source_format,
    )


def pull_count(
    spec: ConnectorSpec,
    connection: Connection,
    start: date,
    end: date,
    params: Mapping[str, Any],
) -> int:
    """How many rows the window holds — a run's reading total (ADR-0051), counted, not read.

    The same count ``pull_summary`` shows, so it is of rows *read*, before a connector drops
    any: a progress total, not a promise of how many lines the run receives.
    """
    parsed, window_start, window_end = check_pull(spec, start, end, params)
    try:
        return spec.count(connection.engine(), window_start, window_end, parsed)
    except SQLAlchemyError as exc:
        raise _unavailable(connection, exc) from exc


def pull(
    spec: ConnectorSpec,
    connection: Connection,
    start: date,
    end: date,
    params: Mapping[str, Any],
    *,
    max_chars: int | None = None,
) -> Pulled:
    """The whole window as one text, for a caller that needs it whole (tests, a script).

    A run never calls this — it streams ``pull_chunks`` — so there is no row ceiling here
    either; ``max_chars`` is for a caller that must bound what it holds, and refuses rather
    than truncates.
    """
    parsed, window_start, window_end = check_pull(spec, start, end, params)
    try:
        lines = [
            line for _, line in spec.stream(connection.engine(), window_start, window_end, parsed)
        ]
    except SQLAlchemyError as exc:
        raise _unavailable(connection, exc) from exc
    content = spec.header + "\n" + "".join(line + "\n" for line in lines)
    if max_chars is not None and len(content) > max_chars:
        raise PullRefused(
            f"{len(lines):,} rows make {len(content):,} characters, over the "
            f"{max_chars:,} one source may hold; pick fewer days or devices"
        )
    return Pulled(
        content=content,
        normalizer=spec.normalizer,
        source_format=spec.source_format,
        rows=len(lines),
    )


def list_devices(
    spec: ConnectorSpec, connection: Connection, params: Mapping[str, Any]
) -> list[Device]:
    if spec.devices is None:
        return []
    parsed = params_for(spec, params)
    try:
        return spec.devices(connection.engine(), parsed)
    except SQLAlchemyError as exc:
        raise _unavailable(connection, exc) from exc


def cell(value: object) -> str:
    """One value as a CSV cell: empty for NULL, a timestamp without its 'T', else str()."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def row(values: Sequence[object]) -> str:
    return ",".join(cell(value) for value in values)
