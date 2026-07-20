"""FLOW-lane detector: a device doing too much of something on the network, too fast.

The second detector we designed rather than ported (ADR-0024), and for the same reason as
the first: there was nothing to port. team5's CICIDS2017 work is the reference set's only
flow module, and its very first preprocessing step drops ``Source IP``, ``Destination
IP``, ``Timestamp`` and the ports — so the model consumes 77 statistical features with no
identity and no time. Our contract correlates on ``(asset, timestamp)``; a detector that
cannot say *which device* or *when* cannot produce a ``DetectionResult`` at all. The
information our contract needs is destroyed upstream of any seam an adapter could sit on,
which is the same situation ADR-0011 met with team4 and resolved by rewriting.

Why behavioural aggregation rather than a classifier (ADR-0024 D2)
------------------------------------------------------------------
The unit of anomaly in flow data is not a row. One connection to port 23 is nothing; forty
connections to forty different hosts on port 23 in ten seconds is a compromised device
looking for its next victim. The signal exists only in the *aggregate over a host's recent
behaviour*, so that is what we compute. This also makes it correct on run #1 — no labels,
no training corpus, no artifact, no new runtime dependency — and explainable, which our
alert contract mandates: "cam-07 contacted 41 distinct destinations in 12s (boundary 20)"
is something an analyst can act on; a random-forest probability of 0.94 is not.

It earns the lane its place: a compromised camera scanning the internet produces perfectly
normal telemetry (it still reads 22 degrees) and writes no log lines (it is not going to
log about it). Only its network behaviour betrays it.

One mechanism, several presets (ADR-0024 D3)
--------------------------------------------
Fan-out, port scanning and exfiltration look like three detectors but are one: *per asset,
over a sliding window, does some aggregate exceed a threshold?* Only the aggregate differs,
so a preset is a ``PRESETS`` entry — an aggregate, a direction filter, and the window and
threshold that suit it. The window matters as much as the number: scanning is a
seconds-to-a-minute phenomenon and exfiltration a minutes-to-hours one, so each preset
carries its own defaults rather than sharing one that fits neither.

The preset rides in ``method`` (``flow_burst:fan_out``) so two configured instances stay
distinguishable in a detection and in the alert rationale — and a run may configure several,
which is what the orchestration UI's detector list exists to express (ADR-0023).
"""

from __future__ import annotations

import heapq
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from iotshield.capabilities import DETECTIONS, Capability, LaneToken
from iotshield.contracts import DetectionResult, EventKind, Lane, NormalizedEvent
from iotshield.detect._group import group_by_asset
from iotshield.detect._windows import (
    Aggregate,
    DistinctAggregate,
    Episode,
    SumAggregate,
    find_episodes,
)
from iotshield.flow import METADATA_KEY, Direction, FlowRecord

# Fixed flow-lane detector; declared for the compatibility guard.
CAPABILITY = Capability.of(consumes={LaneToken(Lane.FLOW)}, produces={DETECTIONS})

_TOP_PEERS = 5  # how many of the busiest peers ride along as evidence
_SAMPLE_PORTS = 10

# A flow the asset *originated*. Fan-out and port scanning are claims about what a device
# is reaching out to, so an inbound connection (someone else dialling in) is a different
# phenomenon and must not be counted toward them — otherwise a popular server would look
# like a scanner. Exfiltration, by contrast, cares only which way the bytes went.
_ASSET_INITIATED = (Direction.OUTBOUND, Direction.INTERNAL)


@dataclass(frozen=True)
class _Flow:
    """An event paired with its decoded record, so the window walk decodes once."""

    event: NormalizedEvent
    record: FlowRecord


@dataclass(frozen=True)
class Preset:
    """A named way of measuring "too much": what to aggregate, over what, how much is normal.

    Carrying ``window_minutes`` and ``threshold`` here rather than on the detector is what
    lets one adapter serve phenomena with entirely different natural timescales.
    """

    describe: str
    unit: str
    aggregate: Callable[[], Aggregate[_Flow]]
    window_minutes: float
    threshold: float
    keep: Callable[[FlowRecord], bool] = lambda record: True


PRESETS: dict[str, Preset] = {
    # Worm/botnet propagation and network sweeps. The IoT case is stark: a camera that
    # normally speaks to one broker suddenly contacting dozens of hosts is Mirai-shaped.
    "fan_out": Preset(
        describe="distinct destinations contacted",
        unit="peers",
        aggregate=lambda: DistinctAggregate(lambda flow: flow.record.peer_ip),
        window_minutes=1.0,
        threshold=20.0,
        keep=lambda record: record.direction in _ASSET_INITIATED,
    ),
    # Reconnaissance against a single host or a small set: many ports, few peers — the
    # pattern fan-out misses because the destination count stays low.
    "port_scan": Preset(
        describe="distinct destination ports probed",
        unit="ports",
        aggregate=lambda: DistinctAggregate(lambda flow: flow.record.peer_port),
        window_minutes=1.0,
        threshold=15.0,
        keep=lambda record: record.direction in _ASSET_INITIATED,
    ),
    # Bulk egress. Direction-agnostic on purpose: data leaving the asset is the concern
    # whether the asset dialled out or an attacker dialled in and pulled it.
    "exfiltration": Preset(
        describe="bytes sent",
        unit="bytes",
        aggregate=lambda: SumAggregate(lambda flow: flow.record.bytes_out),
        window_minutes=30.0,
        threshold=50_000_000.0,
    ),
}

