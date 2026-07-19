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

Two things are deliberately NOT scored here, because no answer key exists for them:

- **Calibration** — whether ``priority_score`` is the *right* number, or whether alert A is
  more urgent than alert B. A corpus records that a fault happened, never how much it
  mattered; that needs analyst feedback (deferred, week-2 design §8). We measure only the
  prioritizer's *discrimination*: can it tell true from false and push true upward?
- **Corroboration** — whether cross-lane agreement improves the queue. Needs a labelled
  *multimodal* corpus, which does not exist yet; on a single-lane corpus the factor is
  pinned at 1.0 and contributes nothing.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from iotshield.contracts import DetectionResult, PrioritizedAlert
from iotshield.eval.truth import GroundTruth, Incident

PRECISION_AT_K = (1, 5, 10)


def _f1(precision: float, recall: float) -> float:
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True)
class DetectorMetrics:
    """How well the detectors found the faults — level 1."""

    incidents: int
    detections: int
    incidents_hit: int
    detections_hitting: int
    point_precision: float
    point_recall: float
    point_f1: float
    event_precision: float
    event_recall: float
    event_f1: float
    detections_per_incident: float
    mean_latency_seconds: float | None


@dataclass(frozen=True)
class AlertMetrics:
    """What the analyst's queue looked like — level 2."""

    alerts: int
    alerts_true: int
    incidents_covered: int
    alerts_per_incident: float
    incident_coverage: float
    precision_at_k: dict[int, float | None]
    mean_rank_of_true: float | None


def _covers(detections: Sequence[DetectionResult], moment: datetime) -> bool:
    """Whether any detection window contains this instant (closed on both ends)."""
    return any(d.window_start <= moment <= d.window_end for d in detections)


def score_detections(truth: GroundTruth, detections: Sequence[DetectionResult]) -> DetectorMetrics:
    """Score the detector level: point coverage, event matching, fragmentation, latency.

    The event rule is what makes fragmentation visible, and it is deliberately asymmetric:
    **recall** credits an incident once no matter how many detections hit it, while
    **precision** judges every detection on its own. So 91 fragments of one real fault score
    full recall and ~0.01 precision — the analyst's experience (91 things to look at, one
    of them worth it), not the row-wise view that would call it near-perfect.
    """
    incidents = truth.incidents

    # Point level: per labelled row, is it inside any detection window?
    true_positive = false_positive = false_negative = 0
    for moment, flagged in truth.timeline:
        covered = _covers(detections, moment)
        if flagged and covered:
            true_positive += 1
        elif flagged:
            false_negative += 1
        elif covered:
            false_positive += 1
    point_precision = _ratio(true_positive, true_positive + false_positive)
    point_recall = _ratio(true_positive, true_positive + false_negative)

    # Event level: an incident is hit if ANY detection overlaps it; a detection is true if
    # it overlaps ANY incident.
    hits = [
        incident
        for incident in incidents
        if any(incident.overlaps(d.window_start, d.window_end) for d in detections)
    ]
    truthful = [
        d
        for d in detections
        if any(incident.overlaps(d.window_start, d.window_end) for incident in incidents)
    ]
    event_precision = _ratio(len(truthful), len(detections))
    event_recall = _ratio(len(hits), len(incidents))

    return DetectorMetrics(
        incidents=len(incidents),
        detections=len(detections),
        incidents_hit=len(hits),
        detections_hitting=len(truthful),
        point_precision=point_precision,
        point_recall=point_recall,
        point_f1=_f1(point_precision, point_recall),
        event_precision=event_precision,
        event_recall=event_recall,
        event_f1=_f1(event_precision, event_recall),
        detections_per_incident=_ratio(len(detections), len(incidents)),
        mean_latency_seconds=_mean_latency(incidents, detections),
    )


def _mean_latency(
    incidents: Sequence[Incident], detections: Sequence[DetectionResult]
) -> float | None:
    """Mean seconds from an incident starting to the first detection that touches it.

    Only over incidents actually caught — averaging in a miss as "infinitely late" would
    conflate two different failures (missed it vs. found it slowly), and recall already
    reports the misses. ``None`` when nothing was caught.
    """
    latencies: list[float] = []
    for incident in incidents:
        touching = [d for d in detections if incident.overlaps(d.window_start, d.window_end)]
        if touching:
            first = min(d.window_start for d in touching)
            latencies.append(max(0.0, (first - incident.start).total_seconds()))
    return sum(latencies) / len(latencies) if latencies else None


def score_alerts(truth: GroundTruth, alerts: Sequence[PrioritizedAlert]) -> AlertMetrics:
    """Score the alert level: queue size per incident, coverage, and ranking quality.

    ``alerts`` must already be in the engine's ranked order (the port ranks by priority),
    since precision@k and mean-rank read positions in that queue.
    """
    incidents = truth.incidents
    is_true = [
        any(incident.overlaps(alert.window_start, alert.window_end) for incident in incidents)
        for alert in alerts
    ]
    covered = [
        incident
        for incident in incidents
        if any(incident.overlaps(a.window_start, a.window_end) for a in alerts)
    ]

    # precision@k is undefined when the queue is shorter than k — reporting it as 0 would
    # read as "the top 10 were all wrong" when there were only 3 alerts. None means "n/a".
    precision_at_k: dict[int, float | None] = {
        k: (sum(is_true[:k]) / k if len(alerts) >= k else None) for k in PRECISION_AT_K
    }
    true_ranks = [rank for rank, truthful in enumerate(is_true, start=1) if truthful]

    return AlertMetrics(
        alerts=len(alerts),
        alerts_true=sum(is_true),
        incidents_covered=len(covered),
        alerts_per_incident=_ratio(len(alerts), len(incidents)),
        incident_coverage=_ratio(len(covered), len(incidents)),
        precision_at_k=precision_at_k,
        mean_rank_of_true=sum(true_ranks) / len(true_ranks) if true_ranks else None,
    )
