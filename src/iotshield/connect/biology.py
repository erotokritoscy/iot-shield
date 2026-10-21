"""The ``biology`` connector (IT): the Biology capture's flows as ``cicflowmeter-csv``.

The Biology pipeline is tshark (10-minute pcaps) -> cicflowmeter (one CSV row per flow) ->
MySQL ``packets``. A second table, ``scans``, is the same rows scored by that project's own
classifier (``is_attack`` = score > 0.5) — another detector's *output*, not ground truth, so it
is deliberately not read: iotshield judges the flows itself.

Only the columns the ``flow`` normalizer maps are selected, of cicflowmeter's ~80; the header
uses cicflowmeter's own names, so a file cicflowmeter wrote and a pull from this table parse
identically.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, bindparam, text

from iotshield.connect.port import ConnectorSpec, Device, row

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


def _fetch(
    engine: Engine, start: datetime, end: datetime, params: BiologyParams, limit: int
) -> tuple[list[str], int]:
    # `timestamp` is a keyword in some dialects; the engine's own quoting suits each one.
    quote = engine.dialect.identifier_preparer.quote
    columns = ", ".join(quote(column) for column in COLUMNS)
    ts = quote("timestamp")
    sql = f"SELECT {columns} FROM packets WHERE {ts} >= :start AND {ts} < :end"
    binds: dict[str, object] = {"start": start, "end": end, "limit": limit}
    if params.devices:
        sql += " AND src_ip IN :devices"
        binds["devices"] = params.devices
    sql += f" ORDER BY {ts}, id LIMIT :limit"
    stmt = text(sql)
    if params.devices:
        stmt = stmt.bindparams(bindparam("devices", expanding=True))
    with engine.connect() as connection:
        lines = [row(record) for record in connection.execute(stmt, binds)]
    return lines, len(lines)


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
    fetch=_fetch,
    devices=_devices,
)
