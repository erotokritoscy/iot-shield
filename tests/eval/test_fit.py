"""Fit protocols: what a detector learns from, what is then off limits to scoring, and
what happens when it cannot learn.

Two product stances under test.

"Optional, never enforced" — every path that cannot fit degrades to evaluating as configured
and *says so*, rather than raising. A benchmark that died because one of several detectors was
not fittable would be far less useful than one that reports which ones were.

**Disjoint by construction** — what the protocol trains on is never scored. The old rule cut at
each asset's first incident, which quietly handed an asserted-clean asset 100% of its own test
set; these tests pin that the reserved head and the scored tail cannot overlap whatever the
data looks like.
"""

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.detect import Fittable, LogBurstDetector, StubThresholdDetector, Team3ZScoreDetector
from iotshield.detect._fitted import is_fitted
from iotshield.detect.team3.params import Team3Params
from iotshield.eval import prepare_detector
from iotshield.eval.fit import FitProtocol, Reservation, reserve
from tests.eval.conftest import at, detection, fleet_truth, truth_of
from tests.team3_fixtures import make_detector


def event(second: int, value: float = 10.0, asset: str = "pump-02") -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=at(second),
        asset_id=asset,
        source_type=Lane.TELEMETRY,
        source_format="test",
        metric="Pressure",
        value=value,
        raw="x",
    )


def fittable_detector() -> Team3ZScoreDetector:
    return make_detector(["Pressure"], window=3)


def untrained_detector() -> Team3ZScoreDetector:
    """Hyperparameters, no cached fit — how a spec asks to fit from scratch (ADR-0022)."""
    return Team3ZScoreDetector(Team3Params(window_size=3, lag_steps=(1,)))


def clean_training(count: int = 80) -> list[NormalizedEvent]:
    """Enough varied normal readings for team3 to compute a baseline from."""
    return [event(i, value=10.0 + (i % 3) * 0.1) for i in range(count)]


class TestReservation:
    """The cut, and the property that makes ``none`` vs ``prefix`` a real experiment."""

    def test_a_clean_asset_no_longer_trains_on_its_entire_test_set(self) -> None:
        # The defect, stated as a test. With no incident to cut at, the old rule returned
        # every event as training and then scored the detector on those same rows.
        truth = truth_of([False] * 100)
        reservation = reserve(truth, FitProtocol.PREFIX)
        trained = {e.timestamp for e in reservation.training([event(i) for i in range(100)])}
        scored = {
            moment
            for asset in reservation.truth.assets
            for rows in asset.timelines.values()
            for moment, _ in rows
        }
        assert trained and scored
        assert not (trained & scored)

    def test_the_reserved_head_is_the_configured_fraction(self) -> None:
        reservation = Reservation.leading(truth_of([False] * 100), fraction=0.2)
        assert len(reservation.training([event(i) for i in range(100)])) == 20

    def test_the_cut_never_reaches_into_a_fault(self) -> None:
        # A fault at row 5 with a 20% reservation: the fraction would take 20 rows, so the
        # first incident is what bounds it. Training on a fault is the other half of the
        # same mistake.
        truth = truth_of([i >= 5 for i in range(100)])
        reservation = Reservation.leading(truth, fraction=0.2)
        assert [e.timestamp for e in reservation.training([event(i) for i in range(100)])] == [
            at(i) for i in range(5)
        ]

    def test_every_incident_survives_the_cut(self) -> None:
        # Nothing is labelled inside the reserved head, so no fault becomes unscoreable.
        truth = truth_of([i >= 40 for i in range(100)])
        assert len(reserve(truth, FitProtocol.PREFIX).truth.incidents) == len(truth.incidents)

    def test_a_recording_opening_mid_fault_reserves_nothing(self) -> None:
        reservation = reserve(truth_of([True] * 50), FitProtocol.PREFIX)
        assert reservation.training([event(i) for i in range(50)]) == []
        # ... and scores whole, rather than silently losing rows to a cut that never happened.
        assert reservation.truth.assets[0].clock == truth_of([True] * 50).assets[0].clock

    def test_the_cut_is_per_asset(self) -> None:
        # One host compromised early must not truncate every other host's history there —
        # on a thirty-device fleet that would discard almost the whole corpus (ADR-0026).
        truth = fleet_truth(scanner=[i >= 5 for i in range(100)], gateway=[False] * 100)
        reservation = Reservation.leading(truth, fraction=0.2)
        assert reservation.cutoffs["scanner"] == at(5)
        assert reservation.cutoffs["gateway"] == at(20)

    def test_events_on_an_unlabelled_asset_stay_trainable(self) -> None:
        # No claim of a fault is no reason to withhold it, and it can never leak because it
        # is never scored.
        reservation = Reservation.leading(truth_of([False] * 100), fraction=0.2)
        outside = [event(i, asset="not-in-roster") for i in range(100)]
        assert len(reservation.training(outside)) == 100

    def test_findings_inside_the_reserved_head_are_not_scored(self) -> None:
        reservation = Reservation.leading(truth_of([False] * 100), fraction=0.2)
        kept = reservation.scoreable([detection(start=0, end=1), detection(start=50, end=51)])
        assert [d.window_start for d in kept] == [at(50)]

    def test_a_finding_straddling_the_boundary_is_kept(self) -> None:
        # Judged on the part that counts rather than discarded whole.
        reservation = Reservation.leading(truth_of([False] * 100), fraction=0.2)
        assert len(reservation.scoreable([detection(start=10, end=30)])) == 1

    def test_protocol_none_reserves_nothing(self) -> None:
        reservation = reserve(truth_of([False] * 100), FitProtocol.NONE)
        assert reservation.cutoffs == {}
        assert len(reservation.scoreable([detection(start=0, end=1)])) == 1


