"""Weighted (team2-derived) AlertEngine behavior: the multiplicative score.

Port-level invariants (coverage, lineage, corroboration monotonicity, ranking)
are in tests/ports/test_alert_engine_port.py. These cover what is particular to
this adapter: the decomposed score_components + rationale, and that ranking is
absolute (batch-independent), not team2's relative min-max — the D1 decision that
keeps corroboration monotone (ADR-0013).
"""

from datetime import UTC, datetime, timedelta

from iotshield.alert import WeightedAlertEngine
from iotshield.contracts import Asset, Criticality, DetectionResult, Lane

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
ASSETS = {
    "pump-02": Asset(
        asset_id="pump-02", name="Feed pump 2", asset_type="pump", criticality=Criticality.HIGH
    )
}


def _detection(
    *,
    asset_id: str = "pump-02",
    lane: Lane = Lane.TELEMETRY,
    offset_s: int = 0,
    score: float = 160.0,
    threshold: float = 80.0,
) -> DetectionResult:
    start = T0 + timedelta(seconds=offset_s)
    return DetectionResult(
        asset_id=asset_id,
        window_start=start,
        window_end=start + timedelta(seconds=30),
        lane=lane,
        method="test_method",
        score=score,
        threshold=threshold,
    )


class TestWeightedAlertEngine:
    def test_score_components_are_decomposed(self) -> None:
        (alert,) = WeightedAlertEngine().build_alerts([_detection()], ASSETS)
        assert set(alert.score_components) >= {
            "severity",
            "corroboration",
            "volume",
            "asset_weight",
            "raw_score",
        }
        assert 0.0 <= alert.priority_score <= 100.0

    def test_rationale_explains_each_factor(self) -> None:
        (alert,) = WeightedAlertEngine().build_alerts([_detection()], ASSETS)
        for factor in ("severity", "corroboration", "volume", "asset_weight"):
            assert factor in alert.rationale

    def test_cross_lane_corroboration_raises_priority(self) -> None:
        engine = WeightedAlertEngine()
        (lone,) = engine.build_alerts([_detection(lane=Lane.TELEMETRY)], ASSETS)
        (both,) = engine.build_alerts(
            [_detection(lane=Lane.TELEMETRY), _detection(lane=Lane.LOG, offset_s=10)], ASSETS
        )
        assert both.priority_score > lone.priority_score
        assert both.score_components["corroboration"] > lone.score_components["corroboration"]

    def test_ranking_is_absolute_not_batch_relative(self) -> None:
        # team2 min-max normalizes scores across the batch; we deliberately don't
        # (D1). A weak alert's score must not change just because a much stronger
        # alert is scored alongside it.
        engine = WeightedAlertEngine()
        weak = _detection(asset_id="gw-01", score=81.0)
        strong = _detection(asset_id="pump-02", score=800.0)
        (weak_alone,) = engine.build_alerts([weak], {})
        weak_with_strong = next(
            a for a in engine.build_alerts([weak, strong], {}) if a.asset_id == "gw-01"
        )
        assert weak_with_strong.priority_score == weak_alone.priority_score

    def test_unregistered_asset_uses_medium_weight(self) -> None:
        (alert,) = WeightedAlertEngine().build_alerts([_detection(asset_id="ghost")], {})
        assert "unregistered asset" in alert.rationale
        assert alert.score_components["asset_weight"] == 1.1
