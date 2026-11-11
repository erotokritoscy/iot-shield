"""What a saved job is (ADR-0050): a chain, a window rule per pull, and an optional schedule.

A job is a run that can be asked for again. Its inputs must all be **pulls**, since an upload is
not kept (ADR-0046) and so there would be nothing to read next time. A pull's window is either
the **fixed** days it was composed with or a **rolling** "last N days", resolved in UTC each time
the job runs, which is what lets a scheduled job read new data rather than the same days
forever.

Schedules are three shapes — hourly at a minute, daily at a time, weekly on a day at a time —
all in **UTC** (rule 4). A cron expression was not offered: these cover every request so far,
need no library, and cannot be mistyped into "every minute".
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from iotshield.connect import MAX_PULL_DAYS

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class FixedWindow(BaseModel):
    """The same days every time: a re-run of exactly what was composed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fixed"] = "fixed"
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> "FixedWindow":
        if self.end < self.start:
            raise ValueError("the window's end is before its start")
        return self

    def resolve(self, now: datetime) -> tuple[date, date]:
        return self.start, self.end


class RollingWindow(BaseModel):
    """The last ``days`` days, ending yesterday or today in UTC, worked out at each run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["rolling"] = "rolling"
    days: int = Field(ge=1, le=MAX_PULL_DAYS, description="How many days the window covers")
    through: Literal["yesterday", "today"] = Field(
        default="yesterday",
        description="Its last day: yesterday (complete days only) or today (so far)",
    )

    def resolve(self, now: datetime) -> tuple[date, date]:
        today = now.astimezone(UTC).date()
        end = today if self.through == "today" else today - timedelta(days=1)
        return end - timedelta(days=self.days - 1), end


Window = FixedWindow | RollingWindow


class JobSource(BaseModel):
    """One input of a job, aligned to ``spec.sources`` by position: a data source and its window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: UUID
    params: dict[str, Any] = Field(default_factory=dict, description="The connector's pull params")
    window: Window = Field(discriminator="kind")


class Schedule(BaseModel):
    """When a job runs by itself, in UTC. ``hour`` is ignored hourly, ``weekday`` unless weekly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    every: Literal["hour", "day", "week"]
    minute: int = Field(default=0, ge=0, le=59)
    hour: int = Field(default=0, ge=0, le=23)
    weekday: int = Field(default=0, ge=0, le=6, description="0 = Monday … 6 = Sunday")

    def next_after(self, moment: datetime) -> datetime:
        """The first time this schedule fires strictly after ``moment``, in UTC."""
        now = moment.astimezone(UTC).replace(second=0, microsecond=0)
        if self.every == "hour":
            candidate = now.replace(minute=self.minute)
            step = timedelta(hours=1)
        else:
            candidate = now.replace(hour=self.hour, minute=self.minute)
            if self.every == "week":
                candidate += timedelta(days=(self.weekday - candidate.weekday()) % 7)
            step = timedelta(days=7 if self.every == "week" else 1)
        while candidate <= moment:
            candidate += step
        return candidate

    def describe(self) -> str:
        at = f"{self.hour:02d}:{self.minute:02d} UTC"
        if self.every == "hour":
            return f"every hour at :{self.minute:02d}"
        if self.every == "day":
            return f"every day at {at}"
        return f"every {WEEKDAYS[self.weekday]} at {at}"
