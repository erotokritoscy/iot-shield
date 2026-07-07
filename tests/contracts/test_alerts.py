from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from iotshield.contracts import AlertSeverity, AlertStatus, PrioritizedAlert

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 7, 12, 15, 0, tzinfo=UTC)


def make_alert(**overrides: object) -> PrioritizedAlert:
    defaults: dict[str, object] = {
        "asset_id": "pump-02",
        "window_start": T0,
        "window_end": T1,
        "severity": AlertSeverity.CRITICAL,
        "priority_score": 87.5,
        "rationale": "Telemetry anomaly on high-criticality asset, corroborated by error logs.",
        "detection_ids": [uuid4(), uuid4()],
    }
    defaults.update(overrides)
    return PrioritizedAlert.model_validate(defaults)


@pytest.mark.parametrize("score", [-0.1, 100.1])
def test_priority_score_out_of_bounds_rejected(score: float) -> None:
    with pytest.raises(ValidationError, match="priority_score"):
        make_alert(priority_score=score)


@pytest.mark.parametrize("score", [0.0, 100.0])
def test_priority_score_bounds_inclusive(score: float) -> None:
    assert make_alert(priority_score=score).priority_score == score


def test_empty_detection_ids_rejected() -> None:
    # Every alert must trace back to at least one detection (lineage is the product).
    with pytest.raises(ValidationError, match="detection_ids"):
        make_alert(detection_ids=[])


def test_defaults() -> None:
    alert = make_alert()
    assert alert.status == AlertStatus.NEW
    assert alert.threat_class == "unknown"
    assert alert.created_at.tzinfo == UTC


def test_empty_rationale_rejected() -> None:
    # The rationale is the product's explainability promise — never blank.
    with pytest.raises(ValidationError):
        make_alert(rationale="")


def test_window_end_before_start_rejected() -> None:
    with pytest.raises(ValidationError, match="window_end"):
        make_alert(window_start=T1, window_end=T0)


def test_json_round_trip_preserves_everything() -> None:
    alert = make_alert(
        score_components={"severity_value": 5, "device_weight": 1.2, "corroboration": 0.15},
        recommended_action="Inspect pump-02 pressure sensor and recent auth logs.",
    )
    restored = PrioritizedAlert.model_validate_json(alert.model_dump_json())
    assert restored == alert
