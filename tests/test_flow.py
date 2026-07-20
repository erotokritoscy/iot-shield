"""The FLOW lane vocabulary: orientation, the metadata round-trip, and the untyped hop.

The round-trip tests matter more than they look: ``to_metadata``/``from_metadata`` are the
one place a flow crosses an unchecked ``dict[str, Any]``, so a field that stops surviving
the trip is exactly the silent-emptiness bug this module exists to make impossible.
"""

from dataclasses import fields
from datetime import UTC, datetime

import pytest

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.flow import (
    DEFAULT_NETWORKS,
    METADATA_KEY,
    Direction,
    FlowFieldError,
    FlowOrientationError,
    FlowRecord,
    Locality,
    is_local,
    orient,
    parse_networks,
)

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def flow_event(record: FlowRecord, *, metadata_override: object | None = None) -> NormalizedEvent:
    metadata = {
        METADATA_KEY: record.to_metadata() if metadata_override is None else metadata_override
    }
    return NormalizedEvent(
        timestamp=T0,
        asset_id=record.asset_ip,
        source_type=Lane.FLOW,
        source_format="test-flow",
        raw="test",
        metadata=metadata,
    )


class TestNetworks:
    def test_default_networks_cover_rfc1918_and_link_local(self) -> None:
        for address in ("10.0.0.7", "172.16.4.1", "192.168.1.50", "169.254.1.1"):
            assert is_local(address, DEFAULT_NETWORKS)

    def test_public_addresses_are_not_local(self) -> None:
        for address in ("8.8.8.8", "203.0.113.9", "1.1.1.1"):
            assert not is_local(address, DEFAULT_NETWORKS)

    def test_ipv6_unique_local_and_link_local_are_local(self) -> None:
        assert is_local("fd00::1", DEFAULT_NETWORKS)
        assert is_local("fe80::1", DEFAULT_NETWORKS)
        assert not is_local("2001:4860:4860::8888", DEFAULT_NETWORKS)

    def test_a_v4_address_is_not_inside_a_v6_network(self) -> None:
        # ipaddress raises on a cross-version containment test; is_local must not.
        assert not is_local("10.0.0.7", parse_networks(["fc00::/7"]))

    def test_unparseable_address_is_not_local_rather_than_raising(self) -> None:
        assert not is_local("not-an-ip", DEFAULT_NETWORKS)
        assert not is_local("", DEFAULT_NETWORKS)

    def test_host_bits_are_tolerated(self) -> None:
        # strict=False: an operator hand-writing a CIDR should not have to zero host bits.
        assert parse_networks(["10.1.2.3/8"]) == parse_networks(["10.0.0.0/8"])

    def test_a_bad_cidr_names_itself(self) -> None:
        with pytest.raises(ValueError, match="invalid network 'nope/8'"):
            parse_networks(["10.0.0.0/8", "nope/8"])


class TestLocality:
    """The memo in front of is_local — two calls per flow row make it the hot path."""

    def test_it_answers_the_same_as_the_uncached_test(self) -> None:
        local = Locality()
        for address in ("10.0.0.7", "8.8.8.8", "fd00::1", "not-an-ip", ""):
            assert local(address) == is_local(address, DEFAULT_NETWORKS)

    def test_a_repeated_address_is_answered_from_the_memo(self) -> None:
        local = Locality()
        assert local("10.0.0.7") is True
        assert local("10.0.0.7") is True  # second call must not disagree with the first

    def test_a_cached_false_is_not_mistaken_for_a_cache_miss(self) -> None:
        # The memo stores bools, so `is None` is the only test that distinguishes
        # "not cached" from "cached as False" — `if not cached` would re-compute forever.
        local = Locality()
        assert local("8.8.8.8") is False
        assert local("8.8.8.8") is False

    def test_the_memo_is_bounded(self) -> None:
        # A capture can carry millions of distinct peers; an unbounded memo would trade
        # CPU for a memory leak.
        local = Locality()
        for octet in range(Locality._CAP + 50):
            local(f"203.0.113.{octet % 256}.{octet}")
        assert len(local._seen) <= Locality._CAP


class TestOrientation:
    def test_local_source_to_public_peer_is_outbound(self) -> None:
        record = orient(
            src_ip="10.0.0.7",
            src_port=51514,
            dst_ip="8.8.8.8",
            dst_port=53,
            protocol="udp",
            src_bytes=64,
            dst_bytes=128,
            src_packets=1,
            dst_packets=1,
        )
        assert record.direction is Direction.OUTBOUND
        assert record.asset_ip == "10.0.0.7"
        assert record.peer_ip == "8.8.8.8"
        assert record.peer_port == 53
        assert (record.bytes_out, record.bytes_in) == (64, 128)

    def test_public_source_to_local_asset_is_inbound_and_swaps_counters(self) -> None:
        """The counter swap is the point of orientation: 'out' must mean 'left the asset'
        regardless of which endpoint the exporter listed first."""
        record = orient(
            src_ip="203.0.113.9",
            src_port=40000,
            dst_ip="10.0.0.7",
            dst_port=22,
            protocol="tcp",
            src_bytes=500,
            dst_bytes=40,
            src_packets=5,
            dst_packets=2,
        )
        assert record.direction is Direction.INBOUND
        assert record.asset_ip == "10.0.0.7"
        assert record.asset_port == 22
        assert record.peer_ip == "203.0.113.9"
        assert record.peer_port == 40000
        # The asset SENT 40 (what the exporter called dst_bytes) and RECEIVED 500.
        assert (record.bytes_out, record.bytes_in) == (40, 500)
        assert (record.packets_out, record.packets_in) == (2, 5)

    def test_both_local_is_internal_and_attributes_to_the_originator(self) -> None:
        # A scan of the internal network belongs to the scanner, not to each victim.
        record = orient(
            src_ip="10.0.0.7", src_port=40000, dst_ip="10.0.0.9", dst_port=23, protocol="tcp"
        )
        assert record.direction is Direction.INTERNAL
        assert record.asset_ip == "10.0.0.7"
        assert record.peer_ip == "10.0.0.9"

    def test_neither_local_is_refused(self) -> None:
        with pytest.raises(FlowOrientationError, match="neither endpoint"):
            orient(src_ip="8.8.8.8", dst_ip="1.1.1.1", protocol="udp")

    def test_home_networks_are_configurable(self) -> None:
        public_fleet = Locality(parse_networks(["203.0.113.0/24"]))
        record = orient(
            src_ip="8.8.8.8", dst_ip="203.0.113.9", protocol="tcp", locality=public_fleet
        )
        assert record.asset_ip == "203.0.113.9"
        assert record.direction is Direction.INBOUND

    def test_portless_protocols_survive(self) -> None:
        record = orient(src_ip="10.0.0.7", dst_ip="8.8.8.8", protocol="icmp")
        assert record.peer_port is None
        assert record.asset_port is None


