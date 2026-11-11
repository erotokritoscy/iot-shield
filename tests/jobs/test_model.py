"""What a job's schedule and windows mean (ADR-0050): every time is UTC, and nothing is skipped
or doubled at a boundary."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from iotshield.jobs.model import FixedWindow, JobSource, RollingWindow, Schedule


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


class TestSchedule:
    @pytest.mark.parametrize(
        ("schedule", "now", "expected"),
        [
            (Schedule(every="hour", minute=15), "2026-11-09T10:00", "2026-11-09T10:15"),
            (Schedule(every="hour", minute=15), "2026-11-09T10:15", "2026-11-09T11:15"),
            (Schedule(every="hour", minute=15), "2026-11-09T23:30", "2026-11-10T00:15"),
            (Schedule(every="day", hour=2), "2026-11-09T01:59", "2026-11-09T02:00"),
            (Schedule(every="day", hour=2), "2026-11-09T02:00", "2026-11-10T02:00"),
            (Schedule(every="day", hour=2, minute=30), "2026-12-31T03:00", "2027-01-01T02:30"),
            # 2026-11-09 is a Monday.
            (Schedule(every="week", weekday=0, hour=6), "2026-11-09T05:00", "2026-11-09T06:00"),
            (Schedule(every="week", weekday=0, hour=6), "2026-11-09T06:00", "2026-11-16T06:00"),
            (Schedule(every="week", weekday=6, hour=6), "2026-11-09T07:00", "2026-11-15T06:00"),
        ],
    )
    def test_the_next_time_is_strictly_after_now(
        self, schedule: Schedule, now: str, expected: str
    ) -> None:
        assert schedule.next_after(at(now)) == at(expected)

    def test_a_moment_in_another_zone_is_read_as_the_same_instant(self) -> None:
        nicosia = timezone(timedelta(hours=2))
        local = datetime(2026, 11, 9, 3, 0, tzinfo=nicosia)  # 01:00 UTC
        assert Schedule(every="day", hour=2).next_after(local) == at("2026-11-09T02:00")

    def test_seconds_do_not_make_the_current_slot_fire_twice(self) -> None:
        moment = at("2026-11-09T02:00") + timedelta(seconds=20)
        assert Schedule(every="day", hour=2).next_after(moment) == at("2026-11-10T02:00")

    @pytest.mark.parametrize(
        ("schedule", "words"),
        [
            (Schedule(every="hour", minute=5), "every hour at :05"),
            (Schedule(every="day", hour=2, minute=30), "every day at 02:30 UTC"),
            (Schedule(every="week", weekday=4, hour=18), "every Friday at 18:00 UTC"),
        ],
    )
    def test_describes_itself(self, schedule: Schedule, words: str) -> None:
        assert schedule.describe() == words

    @pytest.mark.parametrize(
        "body", [{"every": "minute"}, {"every": "day", "hour": 24}, {"every": "week", "weekday": 7}]
    )
    def test_refuses_what_is_not_a_schedule(self, body: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            Schedule.model_validate(body)


class TestWindows:
    def test_a_fixed_window_is_the_same_days_every_time(self) -> None:
        window = FixedWindow(start=date(2026, 11, 1), end=date(2026, 11, 3))
        assert window.resolve(at("2027-01-01T00:00")) == (date(2026, 11, 1), date(2026, 11, 3))

    def test_a_rolling_window_ends_yesterday_by_default(self) -> None:
        window = RollingWindow(days=7)
        assert window.resolve(at("2026-11-09T00:30")) == (date(2026, 11, 2), date(2026, 11, 8))

    def test_a_rolling_window_may_include_today(self) -> None:
        window = RollingWindow(days=1, through="today")
        assert window.resolve(at("2026-11-09T23:59")) == (date(2026, 11, 9), date(2026, 11, 9))

    def test_the_day_is_the_utc_day(self) -> None:
        late_in_nicosia = datetime(2026, 11, 10, 1, 0, tzinfo=timezone(timedelta(hours=2)))
        assert RollingWindow(days=1).resolve(late_in_nicosia) == (
            date(2026, 11, 8),
            date(2026, 11, 8),
        )

    def test_a_rolling_window_of_years_is_accepted(self) -> None:
        # ADR-0052: no window limit; the connector reads it a quarter per query.
        window = RollingWindow(days=1220)
        start, end = window.resolve(datetime(2026, 11, 11, tzinfo=UTC))
        assert (end - start).days + 1 == 1220

    @pytest.mark.parametrize(
        "window",
        [
            {"kind": "rolling", "days": 0},
            {"kind": "fixed", "start": "2026-11-03", "end": "2026-11-01"},
            {"kind": "weekly"},
        ],
    )
    def test_refuses_a_window_no_pull_could_read(self, window: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            JobSource.model_validate(
                {"source_id": "00000000-0000-0000-0000-000000000001", "window": window}
            )
