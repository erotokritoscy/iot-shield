"""The FLOW lane: a fleet's normal traffic, the scan shapes, and flow-csv.

The lane where a corpus finally has more than one device, which is what ADR-0026's asset-scoped
truth exists for: the valuable measurement here is not *catching* the scanner (easy) but the
innocent devices that must stay quiet — including the two controls ``config/demo_flow.yaml``
claims and has never measured, a high-volume single-destination gateway and an inbound-heavy
server.

``flow-csv`` rather than ``zeek-conn``: it is our own dialect, described in ``normalize.flow``
as the lane's "dumb, permanent reference dialect", it parses ISO-8601 exactly rather than epoch
floats, and it is readable under ``gzip -dc``. The Zeek dialect is already exercised by
``data/demo/demo_flow.log``, so this also widens dialect coverage rather than duplicating it.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.eval.generate.port import Calibration, Observed, Streams
from iotshield.eval.generate.world import Clock, Device, FaultSpec
from iotshield.flow import METADATA_KEY, METRIC, Direction, FlowRecord, orient
from iotshield.normalize.flow import FLOW_CSV, MISSING

SOURCE_FORMAT = FLOW_CSV.name

_COLUMNS = tuple(FLOW_CSV.columns.values())
"""The dialect's own headings, in its own order — taken from the reader, not retyped.

``Dialect.columns`` maps canonical field to column heading, so this is the header the parser
will look for by construction. Retyping it meant a new canonical field could be added to the
dialect and silently never written; the round-trip test catches a *rename* but not an
addition. ``_line`` builds its fields in this order.
"""

FAN_OUT_THRESHOLD = 20.0
PORT_SCAN_THRESHOLD = 15.0
CALIBRATION = (
    Calibration("flow_burst", "threshold", FAN_OUT_THRESHOLD, preset="fan_out"),
    Calibration("flow_burst", "threshold", PORT_SCAN_THRESHOLD, preset="port_scan"),
)
"""The boundaries the peer counts below are chosen around — with ``resolver`` deliberately just
under fan_out's and ``mirror`` deliberately over it, so the corpus measures the threshold
instead of confirming it."""

# Non-local space for external peers (TEST-NET-2/3, reserved for documentation), so orientation
# sees exactly one local endpoint and attributes the flow to our device.
_EXTERNAL = "198.51.100.{}"
_UPLINK = "203.0.113.9"

_EXFIL_CHUNK_BYTES = 1_200_000
"""Per-flow payload for a bulk transfer. Sized so a fault's worth of them clears
``flow_burst``'s exfiltration preset (50 MB per half hour) rather than skimming it."""


@dataclass(frozen=True)
class _Profile:
    """How one kind of device talks when nothing is wrong.

    ``distinct_peers`` is the knob that matters: fan-out counts *cardinality* inside a sliding
    minute, so a device's normal peer count — not its flow count — decides whether a
    fleet-wide threshold misfires on it.
    """

    every_steps: int
    distinct_peers: int
    port: int
    service: str
    bytes_out: int
    bytes_in: int
    inbound: bool = False


