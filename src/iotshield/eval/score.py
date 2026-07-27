"""Scoring: comparing a run's output to ground truth, at two levels.

The two levels answer different questions and must never be conflated (ADR-0021 D2):

- **Detector level** — did it turn its observations into one finding per real fault? Its own
  job, since forming judgments is part of detecting.
- **Alert level** — did the alert layer group findings *across detectors* into a sane queue,
  and rank the real ones on top? A different job, at a different altitude.

Neither implies the other, because the alert engine is a *compressor*: it buckets detections
by (asset, window), so a detector that fragments badly may still produce few alerts. Only
reading both reveals what the correlation stage actually did — the compression ratio — and
only the pair distinguishes "the detector fragments" from "the alert layer is fragmenting a
clean detector".

## Everything is matched per asset (ADR-0026)

A finding is compared to an incident on **(asset, time)**, never time alone. Both contract
objects already carry ``asset_id``, so this is a gap that was only ever in the measuring
instrument: the pipeline always knew whose finding it was, and the judge threw it away.

Three consequences worth naming, because each is a number that used to be wrong or absent:

- A detection at the right time on the *wrong* device is a false positive, and
  ``misattributed_detections`` says how many there were. Under time-only matching every one
  of these scored as a correct catch.
- An asset in the roster with no incidents is *asserted clean*, so detections on it count
  against precision. ``clean_assets`` / ``clean_assets_detected`` report that at fleet scale
  — the false-alarm rate a fleet-wide threshold has never had to answer for.
- An asset **outside** the roster is not scored at all, in either direction.
  ``unlabelled_detections`` discloses how much was set aside, so a partially labelled corpus
  cannot quietly inflate or deflate a rate. See ``truth`` for why those are different states.

Two things are deliberately NOT scored here, because no answer key exists for them:

- **Calibration** — whether ``priority_score`` is the *right* number, or whether alert A is
  more urgent than alert B. A corpus records that a fault happened, never how much it
  mattered; that needs analyst feedback (deferred, week-2 design §8). We measure only the
  prioritizer's *discrimination*: can it tell true from false and push true upward?
  ``mean_true_priority`` / ``mean_false_priority`` stay inside that line — comparing the two
  populations, or comparing one spec's true-alert scores against another's over *identical*
  evidence, is discrimination. Claiming 76.4 is correct would be calibration; we never do.
- **Whether a corpus is complete.** That is the corpus's own declaration (the roster), not
  something scoring can infer.
"""

import bisect
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar
from uuid import UUID

from iotshield.contracts import DetectionResult, Lane, PrioritizedAlert
from iotshield.eval.truth import GroundTruth, Incident

PRECISION_AT_K = (1, 5, 10)


def _ratio(numerator: float, denominator: float) -> float | None:
    """The rate, or ``None`` when the denominator makes it *undefined* rather than zero.

    Load-bearing, not defensive. On a clean recording a detector that correctly emits
    nothing has no recall to report — and returning 0.0 would make it indistinguishable
    from one that missed everything, in the JSON report a CI gate reads and in the
    dashboard, not merely in a console table. ``None`` is already the harness's word for
    "not applicable" (precision@k when the queue is short, latency when nothing was
    caught), and the renderer already prints it as ``-``.
    """
    return numerator / denominator if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    """Harmonic mean, propagating undefined-ness from recall.

    Undefined only when there was nothing to find: with no incidents there is no F1 to
    speak of. A detector that found nothing *while there was something* has recall 0 and
    undefined precision (it made no predictions) — that is a score of 0, not a
    non-answer, so precision falls back to 0 there.
    """
    if recall is None:
        return None
    precision = precision or 0.0
    total = precision + recall
    return 2 * precision * recall / total if total else 0.0


def mean(values: Sequence[float]) -> float | None:
    """The mean, or ``None`` when there is nothing to average — the same rule as ``_ratio``.

    Public because ``report`` needs the identical rule when it averages a metric across
    cases; a second copy there would be a third spelling of "undefined is not zero".
    """
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class DetectorMetrics:
    """How well the detectors found the faults — level 1.

    Ratios are ``float | None``; ``None`` means undefined (no incidents to recall, or no
    detections to be precise about), never zero.

    ``detections`` counts *everything the detectors emitted*, including findings on assets
    outside the roster. The ratios that require a judgment use only the scoreable ones, and
    ``unlabelled_detections`` reconciles the two — so the raw output count stays the honest
    "how many things did this produce", while no rate is computed over a finding the corpus
    cannot adjudicate.
    """

    incidents: int
    detections: int
    point_precision: float | None
    point_recall: float | None
    point_f1: float | None
    event_precision: float | None
    event_recall: float | None
    event_f1: float | None
    detections_per_incident: float | None
    mean_latency_seconds: float | None
    # No defaults, matching every field above: there is exactly one construction site, so a
    # default would only let a metric be added here and forgotten there — reading as a
    # plausible 0 rather than failing.
    misattributed_detections: int
    unlabelled_detections: int
    clean_assets: int
    clean_assets_detected: int
    lane_recall: dict[Lane, float | None]


