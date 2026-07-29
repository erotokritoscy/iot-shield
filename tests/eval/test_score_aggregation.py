"""What a corpus-level number is made of, and the two ways it used to be wrong.

Both defects here shared one cause: a rate was computed per case and then *averaged*. A rate
is undefined on a clean recording, averaging skips undefined values, and so the recordings a
corpus includes specifically to measure false alarms reached no headline at all. Pooling the
counts is the fix, and it removes an unstated macro-average at the same time.

The third test is a different blindness in the same table: interval matching asks *whether* a
detection touched a fault, never how much of the window was fault.
"""

from iotshield.contracts import Lane
from iotshield.eval import score_alerts, score_detections
from iotshield.eval.report import CaseReport, SpecReport
from tests.eval.conftest import ASSET, alert, at, detection, fleet_truth, truth_of


def case(name: str, detections: list, alerts: list, flags: list[bool]) -> CaseReport:
    truth = truth_of(flags)
    return CaseReport.build(
        case=name,
        detector_metrics=score_detections(truth, detections),
        alert_metrics=score_alerts(truth, alerts, detections),
        fitted=False,
        notes=[],
    )


def spec_over(*cases: CaseReport) -> SpecReport:
    return SpecReport(spec="s", detectors=["d"], alert_engine="stub", cases=list(cases))


class TestCleanRecordingsReachTheHeadline:
    """A clean case has no recall to report — but a false alarm on it is still wrong."""

    def _faulty(self) -> CaseReport:
        return case(
            "faulty", [detection(start=1, end=2)], [alert(start=1, end=2)], [False, True, True]
        )

    def test_a_spec_that_false_alarms_on_a_clean_case_scores_below_one_that_does_not(
        self,
    ) -> None:
        # THE regression. Both specs are perfect on the faulty case; one then fires four
        # times on a recording the corpus asserts is clean. Averaging per-case rates gave
        # them identical event and point F1, because every rate on the clean case is None.
        quiet = spec_over(self._faulty(), case("clean", [], [], [False] * 4))
        noisy = spec_over(
            self._faulty(),
            case("clean", [detection(start=i, end=i) for i in range(4)], [], [False] * 4),
        )
        assert quiet.summary()["event_f1"] == 1.0
        noisy_f1 = noisy.summary()["event_f1"]
        assert noisy_f1 is not None and noisy_f1 < 1.0
        assert noisy.summary()["event_precision"] == 1 / 5  # one true of five detections

    def test_the_clean_cases_own_row_still_reads_undefined(self) -> None:
        # Only the aggregate changes: a rate that does not exist for one recording must not
        # be printed as a zero it never earned (ADR-0021 D2).
        clean = case("clean", [detection(start=0, end=0)], [], [False] * 4)
        assert clean.event_recall is None and clean.event_f1 is None
        assert clean.event_precision == 0.0  # it DID make a prediction, and it was wrong

    def test_a_big_case_outweighs_a_small_one(self) -> None:
        """Pooling also removes an unstated macro-average.

        Ten incidents missed in one recording and one caught in another is 1/11, not the 0.5
        an average of per-case recalls reports.
        """
        many = case("many", [], [], [True, False] * 10)
        one = case("one", [detection(start=0, end=0)], [], [True])
        assert len(many.incidents * [0]) == 10
        assert spec_over(many, one).summary()["event_recall"] == 1 / 11


class TestRatesUseOnlyScoreableFindings:
    def test_fragmentation_ignores_findings_the_corpus_cannot_adjudicate(self) -> None:
        """The report printed a line promising exactly this and did not do it.

        One correct detection on the labelled asset plus 24 on a host outside the roster gave
        `detections_per_incident` 25.0, beside the console line "24 finding(s) on assets the
        corpus does not label: excluded from every rate".
        """
        truth = truth_of([False, True, True])
        noise = [detection(asset_id="unlabelled", start=i, end=i) for i in range(24)]
        metrics = score_detections(truth, [detection(start=1, end=2), *noise])
        assert metrics.detections == 25  # the honest raw output count is unchanged
        assert metrics.unlabelled_detections == 24
        assert metrics.detections_per_incident == 1.0

    def test_the_alert_queue_length_ignores_them_too(self) -> None:
        truth = truth_of([False, True, True])
        alerts = [alert(start=1, end=2), *[alert(asset_id="unlabelled") for _ in range(9)]]
        metrics = score_alerts(truth, alerts, [])
        assert metrics.alerts == 10 and metrics.alerts_per_incident == 1.0


