from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from iotshield.contracts import DetectionResult, EventKind, Lane

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 7, 12, 15, 0, tzinfo=UTC)


def make_detection(**overrides: object) -> DetectionResult:
    defaults: dict[str, object] = {
        "asset_id": "pump-02",
        "window_start": T0,
        "window_end": T1,
        "lane": Lane.TELEMETRY,
        "method": "lstm_ae",
        "score": 0.91,
        "threshold": 0.42,
    }
    defaults.update(overrides)
    return DetectionResult.model_validate(defaults)


def test_window_end_before_start_rejected() -> None:
    with pytest.raises(ValidationError, match="window_end"):
        make_detection(window_start=T1, window_end=T0)


def test_zero_length_window_allowed() -> None:
    detection = make_detection(window_start=T0, window_end=T0, event_kind=EventKind.POINT)
    assert detection.window_start == detection.window_end


def test_json_round_trip_preserves_everything() -> None:
    detection = make_detection(
        event_kind=EventKind.COLLECTIVE,
        evidence={"top_channel": "Pressure", "channel_error": 0.8},
        metadata={"model_artifact": "artifacts/skab/lstm_ae.pt"},
    )
    restored = DetectionResult.model_validate_json(detection.model_dump_json())
    assert restored == detection


def test_event_kind_optional() -> None:
    assert make_detection().event_kind is None


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        make_detection(confidence=0.9)


def test_naive_window_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_detection(window_start=datetime(2026, 7, 7, 12, 0, 0))


@pytest.mark.parametrize("field", ["score", "threshold"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_score_rejected(field: str, bad: float) -> None:
    # A NaN score is not JSON-serializable and would 500 the API (or NULL out on
    # SQLite); reject it at the contract so a buggy detector fails loudly.
    with pytest.raises(ValidationError):
        make_detection(**{field: bad})
