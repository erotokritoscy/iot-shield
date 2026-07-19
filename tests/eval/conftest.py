"""Shared fixtures for the evaluation suite: one clock, one way to build an answer key."""

from datetime import UTC, datetime, timedelta

from iotshield.eval import GroundTruth

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def at(second: int) -> datetime:
    """A moment on the corpus clock, ``second`` seconds in."""
    return T0 + timedelta(seconds=second)


def truth_of(flags: list[bool]) -> GroundTruth:
    """An answer key from one-second-apart rows: ``[False, True, True]`` is one incident."""
    return GroundTruth.from_timeline([(at(index), flag) for index, flag in enumerate(flags)])
