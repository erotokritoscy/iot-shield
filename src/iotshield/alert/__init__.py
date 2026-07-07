"""AlertEngine port + adapters: DetectionResult(s) -> PrioritizedAlert.

Owns (asset, time-window) correlation: corroborating detections boost priority,
never gate detection. Adapters: StubAlertEngine (reference/fallback); team2
heuristic scoring core (planned).
"""

from iotshield.alert.port import AlertEngine
from iotshield.alert.stub import StubAlertEngine

__all__ = ["AlertEngine", "StubAlertEngine"]
