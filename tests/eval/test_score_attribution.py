"""Attribution: a finding is judged against the incident on **its own asset** (ADR-0026).

Every test here is a number that was wrong or absent before. The headline is
``test_a_detection_on_an_innocent_device_at_a_faulty_moment_is_a_false_positive``: under
time-only matching that detection scored as a *correct catch*, so a fleet-wide false alarm
could be reported as perfect precision. A flow corpus is mostly innocent devices, so this is
the law that makes one measurable at all.
"""

import pytest

from iotshield.contracts import Lane
from iotshield.eval import AssetTruth, GroundTruth, Incident, score_alerts, score_detections
from tests.eval.conftest import ASSET, alert, at, detection, fleet_truth, truth_of

SCANNER = "10.0.0.24"
GATEWAY = "10.0.0.11"
STRANGER = "10.0.0.99"


# The scanner is faulty over rows 20..29; the gateway is in the roster and asserted clean.
FLEET = fleet_truth(
    Lane.FLOW,
    **{
        SCANNER: [20 <= i < 30 for i in range(40)],
        GATEWAY: [False] * 40,
    },
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
        both = fleet_truth(
            Lane.FLOW, **{SCANNER: [False, True, True], GATEWAY: [False, True, True]}
        )
        assert len(both.incidents) == 2
        metrics = score_detections(both, [detection(SCANNER, 1, 2, Lane.FLOW)])
        assert metrics.event_recall == 0.5  # one of the two faults found


def declaring(*lanes: Lane) -> GroundTruth:
    """A generated corpus's shape: one cause, one incident per lane that carried it.

    Post-ADR-0028 D2 this is a list rather than one interval with a lane set, and the shared
    ``fault_id`` is what still identifies them as projections of a single compromise.
    """
    return GroundTruth.from_assets(
        [
            AssetTruth.from_incidents(
                SCANNER,
                {lane: [at(second) for second in range(40)] for lane in lanes},
                [
                    Incident(
                        asset_id=SCANNER,
                        lane=lane,
                        start=at(20),
                        end=at(29),
                        kind="compromise",
                        fault_id=f"{SCANNER}:compromise:20",
                    )
                    for lane in lanes
                ],
            )
        ]
    )


class TestLaneRecall:
    def test_it_measures_each_declared_lane_separately(self) -> None:
        # A three-lane compromise caught only on flow. Post-ADR-0028 D2 the headline event
        # recall is 1/3 rather than a perfect 1.00, because the three projections are three
        # incidents and two of them were genuinely missed — the honest statement that per-lane
        # recall previously had to be bolted on to make.
        truth = declaring(Lane.FLOW, Lane.LOG, Lane.TELEMETRY)
        metrics = score_detections(truth, [detection(SCANNER, 20, 29, Lane.FLOW)])
        assert metrics.incidents == 3
        assert metrics.event_recall == pytest.approx(1 / 3)
        assert metrics.lane_recall == {
            Lane.TELEMETRY: 0.0,
            Lane.LOG: 0.0,
            Lane.FLOW: 1.0,
        }

    def test_the_three_projections_are_still_one_fault(self) -> None:
        # Which is why the alert level's denominator is `faults` and not `incidents`: an analyst
        # investigates the compromised camera once.
        truth = declaring(Lane.FLOW, Lane.LOG, Lane.TELEMETRY)
        assert len(truth.faults) == 1

    def test_a_detection_in_the_wrong_lane_is_a_false_positive(self) -> None:
        # The asset is *observed* on both modalities but only faulty on one, which is the
        # configuration that makes the wrong-lane detection scoreable rather than merely
        # unmeasurable — the telemetry clock exists, and every instant on it is clean.
        truth = GroundTruth.from_assets(
            [
                AssetTruth.from_incidents(
                    SCANNER,
                    {lane: [at(s) for s in range(40)] for lane in (Lane.FLOW, Lane.TELEMETRY)},
                    [
                        Incident(
                            asset_id=SCANNER,
                            lane=Lane.FLOW,
                            start=at(20),
                            end=at(29),
                            fault_id=f"{SCANNER}:scan:20",
                        )
                    ],
                )
            ]
        )
        metrics = score_detections(truth, [detection(SCANNER, 20, 29, Lane.TELEMETRY)])
        # Right device, right moment, wrong modality. Under lane-blind event matching this
        # scored as a correct catch; the corpus says telemetry showed nothing, so it is a false
        # alarm that happened to land at a busy time (ADR-0028 D2).
        assert metrics.event_recall == 0.0
        assert metrics.event_precision == 0.0
        assert metrics.off_lane_detections == 1
        assert metrics.misattributed_detections == 0  # the *asset* was right
        assert metrics.lane_recall == {Lane.FLOW: 0.0, Lane.TELEMETRY: None}

    def test_a_detection_on_a_lane_with_no_clock_is_not_scored_at_all(self) -> None:
        # The lane twin of an unlabelled asset: no instants to be wrong about, so counting it in
        # event precision alone would be a one-sided penalty nothing disclosed (ADR-0028 D4).
        truth = declaring(Lane.FLOW)
        metrics = score_detections(truth, [detection(SCANNER, 20, 29, Lane.LOG)])
        assert metrics.unlabelled_lane_detections == 1
        assert not metrics.lanes[Lane.FLOW].scoreable_detections
        assert metrics.event_precision is None

    def test_a_converted_corpus_reports_the_one_lane_it_covers(self) -> None:
        # It no longer reports nothing: a single-modality dataset's incidents name that modality,
        # which is true, so this is a defined rate for the lane that exists rather than an empty
        # dict standing in for "nobody said".
        metrics = score_detections(FLEET, [detection(SCANNER, 20, 29, Lane.FLOW)])
        assert set(metrics.lane_recall) == {Lane.FLOW}


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
        assert metrics.off_lane_detections == 0
        assert metrics.unlabelled_detections == 0
        assert metrics.unlabelled_lane_detections == 0
        assert metrics.clean_assets == 0 and metrics.clean_assets_detected == 0

    def test_one_lane_means_the_per_lane_rates_are_the_pooled_ones(self) -> None:
        """The one deliberate exception to "inert" (ADR-0028 D2, D4).

        ``lane_recall`` used to be ``{}`` here, because a converted corpus declared no lanes and
        the empty set meant "no claim". Now every incident names a lane, so a single-lane corpus
        reports that lane — and its rates are *identical* to the pooled ones by construction,
        which is the strongest possible statement that per-lane counting changed no published
        single-lane number.
        """
        truth = truth_of([20 <= i < 60 for i in range(100)])
        metrics = score_detections(truth, [detection(ASSET, 20, 59, Lane.TELEMETRY)])
        assert set(metrics.lanes) == {Lane.TELEMETRY}
        lane = metrics.lanes[Lane.TELEMETRY].rates()
        assert lane["event_precision"] == metrics.event_precision
        assert lane["event_recall"] == metrics.event_recall
        assert lane["point_precision"] == metrics.point_precision
        assert lane["point_recall"] == metrics.point_recall
