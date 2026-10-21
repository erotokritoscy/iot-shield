"""FlowNormalizer: dialects, orientation at the boundary, and what counts as a reject.

The port suite (tests/ports/test_normalizer_port.py) already pins the shared laws. These
are the flow-specific behaviours: that a dialect's columns land where the vocabulary says,
that orientation happens at ingest, and that the failures peculiar to flow data (foreign
traffic, a header that does not fit, portless protocols) are captured rather than dropped.
"""

from typing import Any

import pytest

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.flow import METADATA_KEY, METRIC, Direction, FlowRecord
from iotshield.normalize import FlowNormalizer, NormalizeResult
from iotshield.normalize.flow import DIALECTS

ORIGIN = "test://flows"

ZEEK_HEADER = (
    "#separator \\x09\n"
    "#fields\tts\tid.orig_h\tid.orig_p\tid.resp_h\tid.resp_p\tproto\tservice"
    "\tduration\torig_bytes\tresp_bytes\tconn_state\torig_pkts\tresp_pkts\n"
)
CSV_HEADER = (
    "start,src_ip,src_port,dst_ip,dst_port,proto,duration,"
    "src_bytes,dst_bytes,src_packets,dst_packets,state\n"
)


def zeek(*rows: str) -> str:
    return ZEEK_HEADER + "".join(row + "\n" for row in rows)


def csv(*rows: str) -> str:
    return CSV_HEADER + "".join(row + "\n" for row in rows)


def run(text: str, fmt: str = "flow-csv", **kwargs: Any) -> NormalizeResult:
    return FlowNormalizer(**kwargs).normalize(text, source_format=fmt, origin=ORIGIN)


def one_event(text: str, fmt: str = "flow-csv") -> NormalizedEvent:
    (event,) = run(text, fmt).events
    return event


class TestEventShape:
    ROW = "2026-07-07T12:00:00+00:00,10.0.0.7,51514,203.0.113.9,23,tcp,0.25,44,0,1,0,S0"

    def test_a_flow_becomes_one_event_not_one_per_metric(self) -> None:
        # The contract's fan-out rule is for telemetry channels; a flow's signal is the
        # tuple, which splitting into per-number events would destroy (ADR-0023 D1).
        result = run(csv(self.ROW))
        assert len(result.events) == 1

    def test_the_event_carries_lane_metric_and_the_asset_relative_byte_count(self) -> None:
        event = one_event(csv(self.ROW))
        assert event.source_type is Lane.FLOW
        assert event.source_format == "flow-csv"
        assert event.asset_id == "10.0.0.7"  # the local endpoint, not the peer
        assert event.metric == METRIC and event.value == 44.0

    def test_the_message_is_the_analyst_readable_summary(self) -> None:
        event = one_event(csv(self.ROW))
        assert event.message == "10.0.0.7:51514 -> 203.0.113.9:23 tcp S0 1/0 pkt 44/0 B"

    def test_the_typed_record_survives_into_metadata(self) -> None:
        # The whole point of the vocabulary module: what the normalizer wrote is exactly
        # what a detector reads back, checked rather than conventional.
        event = one_event(csv(self.ROW))
        record = FlowRecord.from_event(event)
        assert record is not None
        assert record.peer_ip == "203.0.113.9" and record.peer_port == 23
        assert record.direction is Direction.OUTBOUND
        assert record.duration_s == 0.25 and record.state == "S0"

    def test_raw_is_the_original_line(self) -> None:
        event = one_event(csv(self.ROW))
        assert event.raw == self.ROW
        assert METADATA_KEY in event.metadata


