"""FLOW lane vocabulary: the typed flow record that crosses normalize -> detect.

Why this module exists (ADR-0023 D5)
------------------------------------
For the other two lanes, a modality's whole payload fits in ``NormalizedEvent``'s own
typed fields: telemetry is ``(timestamp, asset_id, metric, value)``, a log line is
``(timestamp, asset_id, severity, message)``. Their normalizers and detectors are
therefore coupled through *attributes*, checked by pydantic and the type checker, and
neither lane needs a shared vocabulary at all.

A flow is the first modality that does not fit. Its signal is a tuple — who talked to
whom, on what port, with what result — of which only one component (a byte count) is a
number the contract can hold. The rest must ride in ``NormalizedEvent.metadata``, which
is ``dict[str, Any]`` and therefore unchecked: if the normalizer writes ``"peer_port"``
and the detector reads ``"dst_port"``, nothing raises. The run simply produces no
detections, which is indistinguishable from a healthy run that found nothing — the
silent-emptiness failure the compatibility guard exists to prevent one layer up.

So this module is that lost type safety, put back: the untyped hop is exactly one
function call wide (``FlowRecord.from_event``), and everything downstream is typed. It is
to ``metadata`` what the pydantic contract is to raw text.

It is deliberately NOT a contract in ``contracts/``: these fields mean something to one
lane only, and hanging ``peer_ip``/``protocol``/``bytes_in`` on every telemetry and log
event (and on the events table, forever NULL) is the cost of pretending otherwise. The
contract stays modality-agnostic; a lane that outgrows it owns its own typed extension.
The day two lanes need to agree on one structure is the day it graduates, with an ADR.

Depends only on ``contracts``, so both ``normalize/flow.py`` and ``detect/flow_burst.py``
import it without a cycle — the same position ``capabilities.py`` occupies.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Any

from iotshield.contracts import NormalizedEvent

METADATA_KEY = "flow"
"""The single ``metadata`` key the whole record lives under.

Namespaced rather than flattened so a normalizer may add its own unrelated metadata
without any chance of colliding with a flow field.
"""

METRIC = "bytes_out"
"""``NormalizedEvent.metric`` for a flow event — naming the one scalar we surface.

