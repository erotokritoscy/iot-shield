"""FlowNormalizer: dialects, orientation at the boundary, and what counts as a reject.

The port suite (tests/ports/test_normalizer_port.py) already pins the shared laws. These
are the flow-specific behaviours: that a dialect's columns land where the vocabulary says,
that orientation happens at ingest, and that the failures peculiar to flow data (foreign
traffic, a header that does not fit, portless protocols) are captured rather than dropped.
"""

import pytest

from iotshield.contracts import Lane
from iotshield.flow import METADATA_KEY, METRIC, Direction, FlowRecord
from iotshield.normalize import FlowNormalizer
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


class TestEventShape:
    ROW = "2026-07-07T12:00:00+00:00,10.0.0.7,51514,203.0.113.9,23,tcp,0.25,44,0,1,0,S0"

    def test_a_flow_becomes_one_event_not_one_per_metric(self) -> None:
        # The contract's fan-out rule is for telemetry channels; a flow's signal is the
        # tuple, which splitting into per-number events would destroy (ADR-0023 D1).
        result = FlowNormalizer().normalize(csv(self.ROW), source_format="flow-csv", origin=ORIGIN)
        assert len(result.events) == 1

    def test_the_event_carries_lane_metric_and_the_asset_relative_byte_count(self) -> None:
        (event,) = (
            FlowNormalizer()
            .normalize(csv(self.ROW), source_format="flow-csv", origin=ORIGIN)
            .events
        )
        assert event.source_type is Lane.FLOW
        assert event.source_format == "flow-csv"
        assert event.asset_id == "10.0.0.7"  # the local endpoint, not the peer
        assert event.metric == METRIC and event.value == 44.0

    def test_the_message_is_the_analyst_readable_summary(self) -> None:
        (event,) = (
            FlowNormalizer()
            .normalize(csv(self.ROW), source_format="flow-csv", origin=ORIGIN)
            .events
        )
        assert event.message == "10.0.0.7:51514 -> 203.0.113.9:23 tcp S0 1/0 pkt 44/0 B"

    def test_the_typed_record_survives_into_metadata(self) -> None:
        # The whole point of the vocabulary module: what the normalizer wrote is exactly
        # what a detector reads back, checked rather than conventional.
        (event,) = (
            FlowNormalizer()
            .normalize(csv(self.ROW), source_format="flow-csv", origin=ORIGIN)
            .events
        )
        record = FlowRecord.from_event(event)
        assert record is not None
        assert record.peer_ip == "203.0.113.9" and record.peer_port == 23
        assert record.direction is Direction.OUTBOUND
        assert record.duration_s == 0.25 and record.state == "S0"

    def test_raw_is_the_original_line(self) -> None:
        (event,) = (
            FlowNormalizer()
            .normalize(csv(self.ROW), source_format="flow-csv", origin=ORIGIN)
            .events
        )
        assert event.raw == self.ROW
        assert METADATA_KEY in event.metadata


class TestZeekDialect:
    def test_epoch_timestamps_become_aware_utc(self) -> None:
        row = "1783425600.000000\t10.0.0.7\t51514\t203.0.113.9\t23\ttcp\t-\t-\t-\t-\tS0\t1\t0"
        (event,) = (
            FlowNormalizer().normalize(zeek(row), source_format="zeek-conn", origin=ORIGIN).events
        )
        assert event.timestamp.isoformat() == "2026-07-07T12:00:00+00:00"

    def test_unset_counters_become_zero_rather_than_a_reject(self) -> None:
        """A rejected connection legitimately has no byte counts. Quarantining those rows
        would throw away exactly the scan traffic the flow lane exists to catch."""
        row = "1783425600.000000\t10.0.0.7\t51514\t203.0.113.9\t23\ttcp\t-\t-\t-\t-\tS0\t1\t0"
        result = FlowNormalizer().normalize(zeek(row), source_format="zeek-conn", origin=ORIGIN)
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
        result = FlowNormalizer().normalize(zeek(row), source_format="zeek-conn", origin=ORIGIN)
        assert len(result.events) == 1

    def test_directive_lines_are_schema_not_data(self) -> None:
        # They are neither events nor rejects — the same category as a blank line.
        result = FlowNormalizer().normalize(zeek(), source_format="zeek-conn", origin=ORIGIN)
        assert result.events == [] and result.malformed == []


