"""FlowBurstDetector: what each preset measures, and why the distinction matters.

The port suite pins the shared laws. These are the flow-specific claims: that fan-out
separates a scanner from a chatty client, that port_scan catches what fan-out misses, that
exfiltration is direction-agnostic where the scanning presets are not, and that one
incident stays one judgment.
"""

from datetime import UTC, datetime, timedelta

import pytest

from iotshield.contracts import EventKind, Lane, NormalizedEvent
from iotshield.detect import FlowBurstDetector
from iotshield.detect.flow_burst import PRESETS
from iotshield.flow import METADATA_KEY, Direction, FlowRecord

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
ASSET = "10.0.0.7"


def event(
    *,
    offset_s: float,
    peer: str = "203.0.113.9",
    port: int | None = 23,
    bytes_out: int = 44,
    direction: Direction = Direction.OUTBOUND,
    asset: str = ASSET,
    with_record: bool = True,
) -> NormalizedEvent:
    record = FlowRecord(
        asset_ip=asset,
        peer_ip=peer,
        protocol="tcp",
        direction=direction,
        peer_port=port,
        bytes_out=bytes_out,
    )
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id=asset,
        source_type=Lane.FLOW,
        source_format="flow-csv",
        metric="bytes_out",
        value=float(bytes_out),
        raw=f"{asset} -> {peer}:{port}",
        metadata={METADATA_KEY: record.to_metadata()} if with_record else {},
    )


class TestFanOut:
    def test_many_distinct_peers_in_a_window_is_one_detection(self) -> None:
        events = [event(offset_s=i, peer=f"203.0.113.{i}") for i in range(25)]
        (detection,) = FlowBurstDetector(preset="fan_out").detect(events)
        assert detection.asset_id == ASSET
        assert detection.lane is Lane.FLOW
        assert detection.method == "flow_burst:fan_out"
        assert detection.score == 25.0 and detection.threshold == 20.0
        assert detection.event_kind is EventKind.COLLECTIVE

    def test_a_chatty_client_is_not_a_scanner(self) -> None:
        """The distinction volume cannot make: 100 connections to ONE host is a device
        talking to its broker; 100 hosts contacted once each is reconnaissance."""
        chatty = [event(offset_s=i, peer="10.0.0.1") for i in range(100)]
        assert FlowBurstDetector(preset="fan_out").detect(chatty) == []

    def test_inbound_connections_do_not_count_toward_fan_out(self) -> None:
        # Otherwise a popular server, dialled by many clients, would look like a scanner.
        inbound = [
            event(offset_s=i, peer=f"203.0.113.{i}", direction=Direction.INBOUND) for i in range(25)
        ]
        assert FlowBurstDetector(preset="fan_out").detect(inbound) == []

    def test_internal_lateral_movement_does_count(self) -> None:
        lateral = [
            event(offset_s=i, peer=f"10.0.0.{i}", direction=Direction.INTERNAL) for i in range(25)
        ]
        assert FlowBurstDetector(preset="fan_out").detect(lateral)

    def test_a_slow_scan_spread_beyond_the_window_does_not_fire(self) -> None:
        # 25 peers over 25 minutes: never more than a couple inside a 1-minute window.
        slow = [event(offset_s=i * 60, peer=f"203.0.113.{i}") for i in range(25)]
        assert FlowBurstDetector(preset="fan_out").detect(slow) == []


class TestPortScan:
    def test_many_ports_on_one_host_is_caught(self) -> None:
        """The case fan_out structurally misses: one destination, so the peer count stays
        at 1 no matter how many ports are probed."""
        events = [event(offset_s=i, peer="10.0.0.9", port=1000 + i) for i in range(20)]
        assert FlowBurstDetector(preset="fan_out").detect(events) == []
        (detection,) = FlowBurstDetector(preset="port_scan").detect(events)
        assert detection.method == "flow_burst:port_scan"
        assert detection.score == 20.0

    def test_repeated_use_of_one_port_is_not_a_scan(self) -> None:
        events = [event(offset_s=i, peer=f"203.0.113.{i}", port=443) for i in range(30)]
        assert FlowBurstDetector(preset="port_scan").detect(events) == []


class TestExfiltration:
    def test_bytes_accumulate_across_flows_in_the_window(self) -> None:
        events = [event(offset_s=i * 60, bytes_out=20_000_000) for i in range(3)]
        (detection,) = FlowBurstDetector(preset="exfiltration").detect(events)
        assert detection.score == 60_000_000.0
        assert detection.threshold == 50_000_000.0

    def test_a_single_huge_transfer_is_a_point_anomaly(self) -> None:
        # Unlike a log burst, one flow can trip this on its own — and the contract has a
        # shape for exactly that.
        (detection,) = FlowBurstDetector(preset="exfiltration").detect(
            [event(offset_s=0, bytes_out=60_000_000)]
        )
        assert detection.event_kind is EventKind.POINT

    def test_it_counts_bytes_leaving_the_asset_regardless_of_who_dialled(self) -> None:
        # Direction-agnostic on purpose: an attacker who connects IN and pulls data out is
        # still exfiltration, and the byte counters are already asset-relative.
        inbound = [event(offset_s=0, bytes_out=60_000_000, direction=Direction.INBOUND)]
        assert FlowBurstDetector(preset="exfiltration").detect(inbound)

    def test_transfers_far_apart_do_not_accumulate(self) -> None:
        events = [
            event(offset_s=0, bytes_out=40_000_000),
            event(offset_s=7200, bytes_out=40_000_000),
        ]
        assert FlowBurstDetector(preset="exfiltration").detect(events) == []


