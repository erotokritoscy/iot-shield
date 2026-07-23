"""Metrics, with every expected value computed by hand.

The centrepiece is ``TestFragmentation``: the case that motivated the whole two-level split
(ADR-0021 D2). One clean detection and 91 fragments of the *same* fault score nearly
identically at the point level and catastrophically differently at the event level — which
is the difference between what a paper reports and what an analyst experiences.
"""

from iotshield.contracts import AlertSeverity, DetectionResult, Lane, PrioritizedAlert
from iotshield.eval import score_alerts, score_detections
from tests.eval.conftest import at, truth_of


def detection(start: int, end: int) -> DetectionResult:
    return DetectionResult(
        asset_id="pump-02",
        lane=Lane.TELEMETRY,
        method="test",
        score=10.0,
        threshold=1.0,
        window_start=at(start),
        window_end=at(end),
    )


def alert(start: int, end: int, priority: float) -> PrioritizedAlert:
    return PrioritizedAlert(
        asset_id="pump-02",
        severity=AlertSeverity.HIGH,
        priority_score=priority,
        rationale="test",
        detection_ids=[detection(start, end).detection_id],
        window_start=at(start),
        window_end=at(end),
    )


# 100 rows; rows 20..59 are the fault (40 anomalous rows, one incident).
FAULT = truth_of([20 <= i < 60 for i in range(100)])


class TestDetectorLevel:
    def test_one_exact_detection_scores_perfectly(self) -> None:
        metrics = score_detections(FAULT, [detection(20, 59)])
        assert metrics.incidents == 1 and metrics.detections == 1
        assert metrics.point_precision == 1.0 and metrics.point_recall == 1.0
        assert metrics.event_precision == 1.0 and metrics.event_recall == 1.0
        assert metrics.detections_per_incident == 1.0

    def test_missing_the_fault_entirely(self) -> None:
        metrics = score_detections(FAULT, [])
        assert metrics.event_recall == 0.0 and metrics.event_f1 == 0.0
        assert metrics.mean_latency_seconds is None

    def test_a_detection_on_clean_data_is_a_false_alarm(self) -> None:
        clean = truth_of([False] * 50)
        metrics = score_detections(clean, [detection(10, 12)])
        assert metrics.incidents == 0
        assert metrics.event_precision == 0.0  # nothing it could have been right about
        assert metrics.point_precision == 0.0

    def test_partial_coverage_splits_point_precision_and_recall(self) -> None:
        # Covers rows 20..39: 20 of the 40 anomalous rows, and nothing clean.
        metrics = score_detections(FAULT, [detection(20, 39)])
        assert metrics.point_precision == 1.0  # every covered row really is anomalous
        assert metrics.point_recall == 0.5  # but only half the fault was covered
        assert metrics.event_recall == 1.0  # the incident was still caught

    def test_latency_measures_from_the_incident_start(self) -> None:
        metrics = score_detections(FAULT, [detection(35, 59)])
        assert metrics.mean_latency_seconds == 15.0  # incident at 20, first detection at 35

    def test_latency_is_never_negative(self) -> None:
        # A detector whose window opens before the fault did is early, not negatively late.
        metrics = score_detections(FAULT, [detection(0, 59)])
        assert metrics.mean_latency_seconds == 0.0


FRAGMENTS = [detection(20 + i, 20 + i) for i in range(40)]


class TestFragmentation:
    """One clean detection vs per-row fragments of the same fault (ADR-0020, ADR-0021)."""

    def test_point_metrics_barely_notice(self) -> None:
        clean = score_detections(FAULT, [detection(20, 59)])
        shredded = score_detections(FAULT, FRAGMENTS)
        # Both cover exactly the fault's rows, so the row-wise view calls them identical.
        assert clean.point_f1 == shredded.point_f1 == 1.0

    def test_event_precision_collapses(self) -> None:
        shredded = score_detections(FAULT, FRAGMENTS)
        # Recall credits the incident once however many detections hit it...
        assert shredded.event_recall == 1.0
        # ...while precision judges each detection on its own: 40 shouts, one real fault.
        assert shredded.event_precision == 1.0  # each fragment IS inside the fault
        assert shredded.detections_per_incident == 40.0

    def test_fragments_outside_the_fault_are_each_false(self) -> None:
        # The IsolationForest failure shape: fragments scattered across the recording.
        scattered = [detection(i * 2, i * 2) for i in range(50)]  # 20 inside, 30 outside
        metrics = score_detections(FAULT, scattered)
        assert metrics.event_recall == 1.0
        assert metrics.event_precision == 20 / 50
        assert metrics.detections_per_incident == 50.0


class TestAlertLevel:
    def test_one_true_alert(self) -> None:
        metrics = score_alerts(FAULT, [alert(20, 59, 80.0)])
        assert metrics.alerts == 1
        assert metrics.alerts_per_incident == 1.0
        assert metrics.incident_coverage == 1.0
        assert metrics.mean_rank_of_true == 1.0

    def test_precision_at_k_reads_the_ranked_queue(self) -> None:
        # Rank 1 is false, ranks 2-3 are true: the exact failure precision@k exists to catch.
        queue = [alert(90, 95, 90.0), alert(20, 30, 80.0), alert(40, 50, 70.0)]
        metrics = score_alerts(FAULT, queue)
        assert metrics.precision_at_k[1] == 0.0
        assert metrics.mean_rank_of_true == 2.5

    def test_precision_at_k_is_none_when_the_queue_is_shorter_than_k(self) -> None:
        # Reporting 0.0 would read as "the top 10 were all wrong" when there were only 2.
        metrics = score_alerts(FAULT, [alert(20, 30, 90.0), alert(40, 50, 80.0)])
        assert metrics.precision_at_k[1] == 1.0
        assert metrics.precision_at_k[5] is None
        assert metrics.precision_at_k[10] is None

    def test_fragmented_alerts_show_up_as_alerts_per_incident(self) -> None:
        queue = [alert(20 + i * 10, 25 + i * 10, 90.0 - i) for i in range(4)]
        metrics = score_alerts(FAULT, queue)
        assert metrics.alerts_per_incident == 4.0  # one fault, four things to triage
        assert metrics.incident_coverage == 1.0  # yet coverage looks perfect

    def test_no_alerts_on_a_real_fault(self) -> None:
        metrics = score_alerts(FAULT, [])
        assert metrics.incident_coverage == 0.0
        assert metrics.mean_rank_of_true is None