_PROFILES: dict[str, _Profile] = {
    # A quiet IoT device: a broker, an NTP server, a resolver. Nothing like a scanner.
    "camera": _Profile(
        every_steps=20, distinct_peers=3, port=1883, service="mqtt", bytes_out=840, bytes_in=410
    ),
    # THE VOLUME CONTROL: thirty flows to ONE destination. Volume is not fan-out — cardinality
    # is the signal, which is exactly what a "too many events" rule could not express.
    "gateway": _Profile(
        every_steps=3, distinct_peers=1, port=443, service="ssl", bytes_out=2210, bytes_in=18400
    ),
    # THE DIRECTION CONTROL: peers dial IN. Someone connecting to a popular server is a
    # different phenomenon, and counting it would make every server a "scanner".
    "server": _Profile(
        every_steps=6,
        distinct_peers=12,
        port=22,
        service="ssh",
        bytes_out=1200,
        bytes_in=800,
        inbound=True,
    ),
    # THE CALIBRATION DEVICE: legitimately chatty, just under fan_out's boundary of 20.
    "resolver": _Profile(
        every_steps=3, distinct_peers=18, port=53, service="dns", bytes_out=90, bytes_in=260
    ),
    # DELIBERATELY OVER the boundary: an update mirror whose normal peer count exceeds a
    # fleet-wide constant. ADR-0024's Consequences predicted this false alarm; the corpus is
    # where it stops being a prediction.
    "mirror": _Profile(
        every_steps=2, distinct_peers=26, port=443, service="ssl", bytes_out=1500, bytes_in=64000
    ),
    "sensor": _Profile(
        every_steps=30, distinct_peers=2, port=8883, service="mqtt", bytes_out=300, bytes_in=180
    ),
}
_DEFAULT_PROFILE = "sensor"


class NetworkHost:
    """Normal traffic per device kind, plus the three flow fault shapes."""

    lane = Lane.FLOW
    calibration = CALIBRATION

    def __init__(self) -> None:
        # The dispatch table is the declaration — see `SensorRig.__init__`.
        self._handlers: dict[str, Callable[[Device, FaultSpec, list[int]], list[FlowRecord]]] = {
            "scan": self._sweep,
            "compromise": self._sweep,
            "port_sweep": self._ports,
            "exfiltration": self._bulk,
        }
        self.expresses = frozenset(self._handlers)

    def observe(
        self,
        device: Device,
        faults: Sequence[FaultSpec],
        clock: Clock,
        streams: Streams,
        source_format: str,
    ) -> Observed:
        profile = _PROFILES.get(device.kind, _PROFILES[_DEFAULT_PROFILE])
        rng = streams("normal")
        # The peer set is cycled through, so build it once instead of formatting a string per
        # flow (a chatty profile emits one every other step).
        peers = [_EXTERNAL.format(10 + offset) for offset in range(profile.distinct_peers)]
        flows: list[tuple[int, FlowRecord]] = [
            (step, self._normal(device, profile, peers, index, int(rng.integers(-40, 40))))
            for index, step in enumerate(range(0, clock.steps, profile.every_steps))
        ]

        expressed: set[str] = set()
        for fault in faults:
            handler = self._handlers.get(fault.kind)
            if handler is None:
                continue
            steps = clock.spread(fault.start_step, fault.length_steps, int(fault.intensity))
            flows.extend(zip(steps, handler(device, fault, steps), strict=True))
            expressed.add(fault.fault_id)

        events = tuple(
            self._event(device, clock.at(step), record, source_format) for step, record in flows
        )
        return Observed(events=events, expressed=frozenset(expressed))

    def _normal(
        self,
        device: Device,
        profile: _Profile,
        peers: Sequence[str],
        index: int,
        jitter: int,
    ) -> FlowRecord:
        peer = peers[index % len(peers)]
        origin, target = (peer, device.asset_id) if profile.inbound else (device.asset_id, peer)
        return orient(
            src_ip=origin,
            dst_ip=target,
            protocol="tcp",
            src_port=40000 + index,
            dst_port=profile.port,
            duration_s=1.5,
            src_bytes=max(1, profile.bytes_out + jitter),
            dst_bytes=max(1, profile.bytes_in + jitter),
            src_packets=6,
            dst_packets=5,
            state="SF",
            service=profile.service,
        )

    def _sweep(self, device: Device, fault: FaultSpec, steps: Sequence[int]) -> list[FlowRecord]:
        """Many distinct peers, one port, unanswered — the Mirai propagation shape."""
        del fault
        return [
            orient(
                src_ip=device.asset_id,
                dst_ip=_EXTERNAL.format(100 + index),
                protocol="tcp",
                src_port=52000 + index,
                dst_port=23,
                state="S0",
                src_packets=1,
            )
            for index in range(len(steps))
        ]

    def _ports(self, device: Device, fault: FaultSpec, steps: Sequence[int]) -> list[FlowRecord]:
        """Low fan-out, high port cardinality: what fan_out is structurally blind to, which is
        what makes the pair discriminating (the flow analogue of a stuck sensor)."""
        del fault
        return [
            orient(
                src_ip=device.asset_id,
                dst_ip=_UPLINK,
                protocol="tcp",
                src_port=53000 + index,
                dst_port=1 + index,
                state="S0",
                src_packets=1,
            )
            for index in range(len(steps))
        ]

    def _bulk(self, device: Device, fault: FaultSpec, steps: Sequence[int]) -> list[FlowRecord]:
        """Bulk out to a single peer: neither a fan-out nor a port sweep, so only the
        byte-summing preset can see it."""
        del fault
        return [
            orient(
                src_ip=device.asset_id,
                dst_ip=_UPLINK,
                protocol="tcp",
                src_port=54000 + index,
                dst_port=443,
                duration_s=9.0,
                src_bytes=_EXFIL_CHUNK_BYTES,
                dst_bytes=400,
                src_packets=900,
                dst_packets=12,
                state="SF",
                service="ssl",
            )
            for index in range(len(steps))
        ]

    def _event(
        self, device: Device, timestamp: datetime, record: FlowRecord, source_format: str
    ) -> NormalizedEvent:
        return NormalizedEvent(
            timestamp=timestamp,
            asset_id=device.asset_id,
            source_type=Lane.FLOW,
            source_format=source_format,
            metric=METRIC,
            value=float(record.bytes_out),
            message=record.summary(),
            metadata={METADATA_KEY: record.to_metadata()},
            raw="",
        )