class TestZeekDialect:
    def test_epoch_timestamps_become_aware_utc(self) -> None:
        row = "1783425600.000000\t10.0.0.7\t51514\t203.0.113.9\t23\ttcp\t-\t-\t-\t-\tS0\t1\t0"
        event = one_event(zeek(row), "zeek-conn")
        assert event.timestamp.isoformat() == "2026-07-07T12:00:00+00:00"

    def test_unset_counters_become_zero_rather_than_a_reject(self) -> None:
        """A rejected connection legitimately has no byte counts. Quarantining those rows
        would throw away exactly the scan traffic the flow lane exists to catch."""
        row = "1783425600.000000\t10.0.0.7\t51514\t203.0.113.9\t23\ttcp\t-\t-\t-\t-\tS0\t1\t0"
        result = run(zeek(row), "zeek-conn")
        assert result.malformed == []
        record = FlowRecord.from_event(result.events[0])
        assert record is not None
        assert (record.bytes_out, record.bytes_in) == (0, 0)
        assert record.service is None

    def test_the_separator_directive_is_honoured(self) -> None:
        # A real conn.log announces its own delimiter; ignoring it would split every row
        # on the wrong character and reject the whole file.
        assert DIALECTS["zeek-conn"].delimiter == "\t"
        row = "1783166400.0\t10.0.0.7\t51514\t203.0.113.9\t23\ttcp\t-\t0\t0\t0\tS0\t1\t0"
        result = run(zeek(row), "zeek-conn")
        assert len(result.events) == 1

    def test_directive_lines_are_schema_not_data(self) -> None:
        # They are neither events nor rejects — the same category as a blank line.
        result = run(zeek(), "zeek-conn")
        assert result.events == [] and result.malformed == []


class TestCicflowmeterDialect:
    """The CSV cicflowmeter writes, as the Biology capture pipeline stores it (ADR-0042)."""

    HEADER = (
        "src_ip,dst_ip,src_port,dst_port,protocol,timestamp,flow_duration,flow_byts_s,"
        "tot_fwd_pkts,tot_bwd_pkts,totlen_fwd_pkts,totlen_bwd_pkts,pkt_len_max\n"
    )

    def event(self, row: str) -> NormalizedEvent:
        return one_event(self.HEADER + row + "\n", "cicflowmeter-csv")

    def test_a_bacnet_broadcast_becomes_a_udp_flow_event(self) -> None:
        event = self.event(
            "192.168.0.140,192.168.0.255,47808,47808,17,2023-07-22 11:45:00,1500.0,0.0,"
            "1.0,0.0,24.0,0.0,24.0"
        )
        record = FlowRecord.from_metadata(event.metadata[METADATA_KEY])
        assert event.asset_id == "192.168.0.140"
        assert record.protocol == "udp"  # IANA 17, named as every other dialect names it
        assert (record.peer_ip, record.peer_port) == ("192.168.0.255", 47808)
        assert record.bytes_out == 24 and record.packets_out == 1

    def test_naive_timestamps_are_utc_and_an_offset_is_honoured(self) -> None:
        naive = self.event("10.0.0.7,8.8.8.8,1,53,17,2023-07-22 11:45:00,0,0,1,1,10,20,10")
        assert naive.timestamp.isoformat() == "2023-07-22T11:45:00+00:00"
        offset = self.event("10.0.0.7,8.8.8.8,1,53,17,2023-07-22T14:45:00+03:00,0,0,1,1,10,20,10")
        assert offset.timestamp == naive.timestamp

    def test_the_duration_is_read_as_microseconds(self) -> None:
        event = self.event("10.0.0.7,8.8.8.8,1,53,6,2023-07-22 11:45:00,2500000.0,0,1,1,1,1,1")
        record = FlowRecord.from_metadata(event.metadata[METADATA_KEY])
        assert record.duration_s == pytest.approx(2.5)

    @pytest.mark.parametrize(("number", "name"), [("6", "tcp"), ("1", "icmp"), ("132", "132")])
    def test_protocol_numbers_are_named_and_an_unknown_one_passes_through(
        self, number: str, name: str
    ) -> None:
        event = self.event(f"10.0.0.7,8.8.8.8,1,53,{number},2023-07-22 11:45:00,0,0,1,1,1,1,1")
        assert FlowRecord.from_metadata(event.metadata[METADATA_KEY]).protocol == name

    def test_a_dhcp_request_from_0_0_0_0_is_quarantined_with_a_reason(self) -> None:
        # The Biology data really holds two of these: neither end is a monitored host.
        result = run(
            self.HEADER
            + "0.0.0.0,255.255.255.255,68,67,17,2023-06-20 07:35:00,0,0,1,0,300,0,300\n",
            "cicflowmeter-csv",
        )
        (rejected,) = result.malformed
        assert result.events == [] and "neither endpoint" in rejected.error