class TestOrientationAtTheBoundary:
    def test_an_inbound_flow_is_attributed_to_the_local_asset(self) -> None:
        row = "2026-07-07T12:00:00+00:00,203.0.113.5,40000,10.0.0.7,22,tcp,1.5,500,40,5,2,SF"
        (event,) = (
            FlowNormalizer().normalize(csv(row), source_format="flow-csv", origin=ORIGIN).events
        )
        assert event.asset_id == "10.0.0.7"
        # value is bytes the ASSET sent (40), not what the exporter listed first (500).
        assert event.value == 40.0

    def test_traffic_between_two_foreign_hosts_is_rejected_with_a_reason(self) -> None:
        row = "2026-07-07T12:00:00+00:00,8.8.8.8,53,1.1.1.1,53,udp,0.01,10,10,1,1,SF"
        result = FlowNormalizer().normalize(csv(row), source_format="flow-csv", origin=ORIGIN)
        assert result.events == []
        (reject,) = result.malformed
        assert "neither endpoint" in reject.error
        assert reject.raw == row and reject.origin == ORIGIN

    def test_home_networks_can_be_widened_for_a_publicly_addressed_fleet(self) -> None:
        row = "2026-07-07T12:00:00+00:00,8.8.8.8,53,203.0.113.9,443,tcp,0.01,10,20,1,1,SF"
        result = FlowNormalizer(home_networks=["203.0.113.0/24"]).normalize(
            csv(row), source_format="flow-csv", origin=ORIGIN
        )
        assert [e.asset_id for e in result.events] == ["203.0.113.9"]

    def test_home_networks_accepts_a_comma_separated_string(self) -> None:
        # How it arrives from a UI text box, as opposed to a YAML list.
        row = "2026-07-07T12:00:00+00:00,8.8.8.8,53,203.0.113.9,443,tcp,0.01,10,20,1,1,SF"
        result = FlowNormalizer(home_networks="203.0.113.0/24, 198.51.100.0/24").normalize(
            csv(row), source_format="flow-csv", origin=ORIGIN
        )
        assert [e.asset_id for e in result.events] == ["203.0.113.9"]

    def test_a_bad_cidr_fails_at_construction_naming_itself(self) -> None:
        with pytest.raises(ValueError, match="invalid network"):
            FlowNormalizer(home_networks=["10.0.0.0/8", "garbage"])


class TestHeaderHandling:
    def test_a_header_missing_required_columns_is_the_malformed_record(self) -> None:
        """The single real problem is the header, so it is reported once — rather than as a
        confusing per-row message on every data line that follows."""
        text = "start,src_ip,proto\n2026-07-07T12:00:00+00:00,10.0.0.7,tcp\n"
        result = FlowNormalizer().normalize(text, source_format="flow-csv", origin=ORIGIN)
        assert result.events == []
        assert "header is missing required column(s)" in result.malformed[0].error
        assert "dst_ip" in result.malformed[0].error

    def test_data_after_an_unusable_header_is_not_parsed_as_a_new_header(self) -> None:
        # The bug this guards: an unusable header used to leave the reader "not ready", so
        # the first data row was consumed as a replacement schema and vanished silently.
        text = "start,src_ip,proto\n2026-07-07T12:00:00+00:00,10.0.0.7,tcp\n"
        result = FlowNormalizer().normalize(text, source_format="flow-csv", origin=ORIGIN)
        assert len(result.malformed) == 2  # the header, and the orphaned data row
        assert "no usable header" in result.malformed[1].error

    def test_unknown_columns_are_ignored_rather_than_fatal(self) -> None:
        # Real exporters carry extra columns; a dialect maps what it needs and skips the rest.
        text = (
            "start,src_ip,src_port,dst_ip,dst_port,proto,uid,vlan\n"
            "2026-07-07T12:00:00+00:00,10.0.0.7,51514,203.0.113.9,23,tcp,Cabc,7\n"
        )
        result = FlowNormalizer().normalize(text, source_format="flow-csv", origin=ORIGIN)
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
        result = FlowNormalizer().normalize(csv(row), source_format="flow-csv", origin=ORIGIN)
        assert result.events == []
        assert reason in result.malformed[0].error

    def test_a_naive_timestamp_is_rejected_by_the_contract(self) -> None:
        row = "2026-07-07T12:00:00,10.0.0.7,51514,203.0.113.9,23,tcp,0,1,0,1,0,S0"
        result = FlowNormalizer().normalize(csv(row), source_format="flow-csv", origin=ORIGIN)
        assert "timezone-aware" in result.malformed[0].error

    def test_an_unknown_dialect_is_a_config_error_not_a_data_error(self) -> None:
        with pytest.raises(ValueError, match="unsupported source_format"):
            FlowNormalizer().normalize("anything", source_format="netflow-v9", origin=ORIGIN)


class TestPortlessProtocols:
    def test_icmp_without_ports_still_produces_an_event(self) -> None:
        text = (
            "start,src_ip,dst_ip,proto,src_bytes,dst_bytes\n"
            "2026-07-07T12:00:00+00:00,10.0.0.7,203.0.113.9,icmp,64,0\n"
        )
        result = FlowNormalizer().normalize(text, source_format="flow-csv", origin=ORIGIN)
        record = FlowRecord.from_event(result.events[0])
        assert record is not None
        assert record.peer_port is None and record.protocol == "icmp"
