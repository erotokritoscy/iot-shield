"""Reference Detector: flags any value strictly above a fixed threshold.

One detection per exceeding event (a point anomaly: zero-length window).
Deliberately dumb and permanent: reference implementation for the port suite
and the pre-committed fallback for the detection stage.
"""

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any

from iotshield.capabilities import DETECTIONS, Capability, LaneToken
from iotshield.contracts import DetectionResult, EventKind, Lane, NormalizedEvent
from iotshield.detect._group import group_by_asset

_VALUE_LANES = (Lane.TELEMETRY, Lane.FLOW)
"""The lanes whose events carry a numeric ``value`` for this detector to compare."""


class StubThresholdDetector:
    def __init__(self, threshold: float = 80.0, lane: Lane = Lane.TELEMETRY) -> None:
        # Refused here, not merely declared in the registry's Literal (ADR-0033). A LOG event
        # has no `value`, so `lane: log` produced a chain the compatibility guard certified
        # CLEAN and that could never fire: capability() dutifully declared the LOG lane, the
        # coverage check saw it covered, and the run reported zero detections forever. A
        # guard that blesses a chain which cannot fire is worse than no guard, because it
        # turns "no findings" from a fact about the world into an artifact of config. `rules`
        # already refuses its unusable lanes at construction; this matches that precedent.
        if lane not in _VALUE_LANES:
            usable = ", ".join(item.value for item in _VALUE_LANES)
            raise ValueError(
                f"stub_threshold compares a numeric reading, which the {lane.value!r} lane"
                f" never carries; usable lanes: {usable}"
            )
        # A non-positive threshold makes every detection's margin degenerate, which sends the
        # alert engine's `detection_margin` down its non-positive branch and scores an
        # arbitrary 2.0; a non-finite one either never fires (nan) or fails contract
        # validation mid-run, after ingest (inf). The burst detectors already refuse both.
        if not isfinite(threshold) or threshold <= 0:
            raise ValueError(f"threshold must be a positive, finite number, got {threshold!r}")
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
                                # :.12g, not :g. Six significant digits rounded the two
                                # numbers into equality, so a reading of 80.00001 against a
                                # threshold of 80.0 read "80 is above the configured
                                # threshold of 80" — self-contradictory, in the one field
                                # the port suite enforces and the dashboard names findings
                                # by (ADR-0032 D7, amendment).
                                "explanation": (
                                    f"{event.metric or 'value'} {event.value:.12g} is above the"
                                    f" configured threshold of {self._threshold:.12g}"
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
