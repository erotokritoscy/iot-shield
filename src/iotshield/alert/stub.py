"""Reference AlertEngine: bucket by (asset, time window), template scoring.

Scoring is a bounded, monotone toy — a stand-in for the Team2 formula adapter:
base from the strongest detection's score/threshold margin, plus bonuses for
cross-lane corroboration, detection volume, and asset criticality. Adding a
corroborating detection can only raise a bucket's priority (port semantics 3).
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from iotshield.contracts import (
    AlertSeverity,
    Asset,
    Criticality,
    DetectionResult,
    PrioritizedAlert,
)

_CRITICALITY_BONUS = {
    Criticality.LOW: 0.0,
    Criticality.MEDIUM: 5.0,
    Criticality.HIGH: 10.0,
    Criticality.CRITICAL: 15.0,
}

_SEVERITY_FLOOR = [
    (90.0, AlertSeverity.EMERGENCY),
    (70.0, AlertSeverity.CRITICAL),
    (50.0, AlertSeverity.HIGH),
    (30.0, AlertSeverity.MEDIUM),
]


def _floor_to_bucket(moment: datetime, bucket: timedelta) -> datetime:
    seconds = bucket.total_seconds()
    return datetime.fromtimestamp((moment.timestamp() // seconds) * seconds, tz=UTC)


def _margin(detection: DetectionResult) -> float:
    if detection.threshold > 0:
        return detection.score / detection.threshold
    return 2.0


class StubAlertEngine:
    def __init__(self, bucket: timedelta = timedelta(minutes=5)) -> None:
        self._bucket = bucket

    def build_alerts(
        self,
        detections: Sequence[DetectionResult],
        assets: Mapping[str, Asset],
    ) -> list[PrioritizedAlert]:
        buckets: dict[tuple[str, datetime], list[DetectionResult]] = {}
        for detection in detections:
            key = (detection.asset_id, _floor_to_bucket(detection.window_start, self._bucket))
            buckets.setdefault(key, []).append(detection)

        alerts = [
            self._build_alert(asset_id, group, assets.get(asset_id))
            for (asset_id, _), group in buckets.items()
        ]
        # Compound key makes the tie-break for equal-priority alerts explicit.
        return sorted(
            alerts, key=lambda alert: (-alert.priority_score, alert.asset_id, alert.window_start)
        )

    def _build_alert(
        self, asset_id: str, group: list[DetectionResult], asset: Asset | None
    ) -> PrioritizedAlert:
        strongest = max(group, key=_margin)
        lanes = {detection.lane for detection in group}

        base = 30.0 + 30.0 * max(0.0, min(_margin(strongest), 2.0)) / 2.0
        corroboration = 20.0 * (len(lanes) - 1)
        volume = 5.0 * min(len(group) - 1, 3)
        criticality = asset.criticality if asset else Criticality.MEDIUM
        criticality_bonus = _CRITICALITY_BONUS[criticality]
        priority = min(base + corroboration + volume + criticality_bonus, 100.0)

        severity = AlertSeverity.LOW
        for floor, candidate in _SEVERITY_FLOOR:
            if priority >= floor:
                severity = candidate
                break

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
            severity=severity,
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
