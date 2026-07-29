"""Correlation shape: what groups into one alert, and what `volume` counts (ADR-0033).

These pin the two decisions that used to make a published priority a property of
something other than the evidence: the *grid* (an alert's membership depended on where
the incident fell on an absolute clock) and the *row count* (redundant detections of one
observation bought corroboration). Scoring particulars stay in test_weighted.py.
"""

from datetime import UTC, datetime, timedelta

import pytest

from iotshield.alert import StubAlertEngine, WeightedAlertEngine
from iotshield.alert._correlate import MAX_BUCKET_SPAN, distinct_judgments
from iotshield.contracts import AlertSeverity, Asset, Criticality, DetectionResult, Lane

ASSETS = {
    "cam-07": Asset(
        asset_id="cam-07", name="Line-1 camera", asset_type="camera", criticality=Criticality.HIGH
    ),
    "sensor-1": Asset(
        asset_id="sensor-1", name="Sensor 1", asset_type="sensor", criticality=Criticality.LOW
    ),
}


def _detection(
    *,
    at: datetime,
    lane: Lane = Lane.TELEMETRY,
    asset_id: str = "cam-07",
    span_s: int = 0,
    score: float = 100.0,
    threshold: float = 50.0,
) -> DetectionResult:
    return DetectionResult(
        asset_id=asset_id,
        window_start=at,
        window_end=at + timedelta(seconds=span_s),
        lane=lane,
        method="test_method",
        score=score,
        threshold=threshold,
        evidence={"explanation": "a thing happened"},
    )


def _trio(base: datetime) -> list[DetectionResult]:
    """Three lanes agreeing within 25 seconds — the corroboration case."""
    return [
        _detection(at=base, lane=Lane.FLOW),
        _detection(at=base + timedelta(seconds=20), lane=Lane.LOG),
        _detection(at=base + timedelta(seconds=25), lane=Lane.TELEMETRY),
    ]


class TestGroupingIsProximityNotPosition:
    @pytest.mark.parametrize(
        "base",
        [
            datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC),  # on a 5-minute grid line
            datetime(2026, 7, 7, 12, 4, 40, tzinfo=UTC),  # straddling one
            datetime(2026, 7, 7, 12, 2, 31, tzinfo=UTC),  # nowhere near one
        ],
    )
    def test_the_same_evidence_scores_the_same_wherever_it_falls_on_the_clock(
        self, base: datetime
    ) -> None:
        """The defect this replaces: a 4m40s shift split one CRITICAL into HIGH + MEDIUM.

        Corroboration is the engine's headline factor, so a grid made a published severity
        a fact about the wall clock rather than about the evidence.
        """
        alerts = WeightedAlertEngine().build_alerts(_trio(base), ASSETS)

        assert len(alerts) == 1
        assert alerts[0].score_components["corroboration"] == 2.0
        assert alerts[0].severity is AlertSeverity.CRITICAL

    def test_a_detection_inside_a_longer_span_joins_it(self) -> None:
        """Grouping read only window_start, so a long collective span did not absorb a
        detection lying within it: one incident became two alerts with overlapping ranges."""
        base = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        detections = [
            _detection(at=base, lane=Lane.TELEMETRY, span_s=8 * 60),
            _detection(at=base + timedelta(minutes=4), lane=Lane.FLOW),
        ]

        alerts = WeightedAlertEngine().build_alerts(detections, ASSETS)

        assert len(alerts) == 1
        assert alerts[0].score_components["corroboration"] == 1.5

    def test_a_gap_wider_than_the_bucket_opens_a_new_alert(self) -> None:
        base = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        detections = [
            _detection(at=base),
            _detection(at=base + timedelta(minutes=6)),
        ]

        alerts = WeightedAlertEngine(bucket=timedelta(minutes=5)).build_alerts(detections, ASSETS)

        assert len(alerts) == 2

    def test_a_chain_of_close_detections_is_capped_so_one_alert_stays_one_incident(self) -> None:
        """Single-link chaining is transitive, so without a ceiling a slow drizzle becomes
        one alert spanning hours. MAX_BUCKET_SPAN is what keeps an alert actionable."""
        base = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        detections = [_detection(at=base + timedelta(minutes=4 * i)) for i in range(6)]

        alerts = WeightedAlertEngine(bucket=timedelta(minutes=5)).build_alerts(detections, ASSETS)

        assert len(alerts) > 1
        for alert in alerts:
            assert alert.window_end - alert.window_start <= MAX_BUCKET_SPAN

    def test_grouping_does_not_depend_on_input_order(self) -> None:
        """The grid got order-independence for free; a walk has to earn it by sorting."""
        base = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        detections = _trio(base)
        forward = WeightedAlertEngine().build_alerts(detections, ASSETS)
        backward = WeightedAlertEngine().build_alerts(list(reversed(detections)), ASSETS)

        assert [a.priority_score for a in forward] == [a.priority_score for a in backward]

    def test_a_pre_1970_timestamp_is_scored_rather_than_crashing(self) -> None:
        """Flooring went through datetime.fromtimestamp, which raises OSError on Windows for
        anything before 1969-12-31T12:00Z. An unsynced RTC took the whole run down."""
        detections = [_detection(at=datetime(1969, 12, 31, tzinfo=UTC))]

        alerts = WeightedAlertEngine().build_alerts(detections, ASSETS)

        assert len(alerts) == 1


