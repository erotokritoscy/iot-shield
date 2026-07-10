"""Shared AlertEngine machinery: correlate, rank, and label — implemented once.

Both alert-engine adapters follow the same skeleton: bucket detections by asset and
a floored time window, build one alert per bucket, then rank by priority. Only the
per-bucket scoring differs. That skeleton (``correlate_and_rank``), the bucketing
and margin measure it builds on, and the analyst-facing priority -> severity mapping
(``severity_from_priority``, a product convention independent of engine) live here
once — the alert-side analog of ``normalize._linewise``. Each bucket is non-empty
and single-asset.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from iotshield.contracts import AlertSeverity, DetectionResult, PrioritizedAlert

_SEVERITY_FLOOR = [
    (90.0, AlertSeverity.EMERGENCY),
    (70.0, AlertSeverity.CRITICAL),
    (50.0, AlertSeverity.HIGH),
    (30.0, AlertSeverity.MEDIUM),
]


def detection_margin(detection: DetectionResult) -> float:
    """How far a detection cleared its threshold (score / threshold); 2.0 when the
    threshold is non-positive, so a degenerate detector still reads as anomalous."""
    if detection.threshold > 0:
        return detection.score / detection.threshold
    return 2.0


def severity_from_priority(priority: float) -> AlertSeverity:
    """Map a 0-100 priority to its analyst-facing label. A shared convention: a 70
    reads as CRITICAL regardless of which engine produced it."""
    for floor, severity in _SEVERITY_FLOOR:
        if priority >= floor:
            return severity
    return AlertSeverity.LOW


def _floor_to_bucket(moment: datetime, bucket: timedelta) -> datetime:
    seconds = bucket.total_seconds()
    return datetime.fromtimestamp((moment.timestamp() // seconds) * seconds, tz=UTC)


def bucket_by_asset_window(
    detections: Sequence[DetectionResult], bucket: timedelta
) -> list[list[DetectionResult]]:
    """Group detections into (asset_id, floored window_start) buckets, insertion-ordered."""
    groups: dict[tuple[str, datetime], list[DetectionResult]] = {}
    for detection in detections:
        key = (detection.asset_id, _floor_to_bucket(detection.window_start, bucket))
        groups.setdefault(key, []).append(detection)
    return list(groups.values())


def correlate_and_rank(
    detections: Sequence[DetectionResult],
    *,
    bucket: timedelta,
    build_alert: Callable[[list[DetectionResult]], PrioritizedAlert],
) -> list[PrioritizedAlert]:
    """Bucket detections, build one alert per bucket, and rank by priority.

    The skeleton every engine shares; only ``build_alert`` (the per-bucket scoring)
    differs. Ranking is a port-level "ranked by priority" concern, so the compound
    tie-break (priority, then asset, then window) lives here once.
    """
    groups = bucket_by_asset_window(detections, bucket)
    alerts = [build_alert(group) for group in groups]
    return sorted(
        alerts, key=lambda alert: (-alert.priority_score, alert.asset_id, alert.window_start)
    )