@dataclass(frozen=True)
class AlertMetrics:
    """What the analyst's queue looked like — level 2."""

    alerts: int
    alerts_per_incident: float | None
    incident_coverage: float | None
    precision_at_k: dict[int, float | None]
    mean_rank_of_true: float | None
    unlabelled_alerts: int
    clean_assets_alerted: int
    mean_true_priority: float | None
    mean_false_priority: float | None
    mean_lanes_per_true_alert: float | None


class _Spans:
    """Disjoint sorted spans, answering "does anything cover this?" by binary search.

    The one index both levels need. Point scoring asks it once per labelled row, and a
    detector that does not merge its own judgments can emit a detection per row — so the
    naive scan is quadratic in recording length (measured: 10.5M comparisons over one
    corpus). Merging once turns every later question into a bisection.

    Built from ``(start, end)`` pairs rather than from findings, so the *same* index serves
    the two directions that used to be separate loops: detections merged to test row
    coverage, and incidents merged to test whether a finding landed on a real fault
    anywhere. The second was a full linear scan per finding and measured 54% of
    ``score_detections`` on a 30-asset fleet.
    """

    __slots__ = ("_ends", "_starts")

    def __init__(self, spans: Iterable[tuple[datetime, datetime]]) -> None:
        self._starts: list[datetime] = []
        self._ends: list[datetime] = []
        for start, end in sorted(spans):
            if self._ends and start <= self._ends[-1]:
                self._ends[-1] = max(self._ends[-1], end)
            else:
                self._starts.append(start)
                self._ends.append(end)

    def covers(self, moment: datetime) -> bool:
        """Whether any span contains this instant — the zero-length window case."""
        return self.overlaps(moment, moment)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """Whether any span touches ``[start, end]``, closed on both ends.

        The only candidate is the last span starting at or before ``end``: spans are
        disjoint and sorted, so an earlier one ends even earlier. Both comparisons are
        inclusive, which is ``truth.overlaps``'s rule.
        """
        index = bisect.bisect_right(self._starts, end) - 1
        return index >= 0 and start <= self._ends[index]


def _coverage(
    timeline: Sequence[tuple[datetime, bool]], detections: Sequence[DetectionResult]
) -> Iterator[tuple[bool, bool]]:
    """Yield ``(is_anomalous, is_covered)`` per labelled row of ONE asset's clock."""
    spans = _Spans((d.window_start, d.window_end) for d in detections)
    for moment, flagged in timeline:
        yield flagged, spans.covers(moment)


class _Finding(Protocol):
    """The shape both levels score: something about one asset over one window.

    ``DetectionResult`` and ``PrioritizedAlert`` share it through ``_WindowedContract``, so
    the matching rules below are written once instead of once per level — which is what
    stops the two altitudes drifting apart on the one question they must answer identically.
    """

    @property
    def asset_id(self) -> str: ...
    @property
    def window_start(self) -> datetime: ...
    @property
    def window_end(self) -> datetime: ...


_FindingT = TypeVar("_FindingT", bound=_Finding)


def _by_asset(findings: Sequence[_FindingT]) -> dict[str, list[_FindingT]]:
    grouped: dict[str, list[_FindingT]] = {}
    for finding in findings:
        grouped.setdefault(finding.asset_id, []).append(finding)
    return grouped


def _incidents_of(truth: GroundTruth) -> dict[str, tuple[Incident, ...]]:
    """The asset -> incidents index ``GroundTruth`` already stores, as a lookup.

    Read once per scoring call and consulted by asset thereafter. The alternative — flatten
    with ``truth.incidents`` and compare ``asset_id`` inside the matching loop — rebuilds
    this index one comparison at a time, once per (finding, incident) pair.
    """
    return {asset.asset_id: asset.incidents for asset in truth.assets}


def _verdicts(
    findings: Sequence[_FindingT], incidents_of: Mapping[str, Sequence[Incident]]
) -> list[bool]:
    """Per finding, whether it overlaps an incident **on its own asset** — the matching rule.

    A parallel list of booleans rather than a set of ids: it needs no hashing, and both
    levels read verdicts positionally anyway (precision@k walks the ranked queue), so this
    is the shape the callers already want.
    """
    return [
        any(
            incident.overlaps(finding.window_start, finding.window_end)
            for incident in incidents_of.get(finding.asset_id, ())
        )
        for finding in findings
    ]


