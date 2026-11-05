"""A per-hour rate as a cumulative counter, in the wire format the meter normalizer reads.

WBN's SCADA and the LEAD dataset report **how much per hour**; every meter dialect we read
reports **a running total**, and the normalizer differences it (ADR-0030). A second, rate-reading
path through the normalizer would duplicate its gap, duplicate and utility handling to serve a
difference of one line of arithmetic, so a rate source is converted instead (ADR-0048 D1).

**What a gap costs.** Upstream's adapters write ``cumsum(consumption)`` on the hourly grid, and by
their own note a difference cannot then recover the hour immediately after a hole, because the row
it would subtract is the missing one. Here a contiguous run opens with its own row one hour earlier,
stamped inside the gap and carrying the running total so far. It closes no interval, so it claims no
consumption for that hour; it only gives the first real hour something to difference against. That
is the opener ``SensiproHourlyRenderer`` writes, and it recovers the reading after any gap of two
hours or more, with the gap still an absence, which is what ``meter_quality``'s heartbeat reads.

**A gap of exactly one hour cannot be expressed.** The opener would sit one hour after the previous
reading, so the normalizer would pair the two and emit a zero-consumption event for the hour nobody
observed: a fabricated reading that a stuck check would then act on. ``representable`` therefore
drops the reading after a one-hour gap, widening the gap to two, and the caller counts what it
dropped. A lost reading is visible; an invented zero is not.

The counter's absolute value is derived, not measured, and says nothing beyond its differences. It
is rounded row by row so a zero hour is two identical strings, and so an exact zero, which is what a
stuck check compares against.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from iotshield.normalize.meter import SENSIPRO_HOURLY_CSV

HOUR = timedelta(hours=1)
_ONE_HOUR_GAP = 2 * HOUR
_DECIMALS = 6

HEADER = ",".join(
    (
        SENSIPRO_HOURLY_CSV.timestamp_column,
        SENSIPRO_HOURLY_CSV.counter_column,
        SENSIPRO_HOURLY_CSV.meter_id_column,
        str(SENSIPRO_HOURLY_CSV.unit_column),
    )
)
SOURCE_FORMAT = SENSIPRO_HOURLY_CSV.name


def representable(
    readings: Sequence[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """The readings a counter can carry: all but the one after each one-hour gap.

    Compared with the last reading *kept*, since dropping one can open the next gap wider.
    """
    kept: list[tuple[datetime, float]] = []
    for reading in readings:
        if kept and reading[0] - kept[-1][0] == _ONE_HOUR_GAP:
            continue
        kept.append(reading)
    return kept


def counter_rows(
    readings: Sequence[tuple[datetime, float]], *, meter_id: str, unit: str
) -> list[str]:
    """One meter's time-ordered ``(hour, rate)`` pairs as counter rows, with no header.

    Timestamps must be timezone-aware: they are written with their offset, which the normalizer
    honours as written, so no dialect timezone can move them. A negative rate has no counter to
    become (the normalizer would read it as a rollback) and is the caller's to filter and count,
    as is a reading ``representable`` would drop.
    """
    lines: list[str] = []
    total = 0.0
    previous: datetime | None = None
    for stamp, rate in readings:
        if stamp.tzinfo is None:
            raise ValueError(f"naive timestamp for meter {meter_id!r}: {stamp.isoformat()}")
        if rate < 0:
            raise ValueError(f"negative rate for meter {meter_id!r} at {stamp.isoformat()}")
        if previous is not None and stamp <= previous:
            raise ValueError(f"readings for meter {meter_id!r} are not strictly time-ordered")
        if previous is not None and stamp - previous == _ONE_HOUR_GAP:
            raise ValueError(
                f"one-hour gap for meter {meter_id!r} before {stamp.isoformat()}: an opener there "
                "would read as a zero-consumption hour; pass the readings through representable()"
            )
        if previous is None or stamp - previous != HOUR:
            lines.append(f"{(stamp - HOUR).isoformat()},{total:.{_DECIMALS}f},{meter_id},{unit}")
        total = round(total + rate, _DECIMALS)
        lines.append(f"{stamp.isoformat()},{total:.{_DECIMALS}f},{meter_id},{unit}")
        previous = stamp
    return lines
