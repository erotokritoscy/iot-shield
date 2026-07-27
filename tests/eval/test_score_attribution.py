"""Attribution: a finding is judged against the incident on **its own asset** (ADR-0026).

Every test here is a number that was wrong or absent before. The headline is
``test_a_detection_on_an_innocent_device_at_a_faulty_moment_is_a_false_positive``: under
time-only matching that detection scored as a *correct catch*, so a fleet-wide false alarm
could be reported as perfect precision. A flow corpus is mostly innocent devices, so this is
the law that makes one measurable at all.
"""

from iotshield.contracts import Lane
from iotshield.eval import AssetTruth, GroundTruth, Incident, score_alerts, score_detections
from tests.eval.conftest import ASSET, alert, at, detection, fleet_truth, truth_of

SCANNER = "10.0.0.24"
GATEWAY = "10.0.0.11"
STRANGER = "10.0.0.99"


# The scanner is faulty over rows 20..29; the gateway is in the roster and asserted clean.
FLEET = fleet_truth(
    **{
        SCANNER: [20 <= i < 30 for i in range(40)],
        GATEWAY: [False] * 40,
    }
)


class TestMisattribution:
    def test_a_detection_on_an_innocent_device_at_a_faulty_moment_is_a_false_positive(
        self,
    ) -> None:
        # THE regression. Both detections overlap the only incident in time, so time-only
        # matching scored precision 1.00 with a live false alarm in the queue.
        metrics = score_detections(
            FLEET, [detection(SCANNER, 20, 29, Lane.FLOW), detection(GATEWAY, 22, 24, Lane.FLOW)]
        )
        assert metrics.detections == 2
        assert metrics.event_precision == 0.5
        assert metrics.event_recall == 1.0

    def test_the_misattribution_is_named_not_merely_counted_wrong(self) -> None:
        # Right moment, wrong device is a specific diagnosis: it separates "the detector is
        # noisy" from "the detector fired on the wrong thing", which need different fixes.
        metrics = score_detections(
            FLEET, [detection(SCANNER, 20, 29, Lane.FLOW), detection(GATEWAY, 22, 24, Lane.FLOW)]
        )
        assert metrics.misattributed_detections == 1

    def test_a_false_alarm_at_a_quiet_moment_is_not_misattribution(self) -> None:
        # Still a false positive, but a plain one — nothing was happening anywhere.
        metrics = score_detections(FLEET, [detection(GATEWAY, 0, 2, Lane.FLOW)])
        assert metrics.event_precision == 0.0
        assert metrics.misattributed_detections == 0

    def test_an_incident_is_hit_only_by_a_detection_on_its_own_asset(self) -> None:
        metrics = score_detections(FLEET, [detection(GATEWAY, 20, 29, Lane.FLOW)])
        assert metrics.event_recall == 0.0  # the scanner's fault went entirely unnoticed

    def test_latency_ignores_a_coincidence_on_another_device(self) -> None:
        # A neighbour firing early must not report a latency this detector never achieved.
        metrics = score_detections(
            FLEET, [detection(GATEWAY, 20, 20, Lane.FLOW), detection(SCANNER, 25, 29, Lane.FLOW)]
        )
        assert metrics.mean_latency_seconds == 5.0


class TestAssertedClean:
    def test_a_clean_asset_that_fires_is_reported_at_fleet_scale(self) -> None:
        metrics = score_detections(FLEET, [detection(GATEWAY, 0, 2, Lane.FLOW)])
        assert metrics.clean_assets == 1
        assert metrics.clean_assets_detected == 1

    def test_a_silent_clean_asset_is_counted_but_not_blamed(self) -> None:
        metrics = score_detections(FLEET, [detection(SCANNER, 20, 29, Lane.FLOW)])
        assert metrics.clean_assets == 1
        assert metrics.clean_assets_detected == 0
        assert metrics.event_precision == 1.0

    def test_clean_assets_alerted_is_the_alert_level_equivalent(self) -> None:
        metrics = score_alerts(FLEET, [alert(GATEWAY, 0, 2, 50.0)], ())
        assert metrics.clean_assets_alerted == 1
        assert metrics.incident_coverage == 0.0


class TestUnlabelledAssets:
    """Out of scope is not innocent: the corpus made no claim, so no rate may use it."""

    def test_a_detection_on_an_unlabelled_device_is_counted_but_not_scored(self) -> None:
        metrics = score_detections(
            FLEET, [detection(SCANNER, 20, 29, Lane.FLOW), detection(STRANGER, 22, 24, Lane.FLOW)]
        )
        assert metrics.detections == 2  # everything emitted is still reported
        assert metrics.unlabelled_detections == 1
        assert metrics.event_precision == 1.0  # judged over the one finding we can adjudicate

    def test_it_is_not_counted_as_a_misattribution_either(self) -> None:
        # Misattribution is a claim about a device we know was innocent. We do not know that.
        metrics = score_detections(FLEET, [detection(STRANGER, 22, 24, Lane.FLOW)])
        assert metrics.misattributed_detections == 0
        assert metrics.event_precision is None  # nothing scoreable: undefined, never 0.0

    def test_an_unlabelled_alert_is_removed_before_ranking(self) -> None:
        # Keeping it would force a wrong answer either way; removing it preserves the
        # relative order of every alert that CAN be judged.
        queue = [alert(STRANGER, 0, 2, 90.0), alert(SCANNER, 20, 29, 80.0)]
        metrics = score_alerts(FLEET, queue, ())
        assert metrics.alerts == 2
        assert metrics.unlabelled_alerts == 1
        assert metrics.precision_at_k[1] == 1.0  # the stranger did not displace the true one
        assert metrics.mean_rank_of_true == 1.0


