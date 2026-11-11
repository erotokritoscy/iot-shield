"""The ``biology`` connector (IT): the Biology capture's flows as ``cicflowmeter-csv``.

The Biology pipeline is tshark (10-minute pcaps) -> cicflowmeter (one CSV row per flow) ->
MySQL ``packets``. A second table, ``scans``, is the same rows scored by that project's own
classifier (``is_attack`` = score > 0.5) — another detector's *output*, not ground truth, so it
is deliberately not read: iotshield judges the flows itself.

Only the columns the ``flow`` normalizer maps are selected, of cicflowmeter's ~80; the header
uses cicflowmeter's own names, so a file cicflowmeter wrote and a pull from this table parse
identically.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, bindparam, text

from iotshield.connect.port import ConnectorSpec, Device, row, time_slices

COLUMNS: tuple[str, ...] = (
    "src_ip",
    "dst_ip",
    "src_port",
    "dst_port",
    "protocol",
    "timestamp",
    "flow_duration",
    "tot_fwd_pkts",
    "tot_bwd_pkts",
    "totlen_fwd_pkts",
    "totlen_bwd_pkts",
)


class BiologyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    devices: list[str] = Field(
        default_factory=list,
        description="Source hosts (IP addresses) to pull; empty for every host.",
        max_length=500,
    )


def _where(engine: Engine, params: BiologyParams) -> tuple[str, dict[str, Any]]:
    # `timestamp` is a keyword in some dialects; the engine's own quoting suits each one.
    ts = engine.dialect.identifier_preparer.quote("timestamp")
    sql = f" WHERE {ts} >= :start AND {ts} < :end"
    if params.devices:
        return sql + " AND src_ip IN :devices", {"devices": params.devices}
    return sql, {}


def _statement(sql: str, binds: dict[str, Any]) -> Any:
    stmt = text(sql)
    if "devices" in binds:
        stmt = stmt.bindparams(bindparam("devices", expanding=True))
    return stmt


def _stream(
    engine: Engine, start: datetime, end: datetime, params: BiologyParams
) -> Iterator[tuple[str | None, str]]:
    quote = engine.dialect.identifier_preparer.quote
    columns = ", ".join(quote(column) for column in COLUMNS)
    where, binds = _where(engine, params)
    sql = f"SELECT {columns} FROM packets{where} ORDER BY {quote('timestamp')}, id"
    stmt = _statement(sql, binds)
    # A long window is read a slice at a time (ADR-0052): the stream is ordered by time, so
    # consecutive slices in order are the same stream, and no query sorts more than a quarter.
    for first, after in time_slices(start, end):
        with engine.connect() as connection:
            result = connection.execution_options(stream_results=True, yield_per=5_000).execute(
                stmt, {"start": first, "end": after, **binds}
            )
            # A flow record stands alone (the flow normalizer is line-wise), so no group: a
            # chunk may end after any line.
            for record in result:
                yield None, row(record)


def _count(engine: Engine, start: datetime, end: datetime, params: BiologyParams) -> int:
    where, binds = _where(engine, params)
    stmt = _statement(f"SELECT COUNT(*) FROM packets{where}", binds)
    total = 0
    with engine.connect() as connection:
        for first, after in time_slices(start, end):
            total += connection.execute(stmt, {"start": first, "end": after, **binds}).scalar_one()
    return int(total)


def _devices(engine: Engine, _params: BiologyParams) -> list[Device]:
    stmt = text(
        "SELECT src_ip, COUNT(*) FROM packets WHERE src_ip IS NOT NULL "
        "GROUP BY src_ip ORDER BY src_ip"
    )
    with engine.connect() as connection:
        return [
            Device(id=address, label=f"{address} — {count:,} flows")
            for address, count in connection.execute(stmt)
        ]


BIOLOGY = ConnectorSpec(
    name="biology",
    description="Biology network capture: cicflowmeter flow records (IT).",
    normalizer="flow",
    source_format="cicflowmeter-csv",
    header=",".join(COLUMNS),
    params_type=BiologyParams,
    stream=_stream,
    count=_count,
    devices=_devices,
)
