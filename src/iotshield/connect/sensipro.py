"""The ``sensipro`` connector (OT): SensiPro's hourly meter rollup as ``sensipro-hourly-csv``.

Reads ``ascendinghourlyreading`` — one row per device-hour, ``last_value`` the hour-end
cumulative counter — joined to ``devices`` for the unit, which is the column the ``meter``
normalizer trusts for water vs electricity (``devices.unit``, not ``category_id``). The 305
million rows of the per-device ``dst_*_atomic`` tables iotShield charted are deliberately not
read: the meter detector judges hourly consumption, and the rollup is SensiPro's own.

A meter is identified by ``devices.destination_address`` (the address iotShield showed and its
atomic tables are named after), falling back to the numeric id where a device has none.

**The rollup never leaves an hour out** (ADR-0043 amendment). When no usable raw reading
arrived, SensiPro still writes the hour, repeating the previous counter — which a reader of
``last_value`` alone sees as an hour of zero consumption. The rollup says what really happened
in two columns, and this connector honours them rather than passing the filler on:

- ``count_accepted = 0`` and ``count_rejected = 0`` — **nothing arrived**. The hour is left
  out, so the normalizer sees a gap and ``meter_quality`` reports a **heartbeat**.
- ``count_accepted = 0`` and ``count_rejected > 0`` — readings arrived and SensiPro **rejected
  every one**. The counter is written as ``rejected-by-sensipro``, so the normalizer refuses the
  hour as a **valueError**, tagged with its meter and hour.
- anything else (including NULL counts) is passed on unchanged: an hour with some rejected and
  some accepted readings still has a real hour-end counter.

Measured on the real database, the 10 meters that fail from 2023-07-18 have 7,091 hours of the
first kind and 17,286 of the second.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, bindparam, text

from iotshield.connect.port import SLICE_DAYS, ConnectorSpec, Device, row, time_slices, window_days

UNIT: dict[str, str] = {"electricity": "kWh", "water": "m3"}

REJECTED = "rejected-by-sensipro"
"""Written in place of the counter for an hour whose every raw reading SensiPro rejected:
not a number, so the meter normalizer refuses it as a valueError and quotes this word."""


class SensiproParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    utility: Literal["electricity", "water"] = Field(
        default="electricity", description="Which meters: electricity (kWh) or water (m³)."
    )
    devices: list[str] = Field(
        default_factory=list,
        description="Meter addresses to pull; empty for every meter of that utility.",
        max_length=500,
    )


_METER = "COALESCE(d.destination_address, CAST(d.id AS VARCHAR(32)))"

METERS_PER_QUERY: Final = 100
"""How many meters one query reads over a window of up to ``SLICE_DAYS`` (ADR-0047). The stream
must be ordered by meter — the normalizer differences each meter's counter in order — and one
query sorted over a whole fleet and a quarter is millions of rows sorted before the first
arrives, against the connection's 30-second statement timeout. A group of meters is bounded by
construction (100 meters x 92 days x 24 hours), and the groups follow the fleet's own meter
order, so the whole stream is still ordered by meter.

A longer window cannot be cut by time without breaking that order (meter A's second quarter
would arrive after meter B's first), so it shrinks the group instead (``meters_per_query``,
ADR-0052): 1,220 days reads 7 meters a query, which is the same rows per query as 100 meters
over a quarter."""


def meters_per_query(days: int) -> int:
    """Meters one query reads over ``days`` days: ``METERS_PER_QUERY`` scaled to keep its rows."""
    return max(1, METERS_PER_QUERY * SLICE_DAYS // max(days, SLICE_DAYS))


def _device_filter(params: SensiproParams) -> tuple[str, dict[str, Any]]:
    if not params.devices:
        return "", {}
    return " AND d.destination_address IN :devices", {"devices": params.devices}


def _expand(sql: str, *names: str) -> Any:
    stmt = text(sql)
    for name in names:
        stmt = stmt.bindparams(bindparam(name, expanding=True))
    return stmt


def _meters(engine: Engine, params: SensiproParams) -> list[int]:
    """The device ids a pull reads, in meter order — the order the stream must follow."""
    where, binds = _device_filter(params)
    sql = f"SELECT d.id FROM devices d WHERE d.unit = :unit{where} ORDER BY {_METER}"
    stmt = _expand(sql, *binds)
    with engine.connect() as connection:
        rows = connection.execute(stmt, {"unit": UNIT[params.utility], **binds})
        return [device_id for (device_id,) in rows]


def _stream(
    engine: Engine, start: datetime, end: datetime, params: SensiproParams
) -> Iterator[tuple[str | None, str]]:
    ids = _meters(engine, params)
    sql = f"""
SELECT {_METER} AS meter, a.from_timestamp, a.last_value, d.unit,
       a.count_accepted, a.count_rejected
FROM ascendinghourlyreading a
JOIN devices d ON d.id = a.device_id
WHERE a.from_timestamp >= :start AND a.from_timestamp < :end AND a.device_id IN :ids
ORDER BY meter, a.from_timestamp
"""
    stmt = _expand(sql, "ids")
    size = meters_per_query(window_days(start, end))
    for offset in range(0, len(ids), size):
        group = ids[offset : offset + size]
        with engine.connect() as connection:
            result = connection.execution_options(stream_results=True, yield_per=5_000).execute(
                stmt, {"start": start, "end": end, "ids": group}
            )
            for meter, stamp, counter, unit, accepted, rejected in result:
                if accepted == 0 and rejected == 0:
                    continue  # nothing arrived: leave the hour out, so the silence is seen
                if accepted == 0 and rejected is not None and rejected > 0:
                    counter = REJECTED  # every reading was refused: the repeated value is filler
                yield meter, row((meter, stamp, counter, unit))


def _count(engine: Engine, start: datetime, end: datetime, params: SensiproParams) -> int:
    where, binds = _device_filter(params)
    sql = (
        "SELECT COUNT(*) FROM ascendinghourlyreading a JOIN devices d ON d.id = a.device_id "
        f"WHERE a.from_timestamp >= :start AND a.from_timestamp < :end AND d.unit = :unit{where}"
    )
    stmt = _expand(sql, *binds)
    total = 0
    with engine.connect() as connection:
        # One count per slice (ADR-0052), so counting a long window is as bounded as reading it.
        for first, after in time_slices(start, end):
            total += connection.execute(
                stmt, {"start": first, "end": after, "unit": UNIT[params.utility], **binds}
            ).scalar_one()
    return int(total)


def _devices(engine: Engine, params: SensiproParams) -> list[Device]:
    stmt = text(
        "SELECT destination_address, description FROM devices "
        "WHERE unit = :unit AND destination_address IS NOT NULL "
        "ORDER BY destination_address"
    )
    with engine.connect() as connection:
        return [
            Device(id=address, label=f"{address} — {description}" if description else address)
            for address, description in connection.execute(stmt, {"unit": UNIT[params.utility]})
        ]


SENSIPRO = ConnectorSpec(
    name="sensipro",
    description="SensiPro utility meters: hourly cumulative readings (OT).",
    normalizer="meter",
    source_format="sensipro-hourly-csv",
    header="device_id,from_timestamp,last_value,unit",
    params_type=SensiproParams,
    stream=_stream,
    count=_count,
    devices=_devices,
)
