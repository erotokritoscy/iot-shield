"""Detector port + adapters: NormalizedEvent stream -> DetectionResult.

Adapters: StubThresholdDetector (reference/fallback, lane-parameterized);
Team3ZScoreDetector (telemetry robust z-score, vendored from team3 — ADR-0012);
LogBurstDetector (log-lane rate/burst rule, designed here — ADR-0020);
FlowBurstDetector (flow-lane behavioural aggregation — fan-out, port scanning,
exfiltration — designed here, ADR-0024); MeterIForestDetector (utility-meter Isolation
Forest over hour-of-week features, vendored from Panayiotis's meterdetect — ADR-0030,
and the first scikit-learn model in the runtime); RuleDetector (named authored signatures
over single events, YAML rules ported from Antonia's alert-correlation project — ADR-0032,
and the first detector extensible without writing Python); MeterQualityDetector (SensiPro's
heartbeat and high meter data-quality anomalies, as written rules — ADR-0043).

Every adapter above answers "is this unusual?" except the last, which answers "is this a
known-bad thing?" — the two families fail in opposite directions and are complements, not
alternatives (ADR-0032).

Deferred: a supervised per-flow classifier in team5's shape, once there is a labelled
flow corpus to prove it on (ADR-0024 D1; scikit-learn itself is now available — ADR-0030).
"""

from iotshield.detect._runs import merge_runs
from iotshield.detect.flow_burst import FlowBurstDetector
from iotshield.detect.log_burst import LogBurstDetector
from iotshield.detect.meter import MeterIForestDetector
from iotshield.detect.meter_quality import MeterQualityDetector
from iotshield.detect.port import Detector, Fittable
from iotshield.detect.rules import RuleDetector
from iotshield.detect.stub import StubThresholdDetector
from iotshield.detect.team3 import Team3ZScoreDetector

__all__ = [
    "Detector",
    "Fittable",
    "FlowBurstDetector",
    "LogBurstDetector",
    "MeterIForestDetector",
    "MeterQualityDetector",
    "RuleDetector",
    "StubThresholdDetector",
    "Team3ZScoreDetector",
    "merge_runs",
]
