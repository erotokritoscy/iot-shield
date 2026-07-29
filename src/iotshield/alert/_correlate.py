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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from iotshield.contracts import AlertSeverity, DetectionResult, PrioritizedAlert
from iotshield.enrichment import Enrichment

UNCLASSIFIED = "unknown"
"""What an alert's threat class says when nothing in its bucket claims to know.

Named here rather than typed as a literal in each engine, and deliberately equal to
``PrioritizedAlert.threat_class``'s own default (``contracts/models.py``) — the contract
owns the value, this owns the fact that both engines use it.
"""

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


def promote_enrichment(group: Sequence[DetectionResult]) -> Enrichment | None:
    """The alert's threat classification, chosen from the detections that carry one.

    Why the alert layer reads inside ``evidence`` at all (ADR-0032 D4). Until now an engine
    read only universal fields — score, threshold, lane, method — and knew nothing about any
    particular detector. ``threat_class`` and ``recommended_action`` cannot be filled that
    way: they are content, and only the detector that made the judgment knows it. So the
    knowledge rides across the seam as a *typed* record with its own read path, which is why
    this function names no evidence keys.

    Two rules, both product decisions rather than mechanics:

    - **``threat_class`` is single, so one claim must win**: the highest precedence tier
      (an authored rule beats a detector preset beats an inference), then the strongest
      detection within that tier. Ties break on position in the group, which is insertion
      order from bucketing, so the choice is deterministic.
    - **``suggested_actions`` are unioned** across every contributing detection. An analyst
      looking at a device showing both failed logins *and* a bulk outbound transfer wants
      both sets of next steps; the combination is more informative than either alone.

    Returns ``None`` when nothing in the bucket carries enrichment — the normal case for
    the unsupervised detectors — so the caller keeps its existing fallback text.
    """
    carried = [
        (enrichment, detection)
        for detection in group
        if (enrichment := Enrichment.from_detection(detection)) is not None
    ]
    if not carried:
        return None
    # Sorted rather than `min`-ed because the ORDER is load-bearing twice over: the winner is
    # first, and the action union is capped, so precedence also decides whose next steps
    # survive the cap. Python's sort is stable, so equal keys keep their order within the
    # group (insertion order from bucketing) and the result stays deterministic.
    carried.sort(key=lambda pair: (pair[0].precedence, -detection_margin(pair[1])))
    winner = carried[0][0]
    return winner.with_union_of(enrichment for enrichment, _ in carried)


@dataclass(frozen=True)
class Explanation:
    """The three analyst-facing fields an alert carries beyond its score.

    Bundled because they are decided together and by the same rule: either an enrichment
    won the bucket and supplies all three, or none did and all three fall back.
    """

    threat_class: str
    rationale: str
    recommended_action: str


def narrate(
    group: Sequence[DetectionResult],
    *,
    rationale: str,
    asset_id: str,
    window_start: datetime,
    strongest: DetectionResult,
) -> Explanation:
    """Turn a bucket's promoted enrichment into what the analyst reads.

    Shared with ``severity_from_priority`` and ``promote_enrichment`` for the same reason:
    what an alert *says* is a product convention, not a property of whichever scoring
    formula produced its number (ADR-0032 D5). Leaving it in each engine had already let
    the two copies drift apart stylistically while saying the same thing, and it re-typed
    ``PrioritizedAlert``'s own ``threat_class`` default as a literal in two adapters.
    """
    fallback_action = (
        f"Inspect {asset_id} around {window_start.isoformat()}; review {strongest.method} evidence."
    )
    enrichment = promote_enrichment(group)
    if enrichment is None:
        return Explanation(UNCLASSIFIED, rationale, fallback_action)
    return Explanation(
        threat_class=enrichment.threat_class,
        rationale=f"{rationale} {enrichment.rationale_clause()}",
        # A rule may name a threat without suggesting a next step; the generated line is
        # still better than an empty field.
        recommended_action=enrichment.action_text() or fallback_action,
    )


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