class TestWindowWidth:
    """Interval matching is blind to width; ``ovl`` is what sees it."""

    def test_a_recording_wide_detection_scores_a_perfect_row_and_a_terrible_overlap(
        self,
    ) -> None:
        # Every event-level number here is the best possible, from a detector carrying no
        # information at all. Only the overlap dissents.
        truth = truth_of([20 <= i < 60 for i in range(100)])
        metrics = score_detections(truth, [detection(start=0, end=99)])
        assert metrics.event_precision == 1.0
        assert metrics.event_recall == 1.0
        assert metrics.detections_per_incident == 1.0
        assert metrics.mean_latency_seconds == 0.0
        # Rows 20..59 is a 39-second closed interval inside a 99-second window.
        assert metrics.mean_true_detection_overlap == 39 / 99

    def test_a_tight_detection_scores_a_full_overlap(self) -> None:
        truth = truth_of([20 <= i < 60 for i in range(100)])
        metrics = score_detections(truth, [detection(start=20, end=59)])
        assert metrics.mean_true_detection_overlap == 1.0

    def test_a_point_detection_that_lands_inside_is_wholly_inside(self) -> None:
        # A zero-width window has no width to divide by, and it is entirely within the fault
        # by definition.
        truth = truth_of([20 <= i < 60 for i in range(100)])
        assert (
            score_detections(truth, [detection(start=30, end=30)]).mean_true_detection_overlap
            == 1.0
        )

    def test_a_false_detection_does_not_dilute_the_overlap(self) -> None:
        # Overlap is measured over the TRUE detections: a false one has zero overlap by
        # definition, so averaging it in would just restate precision in a second column.
        truth = truth_of([20 <= i < 60 for i in range(100)])
        metrics = score_detections(
            truth, [detection(start=20, end=59), detection(start=80, end=90)]
        )
        assert metrics.event_precision == 0.5
        assert metrics.mean_true_detection_overlap == 1.0

    def test_the_overlap_only_counts_faults_on_the_detections_own_asset(self) -> None:
        # A window must be measured against its own device's incidents; crediting it for
        # overlapping another device's fault is the ADR-0026 error one metric further along.
        # The gateway's window covers the scanner's whole fault and is still worth nothing.
        truth = fleet_truth(scanner=[20 <= i < 60 for i in range(100)], gateway=[False] * 100)
        metrics = score_detections(truth, [detection(asset_id="gateway", start=0, end=99)])
        assert metrics.event_precision == 0.0
        assert metrics.mean_true_detection_overlap is None  # no true detection to measure


def test_point_metrics_score_each_lane_against_its_own_rows() -> None:
    """The trimodal defect: a pooled clock hands every lane to whichever samples fastest.

    Ten telemetry rows and one flow row over the same ten seconds. A flow detection covering
    the whole span used to be charged ten telemetry false positives for rows it was never
    asked about; now it is judged against the one flow row that exists.
    """
    from iotshield.eval import AssetTruth, GroundTruth

    truth = GroundTruth.from_assets(
        [
            AssetTruth.from_incidents(
                ASSET,
                {Lane.TELEMETRY: [at(i) for i in range(10)], Lane.FLOW: [at(5)]},
                [],
            )
        ]
    )
    metrics = score_detections(truth, [detection(start=0, end=9, lane=Lane.FLOW)])
    # One flow row covered, and no telemetry row touched: 1 false positive, not 11.
    assert metrics.lanes[Lane.FLOW].point_false_positive == 1