Bytes *sent by the asset* is the only per-flow number that is meaningful standing alone
(it is the exfiltration signal), and it is only well defined because orientation has
already decided which endpoint the asset is. Putting it in ``value`` also means the
lane-parameterized ``stub_threshold`` detector works on FLOW events with no new code.
"""

Network = IPv4Network | IPv6Network

DEFAULT_HOME_NETWORKS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "fc00::/7",
    "fe80::/10",
)
"""Private and link-local space — the monitored side in the overwhelming majority of IoT
deployments, so the normalizer needs no configuration to be correct out of the box.
Overridable because some fleets are publicly addressed (ADR-0023 D2)."""


class Direction(StrEnum):
    """Where the conversation went, *relative to the asset*.

    Direction is only definable once orientation has picked an asset side, which is why
    it lives here rather than being read off a raw row.
    """

    OUTBOUND = "outbound"  # the asset originated a connection to a non-local peer
    INBOUND = "inbound"  # a non-local peer originated a connection to the asset
    INTERNAL = "internal"  # both endpoints are inside the monitored network


class FlowFieldError(ValueError):
    """Metadata that does not carry a well-formed flow record."""


class FlowOrientationError(ValueError):
    """A flow with no endpoint inside the monitored network — not our traffic.

    Distinct from a parse failure: the row was read perfectly, it simply describes a
    conversation between two foreign hosts. The normalizer turns it into a
    ``MalformedRecord`` with this reason, so a mis-set ``home_networks`` shows up as
    ADR-0017's high-reject-rate warning rather than as a silently empty run.
    """


def parse_networks(values: Iterable[str]) -> tuple[Network, ...]:
    """Parse CIDR strings into networks, naming the offender on failure.

    ``strict=False`` so ``10.0.0.1/8`` is accepted as the network containing that host —
    an operator writing a CIDR by hand should not have to zero the host bits by hand.
    """
    networks: list[Network] = []
    for value in values:
        text = str(value).strip()
        try:
            networks.append(ip_network(text, strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid network {text!r}: {exc}") from exc
    return tuple(networks)


DEFAULT_NETWORKS: tuple[Network, ...] = parse_networks(DEFAULT_HOME_NETWORKS)


def is_local(address: str, networks: Sequence[Network]) -> bool:
    """Whether an IP literal falls inside any monitored network (bad literals are not)."""
    try:
        parsed = ip_address(address)
    except ValueError:
        return False
    # A v4 address is never inside a v6 network and vice versa; `version` short-circuits
    # the comparison ipaddress would otherwise raise on.
    return any(parsed.version == net.version and parsed in net for net in networks)


@dataclass(frozen=True)
class FlowRecord:
    """One network conversation, oriented so that "out" and "in" are the *asset's* view.

    Every counter is asset-relative: ``bytes_out`` left the asset regardless of which
    endpoint the exporter happened to list first. That is the whole value of orientation —
    a detector can ask "how much did this device send" without re-deriving which column
    meant what.
    """

    asset_ip: str
    peer_ip: str
    protocol: str
    direction: Direction
    asset_port: int | None = None
    peer_port: int | None = None
    duration_s: float = 0.0
    bytes_out: int = 0
    bytes_in: int = 0
    packets_out: int = 0
    packets_in: int = 0
    state: str | None = None
    service: str | None = None

    def summary(self) -> str:
        """The one-line human rendering that becomes ``NormalizedEvent.message``.

        This is what an analyst actually reads in alert evidence, so it keeps the shape of
        the tuple ("who -> whom, how, how much") rather than dumping fields.
        """
        arrow = "<-" if self.direction is Direction.INBOUND else "->"
        peer = self.peer_ip if self.peer_port is None else f"{self.peer_ip}:{self.peer_port}"
        asset = self.asset_ip if self.asset_port is None else f"{self.asset_ip}:{self.asset_port}"
        parts = [f"{asset} {arrow} {peer}", self.protocol]
        if self.service:
            parts.append(self.service)
        if self.state:
            parts.append(self.state)
        parts.append(f"{self.packets_out}/{self.packets_in} pkt {self.bytes_out}/{self.bytes_in} B")
        return " ".join(parts)

    def to_metadata(self) -> dict[str, Any]:
        """Render for ``NormalizedEvent.metadata[METADATA_KEY]`` (JSON-safe throughout)."""
        return {
            "asset_ip": self.asset_ip,
            "peer_ip": self.peer_ip,
            "protocol": self.protocol,
            "direction": self.direction.value,
            "asset_port": self.asset_port,
            "peer_port": self.peer_port,
            "duration_s": self.duration_s,
            "bytes_out": self.bytes_out,
            "bytes_in": self.bytes_in,
            "packets_out": self.packets_out,
            "packets_in": self.packets_in,
            "state": self.state,
            "service": self.service,
        }

    @classmethod
    def from_metadata(cls, raw: Any) -> FlowRecord:
        """Rebuild from stored metadata, raising ``FlowFieldError`` on anything malformed.

        Strict on purpose: this is the one place the untyped hop is crossed, so a schema
        drift between writer and reader surfaces here as an error with a field name rather
        than as a detector that quietly sees nothing.
        """
        if not isinstance(raw, dict):
            raise FlowFieldError(f"flow metadata must be a mapping, got {type(raw).__name__}")
        try:
            direction = Direction(raw["direction"])
            return cls(
                asset_ip=str(raw["asset_ip"]),
                peer_ip=str(raw["peer_ip"]),
                protocol=str(raw["protocol"]),
                direction=direction,
                asset_port=_optional_int(raw.get("asset_port")),
                peer_port=_optional_int(raw.get("peer_port")),
                duration_s=float(raw.get("duration_s", 0.0)),
                bytes_out=int(raw.get("bytes_out", 0)),
                bytes_in=int(raw.get("bytes_in", 0)),
                packets_out=int(raw.get("packets_out", 0)),
                packets_in=int(raw.get("packets_in", 0)),
                state=_optional_str(raw.get("state")),
                service=_optional_str(raw.get("service")),
            )
        except KeyError as exc:
            raise FlowFieldError(f"flow metadata is missing {exc.args[0]!r}") from exc
        except (TypeError, ValueError) as exc:
            raise FlowFieldError(f"flow metadata has an unreadable field: {exc}") from exc

    @classmethod
    def from_event(cls, event: NormalizedEvent) -> FlowRecord | None:
        """The detector's read path: the record, or None if this event carries none.

        ``None`` rather than an exception because a detector's job on an unjudgeable event
        is to skip it (the port's defensive law), not to fail the run — a foreign
        normalizer may legitimately emit FLOW-lane events we cannot interpret.
        """
        raw = event.metadata.get(METADATA_KEY)
        if raw is None:
            return None
        try:
            return cls.from_metadata(raw)
        except FlowFieldError:
            return None


def orient(
    *,
    src_ip: str,
    dst_ip: str,
    protocol: str,
    src_port: int | None = None,
    dst_port: int | None = None,
    duration_s: float = 0.0,
    src_bytes: int = 0,
    dst_bytes: int = 0,
    src_packets: int = 0,
    dst_packets: int = 0,
    state: str | None = None,
    service: str | None = None,
    home_networks: Sequence[Network] = DEFAULT_NETWORKS,
) -> FlowRecord:
    """Decide which endpoint is the asset, and re-express the flow from its point of view.

    The two-endpoint problem (ADR-0023 D2). Our whole product correlates on
    ``(asset, time)``, but a flow names *two* hosts, so something has to choose. Real
    network-security tooling resolves this with a "home network" (Zeek's ``local_nets``,
    Suricata's ``$HOME_NET``) and so do we:

    - exactly one endpoint local -> that endpoint is the asset, the other is the peer;
    - both local (lateral traffic) -> the *originator* is the asset, so a scan of the
      internal network is attributed to the scanner rather than to each victim;
    - neither local -> ``FlowOrientationError``. It is not our traffic, and inventing an
      asset for it would fill the registry with the public internet.
    """
    src_local = is_local(src_ip, home_networks)
    dst_local = is_local(dst_ip, home_networks)

    if not src_local and not dst_local:
        raise FlowOrientationError(
            f"neither endpoint is inside the monitored network ({src_ip} -> {dst_ip}); "
            f"widen home_networks if this traffic is yours"
        )

    asset_is_source = src_local  # both-local falls here too: the originator wins
    if src_local and dst_local:
        direction = Direction.INTERNAL
    elif src_local:
        direction = Direction.OUTBOUND
    else:
        direction = Direction.INBOUND

    if asset_is_source:
        return FlowRecord(
            asset_ip=src_ip,
            peer_ip=dst_ip,
            protocol=protocol,
            direction=direction,
            asset_port=src_port,
            peer_port=dst_port,
            duration_s=duration_s,
            bytes_out=src_bytes,
            bytes_in=dst_bytes,
            packets_out=src_packets,
            packets_in=dst_packets,
            state=state,
            service=service,
        )
    # The asset responded, so every counter swaps: what the exporter called "source"
    # arrived AT the asset.
    return FlowRecord(
        asset_ip=dst_ip,
        peer_ip=src_ip,
        protocol=protocol,
        direction=direction,
        asset_port=dst_port,
        peer_port=src_port,
        duration_s=duration_s,
        bytes_out=dst_bytes,
        bytes_in=src_bytes,
        packets_out=dst_packets,
        packets_in=src_packets,
        state=state,
        service=service,
    )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