DEFAULT_PRESET = "fan_out"
"""The format-agnostic default: it needs only the peer address, which every dialect
carries, and it is the highest-value flow signal for an IoT fleet."""


class FlowBurstDetector:
    def __init__(
        self,
        *,
        preset: str = DEFAULT_PRESET,
        window_minutes: float | None = None,
        threshold: float | None = None,
    ) -> None:
        if preset not in PRESETS:
            known = ", ".join(sorted(PRESETS))
            raise ValueError(f"unknown flow_burst preset {preset!r}; known: {known}")
        chosen = PRESETS[preset]
        # None means "whatever this preset considers normal" — a fan-out threshold of 20
        # peers and an exfiltration threshold of 50 MB cannot share a default.
        window = chosen.window_minutes if window_minutes is None else float(window_minutes)
        limit = chosen.threshold if threshold is None else float(threshold)
        if window <= 0:
            raise ValueError(f"window_minutes must be positive, got {window}")
        if limit <= 0:
            # A non-positive boundary would make every window qualify, and would put the
            # alert engine's detection_margin in its degenerate branch.
            raise ValueError(f"threshold must be positive, got {limit}")
        self._preset_name = preset
        self._preset = chosen
        self._window = timedelta(minutes=window)
        self._threshold = limit

    @property
    def lane(self) -> Lane:
        return Lane.FLOW

    @property
    def method(self) -> str:
        return f"flow_burst:{self._preset_name}"

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        # Foreign-lane isolation and deterministic asset order are the port's laws, shared
        # by every detector; only "is this event judgeable" is ours — here, does it carry a
        # flow record at all.
        detections: list[DetectionResult] = []
        for asset_id, asset_events in group_by_asset(events, lane=self.lane, keep=_has_flow):
            detections.extend(self._detect_asset(asset_id, asset_events))
        return detections

    def _detect_asset(
        self, asset_id: str, events: Sequence[NormalizedEvent]
    ) -> list[DetectionResult]:
        flows: list[_Flow] = []
        for event in events:
            record = FlowRecord.from_event(event)
            # A record that will not decode is skipped, not fatal: the port's defensive law
            # (a foreign normalizer may emit FLOW events we cannot interpret).
            if record is not None and self._preset.keep(record):
                flows.append(_Flow(event=event, record=record))
        # `flows` inherits its order from group_by_asset, which owns the time-ordering law
        # for every adapter; filtering with `keep` preserves it.
        detections: list[DetectionResult] = []
        for episode in find_episodes(
            flows,
            timestamp=lambda flow: flow.event.timestamp,
            window=self._window,
            threshold=self._threshold,
            aggregate=self._preset.aggregate,
        ):
            span = flows[episode.start : episode.end + 1]
            detections.append(
                DetectionResult(
                    asset_id=asset_id,
                    window_start=span[0].event.timestamp,
                    window_end=span[-1].event.timestamp,
                    lane=self.lane,
                    method=self.method,
                    score=episode.peak,
                    # The configured threshold IS the exceedance boundary: find_episodes
                    # compares strictly, so the number an analyst reads is the number that
                    # decided it (a small, deliberate divergence from log_burst, whose
                    # min_count is a "how many are needed" trigger).
                    threshold=self._threshold,
                    event_kind=_classify(episode),
                    evidence=self._evidence(span, episode),
                )
            )
        return detections

    def _evidence(self, span: Sequence[_Flow], episode: Episode) -> dict[str, Any]:
        # One pass, not six. An `exfiltration` episode spans a 30-minute window and can
        # legitimately cover a large fraction of a capture, so walking the span once per
        # summary field (plus a list copy) is real work on the largest episodes.
        peers: Counter[str] = Counter()
        ports: set[int] = set()
        protocols: set[str] = set()
        directions: set[str] = set()
        bytes_out = 0
        for flow in span:
            record = flow.record
            peers[record.peer_ip] += 1
            if record.peer_port is not None:
                ports.add(record.peer_port)
            protocols.add(record.protocol)
            directions.add(record.direction.value)
            bytes_out += record.bytes_out
        # nsmallest, not a full sort: a wide sweep is exactly when the peer count is large,
        # and taking 5 of 50k costs O(n log 5) instead of O(n log n). The explicit key keeps
        # ties broken by peer name, which Counter.most_common does not promise.
        top = heapq.nsmallest(_TOP_PEERS, peers.items(), key=lambda item: (-item[1], item[0]))
        return {
            # `category` is the preset name — the semantic label of what was detected, the
            # same convention log_burst uses (ADR-0020 D6).
            "category": self._preset_name,
            "measure": self._preset.describe,
            "unit": self._preset.unit,
            "peak": episode.peak,
            "window_minutes": self._window.total_seconds() / 60,
            "flows": len(span),
            "distinct_peers": len(peers),
            "top_peers": [{"peer": peer, "flows": count} for peer, count in top],
            "peer_ports": sorted(ports)[:_SAMPLE_PORTS],
            "bytes_out": bytes_out,
            "protocols": sorted(protocols),
            "directions": sorted(directions),
        }


def _has_flow(event: NormalizedEvent) -> bool:
    """Cheap gate before decoding: only events carrying a flow record are judgeable."""
    return METADATA_KEY in event.metadata


def _classify(episode: Episode) -> EventKind:
    # Unlike a log burst (which needs >= 2 events by construction), a single very large
    # transfer can trip the exfiltration threshold on its own — a genuine point anomaly.
    return EventKind.POINT if episode.start == episode.end else EventKind.COLLECTIVE
