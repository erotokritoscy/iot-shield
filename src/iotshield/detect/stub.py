"""Reference Detector: flags any value strictly above a fixed threshold.

One detection per exceeding event (a point anomaly: zero-length window).
Deliberately dumb and permanent: reference implementation for the port suite
and the pre-committed fallback for the detection stage.
"""

from collections.abc import Sequence

from iotshield.contracts import DetectionResult, EventKind, Lane, NormalizedEvent


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
        detections: list[DetectionResult] = []
        for event in events:
            if event.source_type is not self._lane or event.value is None:
                continue
            if event.value > self._threshold:
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
                            "metric": event.metric,
                            "value": event.value,
                            "event_id": str(event.event_id),
                        },
                    )
                )
        return detections
