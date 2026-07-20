"""Detector port + adapters: NormalizedEvent stream -> DetectionResult.

Adapters: StubThresholdDetector (reference/fallback, lane-parameterized);
Team3ZScoreDetector (telemetry robust z-score, vendored from team3 — ADR-0012);
LogBurstDetector (log-lane rate/burst rule, designed here — ADR-0020);
FlowBurstDetector (flow-lane behavioural aggregation — fan-out, port scanning,
exfiltration — designed here, ADR-0024).

Deferred: a supervised per-flow classifier in team5's shape, once there is a labelled
flow corpus to prove it on and scikit-learn has earned its own ADR (ADR-0024 D1).
"""

from iotshield.detect._runs import merge_runs
from iotshield.detect.flow_burst import FlowBurstDetector
from iotshield.detect.log_burst import LogBurstDetector
from iotshield.detect.port import Detector, Fittable
from iotshield.detect.stub import StubThresholdDetector
from iotshield.detect.team3 import Team3ZScoreDetector

__all__ = [
    "Detector",
    "Fittable",
    "FlowBurstDetector",
    "LogBurstDetector",
    "StubThresholdDetector",
    "Team3ZScoreDetector",
    "merge_runs",
]