class TestPerAssetClocks:
    def test_point_metrics_are_scored_against_each_assets_own_clock(self) -> None:
        # The gateway's 40 clean rows are covered by its own false detection, so they are
        # point false positives; the scanner's rows are judged only by the scanner's window.
        metrics = score_detections(
            FLEET, [detection(SCANNER, 20, 29, Lane.FLOW), detection(GATEWAY, 0, 39, Lane.FLOW)]
        )
        assert metrics.point_recall == 1.0  # every anomalous row of the scanner was covered
        assert metrics.point_precision == 10 / 50  # 10 true rows vs the gateway's 40 clean ones

    def test_two_assets_faulty_at_the_same_instant_are_two_incidents(self) -> None:
        # A single pooled timeline would merge them into one run of flags and undercount.
        both = fleet_truth(**{SCANNER: [False, True, True], GATEWAY: [False, True, True]})
        assert len(both.incidents) == 2
        metrics = score_detections(both, [detection(SCANNER, 1, 2, Lane.FLOW)])
        assert metrics.event_recall == 0.5  # one of the two faults found


def declaring(*lanes: Lane) -> GroundTruth:
    """A generated corpus's shape: one incident that declares which lanes carried it."""
    return GroundTruth.from_assets(
        [
            AssetTruth.from_incidents(
                SCANNER,
                [at(second) for second in range(40)],
                [
                    Incident(
                        asset_id=SCANNER,
                        start=at(20),
                        end=at(29),
                        kind="compromise",
                        lanes=frozenset(lanes),
                    )
                ],
            )
        ]
    )


class TestLaneRecall:
    def test_it_measures_each_declared_lane_separately(self) -> None:
        # A three-lane fault caught only on flow: the headline event recall is a perfect 1.00
        # because *something* found it, which is exactly what per-lane recall exists to
        # qualify — two of the three modalities the corpus labelled saw nothing.
        truth = declaring(Lane.FLOW, Lane.LOG, Lane.TELEMETRY)
        metrics = score_detections(truth, [detection(SCANNER, 20, 29, Lane.FLOW)])
        assert metrics.event_recall == 1.0
        assert metrics.lane_recall == {
            Lane.TELEMETRY: 0.0,
            Lane.LOG: 0.0,
            Lane.FLOW: 1.0,
        }

    def test_a_detection_in_the_wrong_lane_does_not_credit_a_declared_one(self) -> None:
        truth = declaring(Lane.FLOW)
        metrics = score_detections(truth, [detection(SCANNER, 20, 29, Lane.TELEMETRY)])
        # The incident was still caught — but not by the modality the corpus says carried it.
        assert metrics.event_recall == 1.0
        assert metrics.lane_recall == {Lane.FLOW: 0.0}

    def test_a_corpus_that_declares_no_lanes_reports_nothing(self) -> None:
        # Undefined, not zero: a converted corpus never claimed a modality.
        metrics = score_detections(FLEET, [detection(SCANNER, 20, 29, Lane.FLOW)])
        assert metrics.lane_recall == {}


class TestCorroborationEvidence:
    """What the multimodal A/B reads: priority separation, and how many lanes backed it."""

    def test_true_and_false_alert_priorities_are_reported_separately(self) -> None:
        queue = [alert(GATEWAY, 0, 2, 70.0), alert(SCANNER, 20, 29, 40.0)]
        metrics = score_alerts(FLEET, queue, ())
        assert metrics.mean_true_priority == 40.0
        assert metrics.mean_false_priority == 70.0

    def test_lanes_per_true_alert_counts_distinct_backing_lanes(self) -> None:
        backing = [
            detection(SCANNER, 20, 29, Lane.FLOW),
            detection(SCANNER, 21, 25, Lane.LOG),
            detection(SCANNER, 22, 24, Lane.LOG),
        ]
        metrics = score_alerts(FLEET, [alert(SCANNER, 20, 29, 76.0, backing=backing)], backing)
        assert metrics.mean_lanes_per_true_alert == 2.0  # distinct lanes, not detections

    def test_it_is_undefined_when_detections_are_not_supplied(self) -> None:
        metrics = score_alerts(FLEET, [alert(SCANNER, 20, 29, 76.0)], ())
        assert metrics.mean_lanes_per_true_alert is None


class TestSingleAssetEquivalence:
    """The safety net for the rewrite: one asset, one roster entry, unchanged arithmetic.

    Every new formula reduces to the old one when the roster has a single member, so the
    corpora whose numbers are already published (SKAB, the telemetry corpus) cannot move. The
    *rates* are already pinned by ``tests/eval/test_score.py``, which runs against this same
    fixture and would fail on any drift — re-asserting them here would be two copies of one
    guarantee. What is new, and only checkable here, is that none of the added counters fires
    on a single-asset corpus.
    """

    def test_the_new_counters_are_all_inert(self) -> None:
        truth = truth_of([20 <= i < 60 for i in range(100)])
        metrics = score_detections(truth, [detection(ASSET, 20, 59, Lane.TELEMETRY)])
        assert metrics.event_precision == 1.0 and metrics.event_recall == 1.0
        assert metrics.misattributed_detections == 0
        assert metrics.unlabelled_detections == 0
        assert metrics.clean_assets == 0 and metrics.clean_assets_detected == 0
        assert metrics.lane_recall == {}