def _caught(
    truth: GroundTruth, grouped: Mapping[str, Sequence[_Finding]]
) -> tuple[int, float | None]:
    """How many incidents were caught, and the mean seconds until the first finding on each.

    One pass for both, because they ask the same question of the same pairs: an incident is
    *caught* exactly when some finding on its asset touches it, and its latency is that
    finding's earliest start. Computing them separately meant two full traversals, and the
    hit count was literally the length of the latency list.

    Latency averages only over incidents actually caught — averaging a miss as "infinitely
    late" would conflate two different failures (missed it vs. found it slowly), and recall
    already reports the misses. Candidates come from the incident's own asset, so a
    coincidence on another device cannot report a latency the detector never achieved.
    """
    hits = 0
    latencies: list[float] = []
    for asset in truth.assets:
        own = grouped.get(asset.asset_id, ())
        for incident in asset.incidents:
            first: datetime | None = None
            for finding in own:
                if incident.overlaps(finding.window_start, finding.window_end) and (
                    first is None or finding.window_start < first
                ):
                    first = finding.window_start
            if first is not None:
                hits += 1
                latencies.append(max(0.0, (first - incident.start).total_seconds()))
    return hits, mean(latencies)


def _lane_recall(
    truth: GroundTruth, detections: Sequence[DetectionResult]
) -> dict[Lane, float | None]:
    """Per-lane event recall, over the incidents each lane is *expected* to express.

    Only defined for a corpus whose labels declare lanes — a generated one, where the
    declaration is derived from which behaviours actually perturbed their output. A
    converted corpus (SKAB) declares none, so this is empty rather than misleadingly zero:
    "the flow lane found nothing" and "nobody said the flow lane should find anything" are
    different statements.

    Reported over the lanes the *corpus* claims, not the lanes the detectors happened to
    use, so a declared lane that no configured detector covers reads as 0.0 — which is the
    honest answer to "did this run judge everything the corpus labelled?".

    Indexed by ``(lane, asset)`` in one pass. The obvious form — filter every detection per
    lane, then scan that list per incident — costs O(lanes x detections) to build and then
    rescans every asset's detections for every incident of every other asset.
    """
    incidents = truth.incidents
    declared = {lane for incident in incidents for lane in incident.lanes}
    if not declared:
        return {}

    by_lane_asset: dict[tuple[Lane, str], list[DetectionResult]] = {}
    for detection in detections:
        by_lane_asset.setdefault((detection.lane, detection.asset_id), []).append(detection)
    expected: dict[Lane, list[Incident]] = {lane: [] for lane in sorted(declared)}
    for incident in incidents:
        for lane in incident.lanes:
            expected[lane].append(incident)

    return {
        lane: _ratio(
            sum(
                1
                for incident in lane_incidents
                if any(
                    incident.overlaps(detection.window_start, detection.window_end)
                    for detection in by_lane_asset.get((lane, incident.asset_id), ())
                )
            ),
            len(lane_incidents),
        )
        for lane, lane_incidents in expected.items()
    }


def score_detections(truth: GroundTruth, detections: Sequence[DetectionResult]) -> DetectorMetrics:
    """Score the detector level: point coverage, event matching, fragmentation, latency.

    The event rule is what makes fragmentation visible, and it is deliberately asymmetric:
    **recall** credits an incident once no matter how many detections hit it, while
    **precision** judges every detection on its own. So 91 fragments of one real fault score
    full recall and ~0.01 precision — the analyst's experience (91 things to look at, one
    of them worth it), not the row-wise view that would call it near-perfect.
    """
    incidents = truth.incidents
    roster = truth.labelled_assets
    scoreable = [d for d in detections if d.asset_id in roster]
    unlabelled = len(detections) - len(scoreable)
    grouped = _by_asset(scoreable)

    # Point level: per labelled row of each asset's OWN clock, is it inside any detection
    # window for THAT asset? A pooled clock would credit a device for instants at which it
    # was never observed.
    true_positive = false_positive = false_negative = 0
    for asset in truth.assets:
        for flagged, covered in _coverage(asset.timeline, grouped.get(asset.asset_id, [])):
            if flagged and covered:
                true_positive += 1
            elif flagged:
                false_negative += 1
            elif covered:
                false_positive += 1
    point_precision = _ratio(true_positive, true_positive + false_positive)
    point_recall = _ratio(true_positive, true_positive + false_negative)

    # Event level: an incident is hit if any detection ON ITS ASSET overlaps it; a detection
    # is true if it overlaps an incident on its OWN asset.
    hits, latency = _caught(truth, grouped)
    verdicts = _verdicts(scoreable, _incidents_of(truth))
    # Misattribution: false on its own asset, yet landing on a real fault somewhere. Asked of
    # a merged index rather than by scanning every incident per detection — that scan was
    # measured at over half of this function on a 30-asset fleet.
    elsewhere = _Spans((incident.start, incident.end) for incident in incidents)
    misattributed = sum(
        1
        for detection, truthful in zip(scoreable, verdicts, strict=True)
        if not truthful and elsewhere.overlaps(detection.window_start, detection.window_end)
    )
    event_precision = _ratio(sum(verdicts), len(scoreable))
    event_recall = _ratio(hits, len(incidents))

    clean = truth.clean_assets

    return DetectorMetrics(
        incidents=len(incidents),
        detections=len(detections),
        point_precision=point_precision,
        point_recall=point_recall,
        point_f1=_f1(point_precision, point_recall),
        event_precision=event_precision,
        event_recall=event_recall,
        event_f1=_f1(event_precision, event_recall),
        detections_per_incident=_ratio(len(detections), len(incidents)),
        mean_latency_seconds=latency,
        misattributed_detections=misattributed,
        unlabelled_detections=unlabelled,
        clean_assets=len(clean),
        clean_assets_detected=sum(1 for asset_id in clean if grouped.get(asset_id)),
        lane_recall=_lane_recall(truth, scoreable),
    )