class TestOptionality:
    def test_unfittable_detectors_are_used_as_configured(self) -> None:
        for detector in (StubThresholdDetector(threshold=80.0), LogBurstDetector()):
            assert not isinstance(detector, Fittable)
            outcome = prepare_detector(
                detector, protocol=FitProtocol.PREFIX, training=clean_training()
            )
            assert outcome.detector is detector
            assert outcome.fitted is False
            # Never *asked* to fit, so it must not drag the run's fitted count down.
            assert outcome.attempted is False
            assert outcome.note is not None and "not fittable" in outcome.note

    def test_protocol_none_never_fits_even_a_fittable_detector(self) -> None:
        detector = fittable_detector()
        outcome = prepare_detector(detector, protocol=FitProtocol.NONE, training=clean_training())
        assert outcome.detector is detector and outcome.fitted is False
        assert outcome.note is None  # not fitting was the instruction, not a fallback

    def test_no_clean_prefix_degrades_with_a_note(self) -> None:
        outcome = prepare_detector(fittable_detector(), protocol=FitProtocol.PREFIX, training=[])
        assert outcome.fitted is False and outcome.attempted is True
        assert outcome.note is not None and "no clean prefix" in outcome.note

    def test_a_refused_fit_degrades_with_the_reason(self) -> None:
        # A detector refusing its training data is a statement about the data, not a crash.
        # Here every reading is the constant `event()` default, so the baseline is degenerate
        # (MAD at the floor) and team3 declines to fit it. This used to be spelled with two
        # assets, back when team3 refused a fleet outright; since ADR-0034 it fits per asset,
        # so a fleet is no longer a refusal and the degenerate data is what does the work.
        training = [event(i) for i in range(30)]
        outcome = prepare_detector(
            fittable_detector(), protocol=FitProtocol.PREFIX, training=training
        )
        assert outcome.fitted is False and outcome.attempted is True
        assert outcome.note is not None and "fit refused" in outcome.note


class TestUntrained:
    """A detector with hyperparameters and no cached fit (ADR-0022).

    The harness must never crash on one and never silently score it as a bad detector:
    both readings are wrong in opposite directions, so every path reports which it is.
    """

    def test_protocol_none_leaves_it_unable_to_judge_and_says_why(self) -> None:
        outcome = prepare_detector(
            untrained_detector(), protocol=FitProtocol.NONE, training=clean_training()
        )
        assert is_fitted(outcome.detector) is False
        assert outcome.note is not None and "judged nothing" in outcome.note

    def test_a_prefix_fit_makes_it_able_to_judge(self) -> None:
        outcome = prepare_detector(
            untrained_detector(), protocol=FitProtocol.PREFIX, training=clean_training()
        )
        assert outcome.fitted is True and is_fitted(outcome.detector) is True

    def test_a_configured_detector_can_always_judge(self) -> None:
        # The distinction that matters: "not fitted" and "cannot judge" are different, and
        # a shipped artifact under a failed protocol is still perfectly able to run.
        outcome = prepare_detector(fittable_detector(), protocol=FitProtocol.PREFIX, training=[])
        assert outcome.fitted is False and is_fitted(outcome.detector) is True


class TestFitting:
    def test_a_successful_fit_returns_a_new_detector(self) -> None:
        original = fittable_detector()
        outcome = prepare_detector(original, protocol=FitProtocol.PREFIX, training=clean_training())
        assert outcome.fitted is True and outcome.attempted is True
        # A NEW object: the port's determinism law forbids hidden fitted state, and one
        # case's fit must never leak into the next.
        assert outcome.detector is not original

    def test_fitting_leaves_the_original_untouched(self) -> None:
        original = fittable_detector()
        before = original.model_for("pump-02").threshold
        training = [event(i, value=10.0 + (i % 5) * 2.0) for i in range(80)]
        prepare_detector(original, protocol=FitProtocol.PREFIX, training=training)
        assert original.model_for("pump-02").threshold == before


def test_holdout_is_not_offered_at_all() -> None:
    """No bundle format can carry a holdout recording, so the option is absent, not broken.

    Pinned as a test because the failure mode was a *silently* impossible choice: the CLI, the
    API and the dashboard all advertised it, and it could only ever print a table of zeros
    with one line of explanation.
    """
    assert [protocol.value for protocol in FitProtocol] == ["none", "prefix"]
