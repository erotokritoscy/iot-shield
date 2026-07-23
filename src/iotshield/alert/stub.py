"""Reference AlertEngine: bucket by (asset, time window), template scoring.

Scoring is a bounded, monotone toy — the pre-committed fallback next to the
team2-derived WeightedAlertEngine: base from the strongest detection's
score/threshold margin, plus bonuses for cross-lane corroboration, detection
volume, and asset criticality. Adding a corroborating detection can only raise a
bucket's priority (port semantics 3). Correlation, ranking, and the severity-label
mapping are shared with the weighted engine (``_correlate``); only this additive
scoring is stub-specific.
"""

from collections.abc import Mapping, Sequence
from datetime import timedelta

from iotshield.alert._correlate import correlate_and_rank, detection_margin, severity_from_priority
from iotshield.capabilities import ALERTS, DETECTIONS, Capability
from iotshield.contracts import Asset, Criticality, DetectionResult, PrioritizedAlert

# Consumes detections, produces alerts — the universal alert-engine shape.
CAPABILITY = Capability.of(consumes={DETECTIONS}, produces={ALERTS})

_CRITICALITY_BONUS = {
    Criticality.LOW: 0.0,
    Criticality.MEDIUM: 5.0,
    Criticality.HIGH: 10.0,
    Criticality.CRITICAL: 15.0,
}


class StubAlertEngine:
    def __init__(self, bucket: timedelta = timedelta(minutes=5)) -> None:
        self._bucket = bucket

    def build_alerts(
        self,
        detections: Sequence[DetectionResult],
        assets: Mapping[str, Asset],
    ) -> list[PrioritizedAlert]:
        return correlate_and_rank(
            detections,
            bucket=self._bucket,
            build_alert=lambda group: self._build_alert(group, assets.get(group[0].asset_id)),
        )

    def _build_alert(self, group: list[DetectionResult], asset: Asset | None) -> PrioritizedAlert:
        asset_id = group[0].asset_id
        strongest = max(group, key=detection_margin)
        lanes = {detection.lane for detection in group}

        base = 30.0 + 30.0 * max(0.0, min(detection_margin(strongest), 2.0)) / 2.0
        corroboration = 20.0 * (len(lanes) - 1)
        volume = 5.0 * min(len(group) - 1, 3)
        criticality = asset.criticality if asset else Criticality.MEDIUM
        criticality_bonus = _CRITICALITY_BONUS[criticality]
        priority = min(base + corroboration + volume + criticality_bonus, 100.0)

        asset_label = asset.name if asset else "unregistered asset"
        rationale = (
            f"{len(group)} detection(s) across {len(lanes)} lane(s) on {asset_label}"
            f" ({asset_id}, criticality {criticality}); strongest: {strongest.method}"
            f" score {strongest.score:.4g} vs threshold {strongest.threshold:.4g}."
        )
        window_start = min(detection.window_start for detection in group)
        window_end = max(detection.window_end for detection in group)

        return PrioritizedAlert(
            asset_id=asset_id,
            window_start=window_start,
            window_end=window_end,
            severity=severity_from_priority(priority),
            priority_score=priority,
            rationale=rationale,
            score_components={
                "base": base,
                "corroboration": corroboration,
                "volume": volume,
                "asset_criticality": criticality_bonus,
            },
            detection_ids=[detection.detection_id for detection in group],
            recommended_action=(
                f"Inspect {asset_id} around {window_start.isoformat()};"
                f" review {strongest.method} evidence."
            ),
        )