class TestVolumeCountsJudgmentsNotRows:
    def test_two_detectors_judging_one_window_are_one_claim(self) -> None:
        """The shipped demo_rules gateway: two rules matching the SAME flow record pushed
        volume 1.15 -> 1.30 and carried the alert over the HIGH floor by 0.037 points."""
        at = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        same_window = [
            _detection(at=at, lane=Lane.FLOW),
            _detection(at=at, lane=Lane.FLOW),
        ]

        assert distinct_judgments(same_window) == 1
        alert = WeightedAlertEngine().build_alerts(same_window, ASSETS)[0]
        assert alert.score_components["volume"] == 1.0

    def test_the_same_lane_at_different_moments_still_counts_twice(self) -> None:
        """Recurrence over time is what volume exists to measure, so it must survive."""
        at = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        spread = [
            _detection(at=at, lane=Lane.FLOW),
            _detection(at=at + timedelta(seconds=30), lane=Lane.FLOW),
        ]

        assert distinct_judgments(spread) == 2
        alert = WeightedAlertEngine().build_alerts(spread, ASSETS)[0]
        assert alert.score_components["volume"] == pytest.approx(1.15)

    def test_the_rationale_names_the_distinct_count_when_it_differs(self) -> None:
        at = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        duplicated = [_detection(at=at, lane=Lane.FLOW) for _ in range(3)]

        alert = WeightedAlertEngine().build_alerts(duplicated, ASSETS)[0]

        assert "3 detection(s) (1 distinct)" in alert.rationale

    def test_the_stub_engine_counts_the_same_way(self) -> None:
        """Both engines share the definition, so swapping engines cannot change what
        'more of this' means — only how much it is worth."""
        at = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        same_window = [_detection(at=at, lane=Lane.FLOW) for _ in range(4)]

        alert = StubAlertEngine().build_alerts(same_window, ASSETS)[0]

        assert "4 detection(s) (1 distinct)" in alert.rationale


class TestStubEngineSpansTheScale:
    def test_a_barely_over_threshold_detection_reads_as_low(self) -> None:
        """Starting the scale at 30 put a floor of 45 under every stub alert, so LOW was
        unreachable from the DEFAULT engine — a triage queue that cannot say 'minor'."""
        at = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        marginal = [_detection(at=at, asset_id="sensor-1", score=80.0000001, threshold=80.0)]

        alert = StubAlertEngine().build_alerts(marginal, ASSETS)[0]

        assert alert.severity is AlertSeverity.LOW
        assert alert.priority_score < 30.0

    def test_a_strong_corroborated_finding_still_reaches_the_top(self) -> None:
        base = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        strong = [
            _detection(at=base + timedelta(seconds=10 * i), lane=lane, score=1000.0)
            for i, lane in enumerate([Lane.FLOW, Lane.LOG, Lane.TELEMETRY])
        ]

        alert = StubAlertEngine().build_alerts(strong, ASSETS)[0]

        assert alert.severity is AlertSeverity.EMERGENCY
