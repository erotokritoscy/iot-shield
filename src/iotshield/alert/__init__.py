"""AlertEngine port + adapters: DetectionResult(s) -> PrioritizedAlert.

Owns (asset, time-window) correlation: corroborating detections boost priority,
never gate detection. Adapters: StubAlertEngine (reference/fallback);
WeightedAlertEngine (team2-derived multiplicative, explainable scoring — ADR-0013).
"""

from iotshield.alert.port import AlertEngine
from iotshield.alert.stub import StubAlertEngine
from iotshield.alert.weighted import WeightedAlertEngine

__all__ = ["AlertEngine", "StubAlertEngine", "WeightedAlertEngine"]
