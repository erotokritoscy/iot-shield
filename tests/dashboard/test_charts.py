"""Chart builder units: the timezone normalization that keeps the axis in UTC,
matching the tables beside it (golden rule 4)."""

from datetime import datetime, timedelta, timezone

from iotshield.dashboard.charts import _utc_wallclock


def test_utc_wallclock_drops_tz_after_normalizing_to_utc() -> None:
    athens = datetime(2026, 7, 7, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    wall = _utc_wallclock(athens)
    assert wall.tzinfo is None  # naive, so Vega renders it without a local shift
    assert wall == datetime(2026, 7, 7, 12, 0, 0)  # ... showing the UTC wall-clock time