class TestJudgmentFormation:
    def test_one_incident_produces_one_detection(self) -> None:
        """The precondition the alert engine's volume factor depends on (ADR-0020 D4):
        a 60-peer sweep is one judgment, not 40 restatements of it."""
        sweep = [event(offset_s=i * 0.5, peer=f"203.0.113.{i}") for i in range(60)]
        assert len(FlowBurstDetector(preset="fan_out").detect(sweep)) == 1

    def test_two_separate_sweeps_are_two_detections(self) -> None:
        first = [event(offset_s=i, peer=f"203.0.113.{i}") for i in range(25)]
        second = [event(offset_s=3600 + i, peer=f"198.51.100.{i}") for i in range(25)]
        assert len(FlowBurstDetector(preset="fan_out").detect(first + second)) == 2

    def test_assets_are_judged_independently(self) -> None:
        a = [event(offset_s=i, peer=f"203.0.113.{i}", asset="10.0.0.7") for i in range(25)]
        b = [event(offset_s=i, peer=f"203.0.113.{i}", asset="10.0.0.8") for i in range(25)]
        detections = FlowBurstDetector(preset="fan_out").detect(a + b)
        assert sorted(d.asset_id for d in detections) == ["10.0.0.7", "10.0.0.8"]


class TestEvidence:
    def test_evidence_carries_what_an_analyst_needs_to_act(self) -> None:
        events = [event(offset_s=i, peer=f"203.0.113.{i}", port=23) for i in range(25)]
        (detection,) = FlowBurstDetector(preset="fan_out").detect(events)
        evidence = detection.evidence
        assert evidence["category"] == "fan_out"
        assert evidence["unit"] == "peers"
        assert evidence["peak"] == 25.0
        assert evidence["distinct_peers"] == 25
        assert evidence["peer_ports"] == [23]
        assert len(evidence["top_peers"]) == 5  # a sample, not all 25
        assert evidence["directions"] == ["outbound"]


class TestConstruction:
    def test_each_preset_carries_its_own_window_and_threshold(self) -> None:
        # One shared default cannot serve a seconds-scale scan and an hours-scale transfer.
        assert PRESETS["fan_out"].window_minutes != PRESETS["exfiltration"].window_minutes
        assert PRESETS["fan_out"].threshold != PRESETS["exfiltration"].threshold

    def test_defaults_come_from_the_preset_when_not_overridden(self) -> None:
        detector = FlowBurstDetector(preset="exfiltration")
        (detection,) = detector.detect([event(offset_s=0, bytes_out=60_000_000)])
        assert detection.threshold == PRESETS["exfiltration"].threshold

    def test_overrides_are_honoured(self) -> None:
        detector = FlowBurstDetector(preset="fan_out", threshold=3)
        events = [event(offset_s=i, peer=f"203.0.113.{i}") for i in range(5)]
        (detection,) = detector.detect(events)
        assert detection.threshold == 3.0

    @pytest.mark.parametrize(
        "kwargs,message",
        [
            ({"preset": "nope"}, "unknown flow_burst preset"),
            ({"window_minutes": 0}, "window_minutes must be positive"),
            ({"threshold": 0}, "threshold must be positive"),
        ],
        ids=["bad-preset", "zero-window", "zero-threshold"],
    )
    def test_bad_construction_fails_loudly(self, kwargs: dict, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            FlowBurstDetector(**kwargs)


class TestDefensiveness:
    def test_events_without_a_flow_record_are_skipped_not_fatal(self) -> None:
        # A foreign normalizer may emit FLOW-lane events we cannot interpret.
        events = [event(offset_s=i, peer=f"203.0.113.{i}", with_record=False) for i in range(25)]
        assert FlowBurstDetector(preset="fan_out").detect(events) == []

    def test_unordered_input_is_sorted_defensively(self) -> None:
        # The runner guarantees (asset, time) order; the port promises a standalone caller
        # nothing, and a window walk on unordered input would be nonsense.
        events = [event(offset_s=i, peer=f"203.0.113.{i}") for i in range(25)]
        assert FlowBurstDetector(preset="fan_out").detect(list(reversed(events)))


class TestOrderingLawIsInherited:
    """The time-ordering law lives in group_by_asset now, not in each adapter (C2)."""

    def test_unordered_input_is_still_judged_correctly(self) -> None:
        # Previously each adapter sorted defensively; the one that forgot would silently
        # fire on everything. The routing helper owns it, so this must hold with no
        # per-adapter sort at all.
        events = [event(offset_s=i, peer=f"203.0.113.{i}") for i in range(25)]
        (detection,) = FlowBurstDetector(preset="fan_out").detect(list(reversed(events)))
        assert detection.score == 25.0

    def test_interleaved_assets_do_not_disorder_each_other(self) -> None:
        a = [event(offset_s=i, peer=f"203.0.113.{i}", asset="10.0.0.7") for i in range(25)]
        b = [event(offset_s=i, peer=f"198.51.100.{i}", asset="10.0.0.8") for i in range(25)]
        interleaved = [e for pair in zip(a, b, strict=True) for e in pair]
        detections = FlowBurstDetector(preset="fan_out").detect(interleaved)
        assert sorted(d.asset_id for d in detections) == ["10.0.0.7", "10.0.0.8"]
        assert all(d.score == 25.0 for d in detections)
