"""Reference Detector: flags any value strictly above a fixed threshold.

One detection per exceeding event (a point anomaly: zero-length window).
Deliberately dumb and permanent: reference implementation for the port suite
and the pre-committed fallback for the detection stage.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from iotshield.capabilities import DETECTIONS, Capability, LaneToken
from iotshield.contracts import DetectionResult, EventKind, Lane, NormalizedEvent
from iotshield.detect._group import group_by_asset


class StubThresholdDetector:
    def __init__(self, threshold: float = 80.0, lane: Lane = Lane.TELEMETRY) -> None:
        self._threshold = threshold
        self._lane = lane

    @property
    def lane(self) -> Lane:
        return self._lane

    @property
    def method(self) -> str:
        return "stub_threshold"

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        # Routed through group_by_asset like every other detector, so its output is
        # deterministic regardless of input order (the port's law 4) rather than merely
        # echoing the arrival order — which is what the shuffle test in the port suite pins.
        detections: list[DetectionResult] = []
        for _asset_id, asset_events in group_by_asset(
            events, lane=self._lane, keep=lambda event: event.value is not None
        ):
            for event in asset_events:
                if event.value is not None and event.value > self._threshold:
                    detections.append(
                        DetectionResult(
                            asset_id=event.asset_id,
                            window_start=event.timestamp,
                            window_end=event.timestamp,
                            lane=self._lane,
                            method=self.method,
                            score=event.value,
                            threshold=self._threshold,
                            event_kind=EventKind.POINT,
                            evidence={
                                # The headline every detector emits (ADR-0032 D7). Even the
                                # reference adapter owes it: it is a port-suite law, and a
                                # law the simplest implementation may skip is not a law.
                                "explanation": (
                                    f"{event.metric or 'value'} {event.value:g} is above the"
                                    f" configured threshold of {self._threshold:g}"
                                ),
                                "metric": event.metric,
                                "value": event.value,
                                "event_id": str(event.event_id),
                            },
                        )
                    )
        return detections


def capability(params: Mapping[str, Any]) -> Capability:
    """This detector's lane is configurable, so its declared capability follows params.

    Mirrors the factory's default (``registry._stub_threshold``); the meta-test in
    tests/pipeline/test_capabilities.py pins the two together against a built instance.
    """
    lane = Lane(params.get("lane", Lane.TELEMETRY))
    return Capability.of(consumes={LaneToken(lane)}, produces={DETECTIONS})
