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

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, bindparam, text

from iotshield.connect.port import ConnectorSpec, Device, row

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


_SELECT = """
SELECT COALESCE(d.destination_address, CAST(d.id AS VARCHAR(32))) AS meter,
       a.from_timestamp, a.last_value, d.unit, a.count_accepted, a.count_rejected
FROM ascendinghourlyreading a
JOIN devices d ON d.id = a.device_id
WHERE a.from_timestamp >= :start AND a.from_timestamp < :end AND d.unit = :unit
"""


def _fetch(
    engine: Engine, start: datetime, end: datetime, params: SensiproParams, limit: int
) -> tuple[list[str], int]:
    sql = _SELECT
    binds: dict[str, object] = {
        "start": start,
        "end": end,
        "unit": UNIT[params.utility],
        "limit": limit,
    }
    if params.devices:
        sql += " AND d.destination_address IN :devices"
        binds["devices"] = params.devices
    # Per meter, oldest first: the normalizer differences each meter's counter in order.
    sql += " ORDER BY meter, a.from_timestamp LIMIT :limit"
    stmt = text(sql)
    if params.devices:
        stmt = stmt.bindparams(bindparam("devices", expanding=True))
    lines: list[str] = []
    read = 0
    with engine.connect() as connection:
        for meter, stamp, counter, unit, accepted, rejected in connection.execute(stmt, binds):
            read += 1
            if accepted == 0 and rejected == 0:
                continue  # nothing arrived: leave the hour out, so the silence is seen
            if accepted == 0 and rejected is not None and rejected > 0:
                counter = REJECTED  # every reading was refused: the repeated value is filler
            lines.append(row((meter, stamp, counter, unit)))
    return lines, read


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
    fetch=_fetch,
    devices=_devices,
)
