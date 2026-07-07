"""Detector port: normalized events in, positive judgments out.

One detector per modality lane. The pipeline routes events by lane, but
implementations must be defensive: events from a foreign lane are ignored,
never judged. Batch semantics (sequences in, lists out) by ADR-0004 — a
streaming variant would be an additional port, not a change to this one.

Semantics every implementation must honor (enforced by the suite in
tests/ports/test_detector_port.py):
1. Emitted detections carry this detector's ``lane`` and ``method``.
2. Detection windows lie within the time range of the input events.
3. ``score > threshold`` for every emitted detection ("no anomaly" is a
   successful judgment with zero detections, not a failure).
4. Same input, same output (deterministic apart from generated ids).
"""

from collections.abc import Sequence
from typing import Protocol

from iotshield.contracts import DetectionResult, Lane, NormalizedEvent


class Detector(Protocol):
    @property
    def lane(self) -> Lane:
        """The modality this detector judges; the pipeline routes events by it."""
        ...

    @property
    def method(self) -> str:
        """Detector name stamped on every emitted detection, e.g. "lstm_ae"."""
        ...

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        """Judge a batch of events; return only positive detections."""
        ...
