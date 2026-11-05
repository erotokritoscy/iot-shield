"""The noisy-OR's arithmetic and its three departures from upstream (alert/fused.py)."""

from datetime import UTC, datetime

import pytest

from iotshield.alert.fused import WEIGHTS, FusedAlertEngine
from iotshield.contracts import DetectionResult, EventKind, Lane
from iotshield.meter import SIGNATURE_KEY, QualityIssue, Signature

T0 = datetime(2026, 4, 1, tzinfo=UTC)


def found(method: str, signature: Signature | None = None, **evidence: object) -> DetectionResult:
    return DetectionResult(
        asset_id="m",
        window_start=T0,
        window_end=T0,
        lane=Lane.TELEMETRY,
        method=method,
        score=2.0,
        threshold=1.0,
        event_kind=EventKind.POINT,
        evidence={
            "explanation": "x",
            **({SIGNATURE_KEY: signature.value} if signature else {}),
            **evidence,
        },
    )


def priority(*detections: DetectionResult, **engine: float) -> float:
    alerts = FusedAlertEngine(**engine).build_alerts(detections, {})  # type: ignore[arg-type]
    assert len(alerts) == 1
    return alerts[0].priority_score


def test_a_lone_detection_scores_its_own_competence() -> None:
    assert priority(found("meter_iforest", Signature.SUSTAINED)) == pytest.approx(98.0)


def test_agreement_is_a_noisy_or() -> None:
    joint = 1 - (1 - 0.45) * (1 - 0.68)
    assert priority(
        found("meter_iforest", Signature.SPIKE), found("meter_lof", Signature.SPIKE)
    ) == pytest.approx(100 * joint)


def test_a_flag_from_a_detector_poor_at_that_shape_stays_low_but_is_not_dropped() -> None:
    """Upstream's Fuser removes this incident; the port requires it to be covered."""
    assert priority(found("meter_iforest", Signature.PROFILE)) == pytest.approx(1.0)


def test_the_case_that_breaks_upstream_is_monotone_here() -> None:
    """baseline(spike) + lof(spike), then baseline(stuck): upstream re-types and falls."""
    first = priority(found("meter_baseline", Signature.SPIKE), found("meter_lof", Signature.SPIKE))
    second = priority(
        found("meter_baseline", Signature.SPIKE),
        found("meter_lof", Signature.SPIKE),
        found("meter_baseline", Signature.STUCK),
    )
    upstream_second = 100 * (1 - (1 - 0.88) * (1 - 0.03))
    assert upstream_second < first <= second


def test_one_detector_repeating_itself_is_not_agreement() -> None:
    one = found("meter_iforest", Signature.SUSTAINED)
    assert priority(one, one) == priority(one)


def test_a_heartbeat_is_structural() -> None:
    silent = found("meter_quality", quality=QualityIssue.HEARTBEAT.value)
    assert priority(silent) == pytest.approx(100.0)


def test_a_high_reading_is_not_structural() -> None:
    assert priority(found("meter_quality", quality=QualityIssue.HIGH.value)) == pytest.approx(50.0)


def test_a_detector_with_no_measured_weight_gets_other_weight() -> None:
    assert priority(found("flow_burst"), other_weight=0.2) == pytest.approx(20.0)


def test_a_meter_detection_with_no_signature_gets_other_weight() -> None:
    assert priority(found("meter_iforest"), other_weight=0.2) == pytest.approx(20.0)


def test_the_rationale_shows_what_was_multiplied() -> None:
    alert = FusedAlertEngine().build_alerts([found("meter_lof", Signature.STUCK)], {})[0]
    assert "meter_lof 0.03" in alert.rationale
    assert alert.score_components["joint"] == pytest.approx(0.03)


def test_every_weight_is_a_probability_short_of_certainty() -> None:
    for table in WEIGHTS.values():
        assert set(table) == {signature.value for signature in Signature}
        assert all(0.0 < weight < 1.0 for weight in table.values())


@pytest.mark.parametrize("bad", [-0.1, 1.0, 2.0])
def test_a_nonsense_other_weight_is_refused(bad: float) -> None:
    with pytest.raises(ValueError):
        FusedAlertEngine(other_weight=bad)
