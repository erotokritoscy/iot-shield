"""Shared AlertEngine machinery: correlate, rank, and label — implemented once.

Both alert-engine adapters follow the same skeleton: group an asset's detections into
episodes by proximity, build one alert per episode, then rank by priority. Only the
per-bucket scoring differs. That skeleton (``correlate_and_rank``), the bucketing
and margin measure it builds on, and the analyst-facing priority -> severity mapping
(``severity_from_priority``, a product convention independent of engine) live here
once — the alert-side analog of ``normalize._linewise``. Each bucket is non-empty
and single-asset.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

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


def distinct_judgments(group: Sequence[DetectionResult]) -> int:
    """How many distinct claims a bucket holds, which is what ``volume`` measures (ADR-0033).

    ``(lane, window_start, window_end)`` is the whole of what a detector asserts about the
    world (ADR-0028 D1), so two rows describing the same lane over the same window are one
    claim however many detectors or rules produced them. ``method`` is deliberately not in
    the key — including it would count *producers*, which is the defect this replaces: two
    rule instances matching one flow record, or two presets of one adapter judging one
    reading, used to buy corroboration that no additional evidence supported.

    Shared by both engines so they cannot disagree about what "more of this" means.
    """
    return len({(item.lane, item.window_start, item.window_end) for item in group})


def counted(rows: int, judgments: int) -> str:
    """How a rationale reports volume's input: rows, and distinct claims when they differ.

    Naming the distinct count only when it differs keeps the common case terse and makes
    the interesting one — several detectors agreeing about one window — legible rather
    than looking like an arithmetic error.
    """
    if rows == judgments:
        return f"{rows} detection(s)"
    return f"{rows} detection(s) ({judgments} distinct)"


def detection_margin(detection: DetectionResult) -> float:
    """How far a detection cleared its threshold (score / threshold); 2.0 when the
    threshold is non-positive, so a degenerate detector still reads as anomalous."""
    if detection.threshold > 0:
        return detection.score / detection.threshold
    return 2.0


def severity_from_priority(priority: float) -> AlertSeverity:
    """Map a 0-100 priority to its analyst-facing label. A shared convention: a 70
    reads as CRITICAL regardless of which engine produced it.

    The bands are absolute, and each band is ``[floor, next floor)`` — inclusive on the
    left. What is *shared* is the mapping, not the reachable range: an engine occupies
    only part of this scale, so a severity is comparable between two alerts of one engine
    and only loosely between engines.

    - ``weighted`` spans LOW..CRITICAL. Its factors cap at ``4.0 x 2.0 x 1.6 x 1.3 =
      16.64``, and the saturating transform maps that to **88.66**, so EMERGENCY is out
      of reach by construction.
    - ``stub`` spans LOW..EMERGENCY, and reaches 100 when several detections agree.

    Widening either range is a scoring change, not a mapping change: raise a factor cap or
    ``_SATURATION`` in the engine, never a floor here, or the two engines stop agreeing
    about what a 70 means.
    """
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


MAX_BUCKET_SPAN = timedelta(minutes=10)
"""How far one alert may stretch, however tightly its detections chain (ADR-0033).

Proximity grouping is *single-link*: A joins B and B joins C puts all three in one alert
even when A and C are far apart. Without a ceiling a slow drizzle of detections chains
indefinitely and an operator is handed one alert spanning hours, which is not an incident
they can act on. Ten minutes is a product judgment about what reads as *one* thing, not a
derived quantity; it is deliberately larger than the default five-minute ``bucket`` so the
cap only bites on genuine chains rather than on ordinary corroboration.
"""


def bucket_by_asset_window(
    detections: Sequence[DetectionResult],
    bucket: timedelta,
    *,
    max_span: timedelta = MAX_BUCKET_SPAN,
) -> list[list[DetectionResult]]:
    """Group an asset's detections into episodes by PROXIMITY (ADR-0033).

    ``bucket`` is the largest gap that still counts as the same episode: walking an asset's
    detections in time order, a new episode opens when this one starts more than ``bucket``
    after the furthest point the current episode has reached.

    Why not a floored grid, which is what this did before. A grid snaps ``window_start``
    onto fixed multiples of ``bucket`` since the epoch, so grouping became a question of
    *where* an incident fell rather than how close its detections were: two detections
    seconds apart straddling a grid line never corroborated, while two nearly ``bucket``
    apart did. Corroboration is the engine's headline factor, so that made a published
    priority a property of the wall clock — the same three detections scored CRITICAL at
    12:00:00 and split into HIGH + MEDIUM at 12:04:40. It also read only ``window_start``,
    so a long collective span did not absorb a detection lying *inside* it and one incident
    became two alerts with overlapping ranges.

    Comparing against the running ``max_end`` rather than the previous ``window_start``
    is what fixes containment: an enclosed detection yields a negative gap and joins.
    Sorting first is what keeps the result independent of input order, which the grid got
    for free and a walk has to earn.
    """
    by_asset: dict[str, list[DetectionResult]] = {}
    for detection in detections:
        by_asset.setdefault(detection.asset_id, []).append(detection)

    groups: list[list[DetectionResult]] = []
    for series in by_asset.values():
        ordered = sorted(series, key=lambda item: (item.window_start, item.window_end))
        current: list[DetectionResult] = []
        start = end = ordered[0].window_start
        for detection in ordered:
            if current and (
                detection.window_start - end > bucket
                or max(end, detection.window_end) - start > max_span
            ):
                groups.append(current)
                current = []
            if not current:
                start, end = detection.window_start, detection.window_end
            else:
                end = max(end, detection.window_end)
            current.append(detection)
        groups.append(current)
    return groups


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
