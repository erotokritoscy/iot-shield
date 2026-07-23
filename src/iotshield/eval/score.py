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

import bisect
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from iotshield.contracts import DetectionResult, PrioritizedAlert
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


@dataclass(frozen=True)
class DetectorMetrics:
    """How well the detectors found the faults — level 1.

    Ratios are ``float | None``; ``None`` means undefined (no incidents to recall, or no
    detections to be precise about), never zero.
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


@dataclass(frozen=True)
class AlertMetrics:
    """What the analyst's queue looked like — level 2."""

    alerts: int
    alerts_per_incident: float | None
    incident_coverage: float | None
    precision_at_k: dict[int, float | None]
    mean_rank_of_true: float | None


def _merged_windows(
    detections: Sequence[DetectionResult],
) -> tuple[list[datetime], list[datetime]]:
    """Detection windows merged into disjoint spans, as parallel sorted (starts, ends).

    Point scoring asks "is this instant covered?" once per labelled row, and a detector
    that does not merge its own judgments can emit a detection per row — so the naive
    scan is quadratic in recording length (measured: 10.5M comparisons over one corpus).
    Merging once lets each row answer by binary search instead.
    """
    starts: list[datetime] = []
    ends: list[datetime] = []
    for start, end in sorted((d.window_start, d.window_end) for d in detections):
        if ends and start <= ends[-1]:
            ends[-1] = max(ends[-1], end)
        else:
            starts.append(start)
            ends.append(end)
    return starts, ends


def _coverage(
    truth: GroundTruth, detections: Sequence[DetectionResult]
) -> Iterator[tuple[bool, bool]]:
    """Yield ``(is_anomalous, is_covered)`` per labelled row.

    The span containing an instant, if any, is the last one starting at or before it —
    ``bisect_right`` finds it in log time. Both comparisons are inclusive, which is
    ``truth.overlaps``'s closed-interval rule specialized to a zero-length window.
    """
    starts, ends = _merged_windows(detections)
    for moment, flagged in truth.timeline:
        index = bisect.bisect_right(starts, moment) - 1
        yield flagged, index >= 0 and moment <= ends[index]


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
    for flagged, covered in _coverage(truth, detections):
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
    hits = sum(
        1
        for incident in incidents
        if any(incident.overlaps(d.window_start, d.window_end) for d in detections)
    )
    truthful = sum(
        1
        for d in detections
        if any(incident.overlaps(d.window_start, d.window_end) for incident in incidents)
    )
    event_precision = _ratio(truthful, len(detections))
    event_recall = _ratio(hits, len(incidents))

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
    covered = sum(
        1
        for incident in incidents
        if any(incident.overlaps(a.window_start, a.window_end) for a in alerts)
    )

    # precision@k is undefined when the queue is shorter than k — reporting it as 0 would
    # read as "the top 10 were all wrong" when there were only 3 alerts. None means "n/a".
    precision_at_k: dict[int, float | None] = {
        k: (sum(is_true[:k]) / k if len(alerts) >= k else None) for k in PRECISION_AT_K
    }
    true_ranks = [rank for rank, truthful in enumerate(is_true, start=1) if truthful]

    return AlertMetrics(
        alerts=len(alerts),
        alerts_per_incident=_ratio(len(alerts), len(incidents)),
        incident_coverage=_ratio(covered, len(incidents)),
        precision_at_k=precision_at_k,
        mean_rank_of_true=sum(true_ranks) / len(true_ranks) if true_ranks else None,
    )