class TestOrientationAtTheBoundary:
    def test_an_inbound_flow_is_attributed_to_the_local_asset(self) -> None:
        row = "2026-07-07T12:00:00+00:00,203.0.113.5,40000,10.0.0.7,22,tcp,1.5,500,40,5,2,SF"
        event = one_event(csv(row))
        assert event.asset_id == "10.0.0.7"
        # value is bytes the ASSET sent (40), not what the exporter listed first (500).
        assert event.value == 40.0

    def test_traffic_between_two_foreign_hosts_is_rejected_with_a_reason(self) -> None:
        row = "2026-07-07T12:00:00+00:00,8.8.8.8,53,1.1.1.1,53,udp,0.01,10,10,1,1,SF"
        result = run(csv(row))
        assert result.events == []
        (reject,) = result.malformed
        assert "neither endpoint" in reject.error
        assert reject.raw == row and reject.origin == ORIGIN

    def test_home_networks_can_be_widened_for_a_publicly_addressed_fleet(self) -> None:
        row = "2026-07-07T12:00:00+00:00,8.8.8.8,53,203.0.113.9,443,tcp,0.01,10,20,1,1,SF"
        result = run(csv(row), home_networks=["203.0.113.0/24"])
        assert [e.asset_id for e in result.events] == ["203.0.113.9"]

    def test_home_networks_accepts_a_comma_separated_string(self) -> None:
        # How it arrives from a UI text box, as opposed to a YAML list.
        row = "2026-07-07T12:00:00+00:00,8.8.8.8,53,203.0.113.9,443,tcp,0.01,10,20,1,1,SF"
        result = run(csv(row), home_networks="203.0.113.0/24, 198.51.100.0/24")
        assert [e.asset_id for e in result.events] == ["203.0.113.9"]

    def test_a_bad_cidr_fails_at_construction_naming_itself(self) -> None:
        with pytest.raises(ValueError, match="invalid network"):
            FlowNormalizer(home_networks=["10.0.0.0/8", "garbage"])


class TestHeaderHandling:
    def test_a_header_missing_required_columns_is_the_malformed_record(self) -> None:
        """The single real problem is the header, so it is reported once — rather than as a
        confusing per-row message on every data line that follows."""
        text = "start,src_ip,proto\n2026-07-07T12:00:00+00:00,10.0.0.7,tcp\n"
        result = run(text)
        assert result.events == []
        assert "header is missing required column(s)" in result.malformed[0].error
        assert "dst_ip" in result.malformed[0].error

    def test_data_after_an_unusable_header_is_not_parsed_as_a_new_header(self) -> None:
        # The bug this guards: an unusable header used to leave the reader "not ready", so
        # the first data row was consumed as a replacement schema and vanished silently.
        text = "start,src_ip,proto\n2026-07-07T12:00:00+00:00,10.0.0.7,tcp\n"
        result = run(text)
        assert len(result.malformed) == 2  # the header, and the orphaned data row
        assert "no usable header" in result.malformed[1].error

    def test_unknown_columns_are_ignored_rather_than_fatal(self) -> None:
        # Real exporters carry extra columns; a dialect maps what it needs and skips the rest.
        text = (
            "start,src_ip,src_port,dst_ip,dst_port,proto,uid,vlan\n"
            "2026-07-07T12:00:00+00:00,10.0.0.7,51514,203.0.113.9,23,tcp,Cabc,7\n"
        )
        result = run(text)
        assert len(result.events) == 1 and result.malformed == []