class FlowCsvRenderer:
    """Inverts ``flow.orient``: an asset-relative record back into two endpoints.

    Orientation is the one genuinely lossy-looking step on the reading side — it decides which
    endpoint is "the asset" from the monitored networks and re-expresses every counter relative
    to it. Writing it back out means undoing that choice: for an inbound flow the asset is the
    *destination*, so ``bytes_out`` (which left the asset) is the destination's byte count. The
    round-trip test is what proves this inversion is exact rather than approximately right.
    """

    lane = Lane.FLOW
    source_format = SOURCE_FORMAT

    def header(self) -> tuple[str, ...]:
        return (",".join(_COLUMNS),)

    def render(self, events: Sequence[NormalizedEvent]) -> tuple[str, ...]:
        return tuple(self._line(event) for event in events)

    def _line(self, event: NormalizedEvent) -> str:
        record = FlowRecord.from_metadata(event.metadata.get(METADATA_KEY))
        if record.direction is Direction.INBOUND:
            source, destination = record.peer_ip, record.asset_ip
            source_port, destination_port = record.peer_port, record.asset_port
            source_bytes, destination_bytes = record.bytes_in, record.bytes_out
            source_packets, destination_packets = record.packets_in, record.packets_out
        else:
            source, destination = record.asset_ip, record.peer_ip
            source_port, destination_port = record.asset_port, record.peer_port
            source_bytes, destination_bytes = record.bytes_out, record.bytes_in
            source_packets, destination_packets = record.packets_out, record.packets_in
        fields = (
            event.timestamp.isoformat(),
            source,
            _optional(source_port),
            destination,
            _optional(destination_port),
            record.protocol,
            _optional(record.service),
            f"{record.duration_s:g}",
            str(source_bytes),
            str(destination_bytes),
            str(source_packets),
            str(destination_packets),
            _optional(record.state),
        )
        return ",".join(fields)


_UNSET = next(marker for marker in MISSING if marker)
"""What an unset field is written as: the reader's own missing-value marker.

``MISSING`` lists every token the parser *accepts* as unset (including the empty string); a
writer has to pick one, and the non-empty one is the only choice that survives a CSV round
trip legibly.
"""


def _optional(value: object) -> str:
    return _UNSET if value is None else str(value)
