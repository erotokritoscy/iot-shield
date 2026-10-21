"""The Connector port: a time range of one known schema, as text a normalizer reads (ADR-0042).

A connector is two functions over an engine — ``fetch`` the rows of a window as lines of an
existing format, and optionally list the ``devices`` a pull can be narrowed to — plus a
declaration of that format and the pydantic model of its pull parameters. ``pull`` owns
everything they share: the window rules, the row and size ceilings, and turning a database
failure into a message that names the connection and nothing about where it is.

The window is **calendar days in the source's own clock** — ``start`` inclusive, ``end``
inclusive — because both shipped schemas store naive timestamps (SensiPro in Cyprus local
time, Biology in UTC) and the normalizer, not the connector, is what places them on the UTC
timeline. Converting here as well would apply the offset twice.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from iotshield.connect.connections import Connection, ConnectionUnavailable

log = logging.getLogger(__name__)

MAX_PULL_ROWS: Final = 100_000
"""Most rows one pull may return. Above it the pull is refused with the count, so the caller
narrows the window or the devices rather than receiving a silently truncated recording."""

MAX_PULL_DAYS: Final = 92
"""Longest window one pull may ask for: a quarter, which bounds the query as well as its
result — a two-year scan of SensiPro's 5 million hourly rows is refused before it starts."""


class PullRefused(ValueError):
    """The pull as asked cannot be served: bad parameters, or more than the ceilings allow."""


@dataclass(frozen=True)
class Device:
    """One thing a pull can be narrowed to: the id that goes in the request, and a label."""

    id: str
    label: str


Fetch = Callable[[Engine, datetime, datetime, Any, int], tuple[list[str], int]]
"""``(engine, start, end_exclusive, params, limit) -> (data lines, rows read)``.

The two counts differ when a connector drops a row (SensiPro's filler hours), and the row
ceiling is judged on rows *read*: judged on lines, a truncated result whose dropped rows
brought it under the ceiling would be served as if it were whole."""


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    description: str
    normalizer: str
    source_format: str
    header: str
    params_type: type[BaseModel]
    fetch: Fetch
    devices: Callable[[Engine, Any], list[Device]] | None = None


@dataclass(frozen=True)
class Pulled:
    content: str
    normalizer: str
    source_format: str
    rows: int


def params_for(spec: ConnectorSpec, raw: Mapping[str, Any]) -> BaseModel:
    try:
        return spec.params_type.model_validate(dict(raw))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'params'}: {error['msg']}"
            for error in exc.errors()
        )
        raise PullRefused(f"invalid pull parameters for {spec.name}: {problems}") from exc


def _window(start: date, end: date) -> tuple[datetime, datetime]:
    if end < start:
        raise PullRefused("the end date is before the start date")
    days = (end - start).days + 1
    if days > MAX_PULL_DAYS:
        raise PullRefused(f"{days} days asked for; one pull covers at most {MAX_PULL_DAYS}")
    return datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)


def _unavailable(connection: Connection, exc: Exception) -> ConnectionUnavailable:
    # The driver's message can carry the host, the user and the server's own diagnostics, so
    # it goes to the server log and the caller gets the connection's name alone.
    log.warning("pull through connection %r failed: %s", connection.name, exc)
    return ConnectionUnavailable(f"could not read from connection {connection.name!r}")


def pull(
    spec: ConnectorSpec,
    connection: Connection,
    start: date,
    end: date,
    params: Mapping[str, Any],
    *,
    max_chars: int,
) -> Pulled:
    """Fetch one window and render it, or refuse saying why. Never truncates."""
    parsed = params_for(spec, params)
    window_start, window_end = _window(start, end)
    try:
        lines, read = spec.fetch(
            connection.engine(), window_start, window_end, parsed, MAX_PULL_ROWS + 1
        )
    except SQLAlchemyError as exc:
        raise _unavailable(connection, exc) from exc
    if read > MAX_PULL_ROWS:
        raise PullRefused(
            f"more than {MAX_PULL_ROWS:,} rows in this window; pick fewer days or devices"
        )
    content = spec.header + "\n" + "".join(line + "\n" for line in lines)
    if len(content) > max_chars:
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