class TestRejects:
    @pytest.mark.parametrize(
        "row,reason",
        [
            ("2026-07-07T12:00:00+00:00,10.0.0.7,x,203.0.113.9,23,tcp,0,1,0,1,0,S0", "port"),
            ("nope,10.0.0.7,51514,203.0.113.9,23,tcp,0,1,0,1,0,S0", "timestamp"),
            ("2026-07-07T12:00:00+00:00,,51514,203.0.113.9,23,tcp,0,1,0,1,0,S0", "src_ip"),
            ("2026-07-07T12:00:00+00:00,10.0.0.7,51514,203.0.113.9,23,tcp,x,1,0,1,0,S0", "durat"),
        ],
        ids=["bad-port", "bad-timestamp", "missing-src", "bad-duration"],
    )
    def test_each_failure_names_what_was_wrong(self, row: str, reason: str) -> None:
        result = run(csv(row))
        assert result.events == []
        assert reason in result.malformed[0].error

    def test_a_naive_timestamp_is_rejected_by_the_contract(self) -> None:
        row = "2026-07-07T12:00:00,10.0.0.7,51514,203.0.113.9,23,tcp,0,1,0,1,0,S0"
        result = run(csv(row))
        assert "timezone-aware" in result.malformed[0].error

    def test_an_unreadable_counter_is_quarantined_not_fatal(self) -> None:
        """A bad *record* is data, not an exception (the port's first law).

        float("1e400") is inf and int(inf) raises OverflowError, not ValueError — uncaught,
        one corrupt counter escaped normalize() and aborted the whole multi-source run.
        """
        row = "2026-07-07T12:00:00+00:00,10.0.0.7,1,203.0.113.9,23,tcp,0,1e400,0,1,0,S0"
        result = run(csv(row))
        assert result.events == []
        assert "not a usable number" in result.malformed[0].error

    def test_a_truncated_separator_directive_does_not_abort_the_run(self) -> None:
        # A hand-edited or torn conn.log: "\\" is an incomplete escape and raised
        # UnicodeDecodeError out of normalize(). The dialect's declared delimiter stands.
        text = "#separator \\" + chr(10) + ZEEK_HEADER.split(chr(10), 1)[1]
        row = "1783425600.0	10.0.0.7	1	203.0.113.9	23	tcp	-	0	0	0	S0	1	0"
        result = run(text + row + chr(10), "zeek-conn")
        assert len(result.events) == 1

    def test_an_unknown_dialect_is_a_config_error_not_a_data_error(self) -> None:
        with pytest.raises(ValueError, match="unsupported source_format"):
            FlowNormalizer().normalize("anything", source_format="netflow-v9", origin=ORIGIN)

    def test_a_non_finite_duration_is_rejected_before_it_reaches_metadata(self) -> None:
        # inf/nan pass float() and become invalid JSON in metadata, which Postgres JSONB
        # rejects at write time — so they are quarantined at the boundary instead.
        row = "2026-07-07T12:00:00+00:00,10.0.0.7,1,203.0.113.9,23,tcp,inf,1,0,1,0,S0"
        result = run(csv(row))
        assert result.events == []
        assert "not finite" in result.malformed[0].error

    def test_a_negative_counter_is_rejected(self) -> None:
        row = "2026-07-07T12:00:00+00:00,10.0.0.7,1,203.0.113.9,23,tcp,0,-5,0,1,0,S0"
        result = run(csv(row))
        assert result.events == [] and "negative" in result.malformed[0].error

    def test_an_out_of_range_port_is_rejected(self) -> None:
        row = "2026-07-07T12:00:00+00:00,10.0.0.7,99999,203.0.113.9,23,tcp,0,1,0,1,0,S0"
        result = run(csv(row))
        assert result.events == [] and "out of range" in result.malformed[0].error

    def test_a_truncated_row_is_rejected_not_zero_filled(self) -> None:
        # A torn write at log rotation: 8 of 12 columns. Previously the missing counters
        # were silently read as unset (0), producing a plausible but corrupt event.
        row = "2026-07-07T12:00:00+00:00,10.0.0.7,51514,203.0.113.9,23,tcp,0.5,999"
        result = run(csv(row))
        assert result.events == []
        assert "field(s)" in result.malformed[0].error

    def test_a_quoted_delimiter_does_not_shift_the_columns(self) -> None:
        # flow-csv is a real CSV: a field carrying a comma inside quotes is one field, not
        # two, and str.split would shatter it and misalign every column after it.
        text = (
            "start,src_ip,src_port,dst_ip,dst_port,proto,service\n"
            '2026-07-07T12:00:00+00:00,10.0.0.7,51514,203.0.113.9,23,tcp,"http,proxy"\n'
        )
        (event,) = run(text).events
        record = FlowRecord.from_event(event)
        assert record is not None
        assert record.peer_port == 23 and record.service == "http,proxy"

    def test_an_explicitly_empty_home_networks_is_a_config_error(self) -> None:
        # Distinct from unset: an empty range set rejects every flow, so it is almost
        # certainly a mistake rather than a request.
        with pytest.raises(ValueError, match="empty"):
            FlowNormalizer(home_networks=[])


class TestPortlessProtocols:
    def test_icmp_without_ports_still_produces_an_event(self) -> None:
        text = (
            "start,src_ip,dst_ip,proto,src_bytes,dst_bytes\n"
            "2026-07-07T12:00:00+00:00,10.0.0.7,203.0.113.9,icmp,64,0\n"
        )
        result = run(text)
        record = FlowRecord.from_event(result.events[0])
        assert record is not None
        assert record.peer_port is None and record.protocol == "icmp"
