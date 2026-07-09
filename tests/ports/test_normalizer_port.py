"""Normalizer PORT suite: every implementation must pass every test here.

To add an adapter: append a NormalizerCase with a fixture text whose good/bad
line counts you know. This suite is the swap-safety net (CLAUDE.md rule 2).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest

from iotshield.normalize import (
    STUB_SOURCE_FORMAT,
    SYSLOG_SOURCE_FORMAT,
    Normalizer,
    StubCsvNormalizer,
    SyslogNormalizer,
)

ORIGIN = "test://fixture"


@dataclass(frozen=True)
class NormalizerCase:
    make: Callable[[], Normalizer]
    source_format: str
    text: str
    expected_events: int
    expected_malformed: int


STUB_TEXT = """\
2026-07-07T12:00:00+00:00,pump-02,Pressure,0.38
2026-07-07T12:00:01+00:00,pump-02,Pressure,0.41

2026-07-07T12:00:02+00:00,pump-02,Pressure,not-a-number
2026-07-07T12:00:03+00:00,pump-02,Pressure
2026-07-07T12:00:04,pump-02,Pressure,0.44
2026-07-07T12:00:05+02:00,gw-01,Temperature,45.2
"""
# 3 good lines (one with a non-UTC offset), 1 blank (skipped: no content is not
# data), 3 bad lines (non-numeric value, missing field, naive timestamp).

SYSLOG_TEXT = """\
<34>1 2026-07-07T12:00:00Z host-a su - ID1 - failed password for root
<13>1 2026-07-07T12:00:01+02:00 host-a cron - ID2 - disk usage high
Jul  7 12:00:02 host-b sshd[1234]: Accepted publickey for admin

<133>Jul  7 12:00:03 host-b kernel: out of memory: killed process
this is not a syslog line at all
<34>1 not-a-timestamp host-a su - ID3 - bad timestamp field
"""
# 4 good (2 RFC 5424 incl. a non-UTC offset, 2 RFC 3164 incl. a PRI variant),
# 1 blank (skipped), 2 bad (non-syslog line, RFC 5424 unparseable timestamp).

CASES = [
    pytest.param(
        NormalizerCase(
            make=StubCsvNormalizer,
            source_format=STUB_SOURCE_FORMAT,
            text=STUB_TEXT,
            expected_events=3,
            expected_malformed=3,
        ),
        id="stub-csv",
    ),
    pytest.param(
        NormalizerCase(
            make=SyslogNormalizer,
            source_format=SYSLOG_SOURCE_FORMAT,
            text=SYSLOG_TEXT,
            expected_events=4,
            expected_malformed=2,
        ),
        id="syslog",
    ),
]


@pytest.fixture(params=CASES)
def case(request: pytest.FixtureRequest) -> NormalizerCase:
    return request.param


def test_conservation_nothing_silently_dropped(case: NormalizerCase) -> None:
    result = case.make().normalize(case.text, source_format=case.source_format, origin=ORIGIN)
    assert len(result.events) == case.expected_events
    assert len(result.malformed) == case.expected_malformed


def test_all_timestamps_aware_utc(case: NormalizerCase) -> None:
    result = case.make().normalize(case.text, source_format=case.source_format, origin=ORIGIN)
    for event in result.events:
        assert event.timestamp.utcoffset() == timedelta(0)


def test_raw_preserved_verbatim(case: NormalizerCase) -> None:
    result = case.make().normalize(case.text, source_format=case.source_format, origin=ORIGIN)
    for event in result.events:
        assert event.raw and event.raw in case.text
    for record in result.malformed:
        assert record.raw in case.text


def test_malformed_records_carry_error_and_provenance(case: NormalizerCase) -> None:
    result = case.make().normalize(case.text, source_format=case.source_format, origin=ORIGIN)
    for record in result.malformed:
        assert record.error
        assert record.origin == ORIGIN
        assert record.source_format == case.source_format


def test_unsupported_source_format_raises(case: NormalizerCase) -> None:
    # Config errors fail loudly; they are not per-record data errors.
    with pytest.raises(ValueError, match="source_format"):
        case.make().normalize("anything", source_format="no-such-format-xyz", origin=ORIGIN)


def test_empty_text_yields_nothing(case: NormalizerCase) -> None:
    result = case.make().normalize("", source_format=case.source_format, origin=ORIGIN)
    assert result.events == []
    assert result.malformed == []
