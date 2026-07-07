"""AlertEngine port: detections + asset knowledge in, ranked decisions out.

Owns both halves of the analyst-facing judgment: correlation (grouping
detections into (asset, time-window) buckets — corroboration across lanes
boosts priority, never gates detection) and scoring/ranking (ADR-0004).

Semantics every implementation must honor (enforced by the suite in
tests/ports/test_alert_engine_port.py):
1. Every input detection is covered by exactly one alert (D3: ranking filters,
   not a hidden gate).
2. An alert's window spans its detections' windows; asset lineage is consistent.
3. Corroboration is monotone: adding a corroborating detection never lowers
   the bucket's priority.
4. Unknown assets degrade gracefully (missing metadata is not an error).
"""

from collections.abc import Mapping, Sequence
from typing import Protocol

from iotshield.contracts import Asset, DetectionResult, PrioritizedAlert


class AlertEngine(Protocol):
    def build_alerts(
        self,
        detections: Sequence[DetectionResult],
        assets: Mapping[str, Asset],
    ) -> list[PrioritizedAlert]:
        """Group, score, and rank detections into analyst-facing alerts."""
        ...
