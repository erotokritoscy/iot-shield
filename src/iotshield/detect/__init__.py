"""Detector port + adapters: NormalizedEvent stream -> DetectionResult.

One adapter per modality lane. Adapters: StubThresholdDetector (reference/fallback);
Team3ZScoreDetector (telemetry robust z-score, vendored from team3 — ADR-0012);
LogBurstDetector (log-lane rate/burst rule, designed here — ADR-0020).
Planned: team5 flows.
"""

from iotshield.detect.log_burst import LogBurstDetector
from iotshield.detect.port import Detector
from iotshield.detect.stub import StubThresholdDetector
from iotshield.detect.team3 import Team3ZScoreDetector

__all__ = ["Detector", "LogBurstDetector", "StubThresholdDetector", "Team3ZScoreDetector"]