class TestMetadataRoundTrip:
    RECORD = FlowRecord(
        asset_ip="10.0.0.7",
        peer_ip="203.0.113.9",
        protocol="tcp",
        direction=Direction.OUTBOUND,
        asset_port=51514,
        peer_port=23,
        duration_s=0.25,
        bytes_out=44,
        bytes_in=0,
        packets_out=1,
        packets_in=0,
        state="S0",
        service="telnet",
    )

    def test_round_trip_preserves_every_field(self) -> None:
        assert FlowRecord.from_metadata(self.RECORD.to_metadata()) == self.RECORD

    def test_to_metadata_writes_every_field_of_the_record(self) -> None:
        """Drift guard for the hand-written writer.

        ``to_metadata`` lists its keys explicitly (it runs once per flow row, so it is
        deliberately not ``asdict``). The round-trip test above cannot catch a field added
        to ``FlowRecord`` but forgotten here — both sides would simply default it — so the
        completeness check is made directly.
        """
        assert set(self.RECORD.to_metadata()) == {f.name for f in fields(FlowRecord)}

    def test_round_trip_survives_json(self) -> None:
        # Metadata is stored as JSON/JSONB, so the trip through a serializer is the real one.
        import json

        revived = FlowRecord.from_metadata(json.loads(json.dumps(self.RECORD.to_metadata())))
        assert revived == self.RECORD

    def test_from_event_reads_the_record_back(self) -> None:
        assert FlowRecord.from_event(flow_event(self.RECORD)) == self.RECORD

    def test_from_event_returns_none_for_an_event_with_no_flow_metadata(self) -> None:
        event = NormalizedEvent(
            timestamp=T0,
            asset_id="10.0.0.7",
            source_type=Lane.FLOW,
            source_format="test-flow",
            raw="test",
        )
        assert FlowRecord.from_event(event) is None

    @pytest.mark.parametrize(
        "broken",
        [
            "not-a-mapping",
            {"peer_ip": "8.8.8.8", "protocol": "udp", "direction": "outbound"},  # no asset_ip
            {"asset_ip": "10.0.0.7", "peer_ip": "8.8.8.8", "protocol": "udp"},  # no direction
            {
                "asset_ip": "10.0.0.7",
                "peer_ip": "8.8.8.8",
                "protocol": "udp",
                "direction": "sideways",
            },  # not a Direction
            {
                "asset_ip": "10.0.0.7",
                "peer_ip": "8.8.8.8",
                "protocol": "udp",
                "direction": "outbound",
                "bytes_out": "lots",
            },  # unreadable field
        ],
        ids=["not-a-mapping", "missing-asset", "missing-direction", "bad-direction", "bad-number"],
    )
    def test_malformed_metadata_raises_with_a_reason(self, broken: object) -> None:
        with pytest.raises(FlowFieldError):
            FlowRecord.from_metadata(broken)

    def test_from_event_skips_rather_than_raises_on_malformed_metadata(self) -> None:
        """The detector's read path must not fail a run over one uninterpretable event —
        the port's defensive law is to ignore what it cannot judge."""
        assert FlowRecord.from_event(flow_event(self.RECORD, metadata_override={"bad": 1})) is None


class TestSummary:
    def test_outbound_summary_reads_left_to_right(self) -> None:
        record = orient(
            src_ip="10.0.0.7",
            src_port=51514,
            dst_ip="203.0.113.9",
            dst_port=23,
            protocol="tcp",
            src_bytes=44,
            src_packets=1,
            state="S0",
        )
        assert record.summary() == "10.0.0.7:51514 -> 203.0.113.9:23 tcp S0 1/0 pkt 44/0 B"

    def test_inbound_summary_points_at_the_asset(self) -> None:
        record = orient(
            src_ip="203.0.113.9",
            src_port=40000,
            dst_ip="10.0.0.7",
            dst_port=22,
            protocol="tcp",
            src_bytes=500,
            dst_bytes=40,
            src_packets=5,
            dst_packets=2,
        )
        assert record.summary().startswith("10.0.0.7:22 <- 203.0.113.9:40000 tcp")

    def test_portless_summary_omits_ports(self) -> None:
        record = orient(src_ip="10.0.0.7", dst_ip="8.8.8.8", protocol="icmp")
        assert record.summary().startswith("10.0.0.7 -> 8.8.8.8 icmp")
