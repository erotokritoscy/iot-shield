"""Detector port + adapters: NormalizedEvent stream -> DetectionResult.

One adapter per modality lane. Adapters: StubThresholdDetector (reference/fallback);
Team3ZScoreDetector (telemetry robust z-score, vendored from team3 — ADR-0012).
Planned: log severity rules, team5 flows.
"""

from iotshield.detect.port import Detector
from iotshield.detect.stub import StubThresholdDetector
from iotshield.detect.team3 import Team3ZScoreDetector

__all__ = ["Detector", "StubThresholdDetector", "Team3ZScoreDetector"]