def score_alerts(
    truth: GroundTruth,
    alerts: Sequence[PrioritizedAlert],
    detections: Sequence[DetectionResult],
) -> AlertMetrics:
    """Score the alert level: queue size per incident, coverage, and ranking quality.

    ``alerts`` must already be in the engine's ranked order (the port ranks by priority),
    since precision@k and mean-rank read positions in that queue.

    ``detections`` is the same run's detections, used to count how many lanes backed each
    true alert — the direct evidence that cross-lane corroboration fired. Required rather
    than defaulted: every real caller has them, and an optional argument would buy nothing
    but a third meaning for ``None`` in the reported metric.

    Unlabelled alerts are removed before ranking rather than counted either way. Including
    one would force a wrong answer (false is a lie about a device we never adjudicated;
    true is worse), and *removing* it preserves the relative order of every alert we can
    judge. On a fully labelled corpus nothing is removed, so this is a no-op there.
    """
    incidents = truth.incidents
    roster = truth.labelled_assets
    scoreable = [a for a in alerts if a.asset_id in roster]
    grouped = _by_asset(scoreable)

    is_true = _verdicts(scoreable, _incidents_of(truth))
    covered, _ = _caught(truth, grouped)
    true_alerts = [a for a, truthful in zip(scoreable, is_true, strict=True) if truthful]
    false_alerts = [a for a, truthful in zip(scoreable, is_true, strict=True) if not truthful]

    # precision@k is undefined when the queue is shorter than k — reporting it as 0 would
    # read as "the top 10 were all wrong" when there were only 3 alerts. None means "n/a".
    precision_at_k: dict[int, float | None] = {
        k: (sum(is_true[:k]) / k if len(scoreable) >= k else None) for k in PRECISION_AT_K
    }
    clean = truth.clean_assets

    # How many distinct lanes backed each true alert — the direct evidence that cross-lane
    # corroboration fired, rather than inferring it from the priority score it produced.
    # Only the ids on *true* alerts are ever read, so the index is narrowed to those first: a
    # run can carry tens of thousands of detections and a handful of true alerts, and on a
    # clean recording there are none at all.
    wanted = {i for alert in true_alerts for i in alert.detection_ids}
    lane_of: dict[UUID, Lane] = (
        {d.detection_id: d.lane for d in detections if d.detection_id in wanted} if wanted else {}
    )
    # An alert whose detections cannot be resolved contributes nothing rather than a zero: "we
    # could not tell how many lanes backed this" is the undefined case, and averaging it as 0
    # would understate every run where it happened.
    lanes_per_true = [
        count
        for alert in true_alerts
        if (count := float(len({lane_of[i] for i in alert.detection_ids if i in lane_of})))
    ]

    return AlertMetrics(
        alerts=len(alerts),
        alerts_per_incident=_ratio(len(alerts), len(incidents)),
        incident_coverage=_ratio(covered, len(incidents)),
        precision_at_k=precision_at_k,
        mean_rank_of_true=mean(
            [rank for rank, truthful in enumerate(is_true, start=1) if truthful]
        ),
        unlabelled_alerts=len(alerts) - len(scoreable),
        clean_assets_alerted=sum(1 for asset_id in clean if grouped.get(asset_id)),
        mean_true_priority=mean([a.priority_score for a in true_alerts]),
        mean_false_priority=mean([a.priority_score for a in false_alerts]),
        mean_lanes_per_true_alert=mean(lanes_per_true),
    )
