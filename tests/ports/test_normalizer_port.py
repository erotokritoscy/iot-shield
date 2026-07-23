"""Normalizer PORT suite: every implementation must pass every test here.

To add an adapter: append a NormalizerCase with a fixture text whose good/bad
line counts you know. This suite is the swap-safety net (CLAUDE.md rule 2).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

import pytest

from iotshield.normalize import (
    STUB_SOURCE_FORMAT,
    SYSLOG_SOURCE_FORMAT,
    FlowNormalizer,
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

ZEEK_TEXT = """\
#separator \\x09
#path\tconn
#fields\tts\tuid\tid.orig_h\tid.orig_p\tid.resp_h\tid.resp_p\tproto\tservice\tduration\torig_bytes\tresp_bytes\tconn_state\torig_pkts\tresp_pkts
#types\ttime\tstring\taddr\tport\taddr\tport\tenum\tstring\tinterval\tcount\tcount\tstring\tcount\tcount
1783425600.000000\tCabc\t10.0.0.7\t51514\t203.0.113.9\t23\ttcp\t-\t-\t-\t-\tS0\t1\t0
1783425601.500000\tCdef\t10.0.0.7\t51515\t8.8.8.8\t53\tudp\tdns\t0.02\t64\t128\tSF\t1\t1
1783425602.000000\tCghi\t203.0.113.5\t40000\t10.0.0.7\t22\ttcp\tssh\t1.5\t500\t40\tSF\t5\t2

1783425603.000000\tCjkl\t198.51.100.1\t1234\t203.0.113.2\t80\ttcp\thttp\t1.0\t10\t20\tSF\t1\t1
not-a-timestamp\tCmno\t10.0.0.7\t9\t8.8.8.8\t9\tudp\t-\t-\t-\t-\tS0\t0\t0
"""
# 3 good (outbound scan attempt, outbound DNS, inbound ssh), 1 blank + 4 directives
# (schema, not data), 2 bad: a flow between two FOREIGN hosts (not our traffic) and an
# unparseable timestamp.

FLOW_CSV_TEXT = """\
start,src_ip,src_port,dst_ip,dst_port,proto,duration,src_bytes,dst_bytes,src_packets,dst_packets,state
2026-07-07T12:00:00+00:00,10.0.0.7,51514,203.0.113.9,23,tcp,0.0,44,0,1,0,S0
2026-07-07T12:00:01+02:00,10.0.0.8,51515,8.8.8.8,53,udp,0.02,64,128,1,1,SF

2026-07-07T12:00:02+00:00,8.8.8.8,53,1.1.1.1,53,udp,0.01,10,10,1,1,SF
2026-07-07T12:00:03,10.0.0.7,51516,203.0.113.9,23,tcp,0.0,44,0,1,0,S0
2026-07-07T12:00:04+00:00,10.0.0.7,notaport,203.0.113.9,23,tcp,0.0,44,0,1,0,S0
"""
# 2 good (one with a non-UTC offset), 1 header (schema), 1 blank, 3 bad: two foreign
# endpoints, a naive timestamp (the contract rejects it), and a non-numeric port.

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
    pytest.param(
        NormalizerCase(
            make=FlowNormalizer,
            source_format="zeek-conn",
            text=ZEEK_TEXT,
            expected_events=3,
            expected_malformed=2,
        ),
        id="flow-zeek-conn",
    ),
    pytest.param(
        NormalizerCase(
            make=FlowNormalizer,
            source_format="flow-csv",
            text=FLOW_CSV_TEXT,
            expected_events=2,
            expected_malformed=3,
        ),
        id="flow-csv",
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


def test_every_registered_normalizer_format_joins_this_suite() -> None:
    """Golden rule 2 made enforceable for normalizers: every format a registered normalizer
    declares it accepts must have a port case, or a new dialect ships with no coverage."""
    from iotshield.capabilities import formats_in
    from iotshield.pipeline.registry import NORMALIZERS, normalizer_capability

    declared = {
        fmt for name in NORMALIZERS for fmt in formats_in(normalizer_capability(name).consumes)
    }
    covered = {cast(NormalizerCase, param.values[0]).source_format for param in CASES}
    assert declared == covered, f"formats missing a port case: {declared - covered}"
