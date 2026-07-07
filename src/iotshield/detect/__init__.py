"""Detector port + adapters: NormalizedEvent stream -> DetectionResult.

One adapter per modality lane. Adapters: StubThresholdDetector (reference/fallback);
team3 telemetry-ad, log severity rules, team5 flows (planned).
"""

from iotshield.detect.port import Detector
from iotshield.detect.stub import StubThresholdDetector

__all__ = ["Detector", "StubThresholdDetector"]
