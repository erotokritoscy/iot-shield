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
    PASSTHROUGH_SOURCE_FORMAT,
    STUB_SOURCE_FORMAT,
    SYSLOG_SOURCE_FORMAT,
    FlowNormalizer,
    MeterNormalizer,
    Normalizer,
    PassThroughNormalizer,
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

CICFLOWMETER_TEXT = """\
src_ip,dst_ip,src_port,dst_port,protocol,timestamp,flow_duration,flow_byts_s,tot_fwd_pkts,tot_bwd_pkts,totlen_fwd_pkts,totlen_bwd_pkts
192.168.0.140,192.168.0.255,47808,47808,17,2023-07-22 11:45:00,1500.0,0.0,1.0,0.0,24.0,0.0
192.168.0.109,203.0.113.9,51514,443,6,2023-07-22 11:46:00,2000000.0,10.5,10.0,8.0,1200.0,900.0

203.0.113.1,198.51.100.2,1,2,6,2023-07-22 11:47:00,0.0,0.0,1.0,0.0,40.0,0.0
192.168.0.140,192.168.0.255,47808,47808,17,yesterday,0.0,0.0,1.0,0.0,24.0,0.0
"""
# 2 good (BACnet broadcast, outbound TCP), 1 header (schema), 1 blank, 2 bad: two foreign
# endpoints and an unparseable timestamp.


PASSTHROUGH_TEXT = """\
{"timestamp":"2026-07-07T12:00:00Z","asset_id":"pump-02","source_type":"telemetry","value":0.38}
{"timestamp":"2026-07-07T12:00:01+02:00","asset_id":"pump-02","value":0.41}

{"timestamp":"2026-07-07T12:00:02","asset_id":"pump-02","value":0.44}
{"timestamp":"2026-07-07T12:00:03Z","asset_id":"gw-01","source_type":"log","message":"wrong lane"}
this is not a json object at all
"""
# 2 good (line 1 explicit telemetry; line 2 omits source_type -> filled from the lane param,
# and carries a non-UTC offset), 1 blank (skipped), 3 bad (naive timestamp, a record on the
# wrong lane for this telemetry source, a non-JSON line). Provenance defaulting is pinned in
# tests/adapters/test_passthrough_normalizer.py, so these records omit source_format.

EOA_METER_TEXT = """\
params,id,updateAt,deviceId,flow,dataTime,signalCsq,isWaring,paValue
{},5,2026-07-07 09:00:00,M-1,10.400,2026-07-07 04:00:00,0,,
{},4,2026-07-07 09:00:00,M-1,10.100,2026-07-07 03:00:00,0,,
{},3,2026-07-07 09:00:00,M-1,10.500,2026-07-07 02:00:00,0,,

{},2,2026-07-07 09:00:00,M-1,10.000,2026-07-07 01:00:00,0,,
{},1,2026-07-07 09:00:00,M-1,9.000,2026-07-07 00:00:00,0,,
{},6,2026-07-07 09:00:00,M-1,not-a-number,2026-07-07 05:00:00,0,,
{},7,2026-07-07 09:00:00,M-1,12.000,not-a-timestamp,0,,
"""
# Rows arrive NEWEST-FIRST, as the real export writes them; sorting per meter is what makes
# that harmless. Five readable readings at 00:00..04:00 -> FOUR closed hourly intervals, of
# which the 02:00->03:00 one has a DECREASING counter (10.500 -> 10.100), a rollback whose
# consumption is undefined. So 3 events; 1 blank line and 1 header are schema, not data.
# 3 malformed: the rollback, a non-numeric counter, an unparseable timestamp.

SENSIPRO_METER_TEXT = """\
device_id,from_timestamp,to_timestamp,first_value,last_value,diff_last_first,unit
SP-1,2026-07-07 00:00:00,2026-07-07 00:59:59,100.000,100.500,0.500,m3
SP-1,2026-07-07 01:00:00,2026-07-07 01:59:59,100.500,101.250,0.750,m3
SP-1,2026-07-07 03:00:00,2026-07-07 03:59:59,101.250,102.000,0.750,m3

SP-2,2026-07-07 00:00:00,2026-07-07 00:59:59,8000.000,8055.000,55.000,kWh
SP-2,2026-07-07 01:00:00,2026-07-07 01:59:59,8055.000,8110.000,55.000,kWh
SP-3,2026-07-07 00:00:00,2026-07-07 00:59:59,5.000,6.000,1.000,V
"""
# SP-1: three readings but 02:00 is MISSING, so only 00:00->01:00 closes -> 1 event (the
# 01:00->03:00 pair is two hours apart: consumption across a gap is unknown, not zero).
# SP-2 (electricity): two readings -> 1 event, metric hourly_consumption_kwh. So 2 events.
# 1 malformed: SP-3 reports volts, which is not a consumption unit (the rule the upstream
# project reached after finding `devices.unit` authoritative and category_id unreliable).

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
    pytest.param(
        NormalizerCase(
            make=FlowNormalizer,
            source_format="cicflowmeter-csv",
            text=CICFLOWMETER_TEXT,
            expected_events=2,
            expected_malformed=2,
        ),
        id="flow-cicflowmeter-csv",
    ),
    pytest.param(
        NormalizerCase(
            make=PassThroughNormalizer,
            source_format=PASSTHROUGH_SOURCE_FORMAT,
            text=PASSTHROUGH_TEXT,
            expected_events=2,
            expected_malformed=3,
        ),
        id="passthrough-jsonl",
    ),
    pytest.param(
        NormalizerCase(
            make=MeterNormalizer,
            source_format="eoa-meter-csv",
            text=EOA_METER_TEXT,
            expected_events=3,
            expected_malformed=3,
        ),
        id="meter-eoa-csv",
    ),
    pytest.param(
        NormalizerCase(
            make=MeterNormalizer,
            source_format="sensipro-hourly-csv",
            text=SENSIPRO_METER_TEXT,
            expected_events=2,
            expected_malformed=1,
        ),
        id="meter-sensipro-csv",
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
