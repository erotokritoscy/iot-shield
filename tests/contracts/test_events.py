from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from iotshield.contracts import Lane, NormalizedEvent, Severity


def make_event(**overrides: object) -> NormalizedEvent:
    defaults: dict[str, object] = {
        "timestamp": datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC),
        "asset_id": "pump-02",
        "source_type": Lane.TELEMETRY,
        "source_format": "skab-csv",
        "metric": "Pressure",
        "value": 0.38,
        "raw": "2026-07-07 12:00:00;0.38",
    }
    defaults.update(overrides)
    return NormalizedEvent.model_validate(defaults)


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_event(timestamp=datetime(2026, 7, 7, 12, 0, 0))


def test_non_utc_timestamp_normalized_to_utc_same_instant() -> None:
    athens = timezone(timedelta(hours=3))
    event = make_event(timestamp=datetime(2026, 7, 7, 15, 0, 0, tzinfo=athens))
    assert event.timestamp.tzinfo == UTC
    assert event.timestamp == datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def test_iso_string_timestamp_accepted() -> None:
    event = make_event(timestamp="2026-07-07T12:00:00+00:00")
    assert event.timestamp == datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def test_naive_iso_string_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_event(timestamp="2026-07-07T12:00:00")


@pytest.mark.parametrize("epoch", [1720000000, 1720000000.0, "1720000000", "1.7e9"])
def test_epoch_number_timestamp_rejected(epoch: object) -> None:
    # A bare number is epoch seconds to pydantic; reject it so a column-shifted
    # CSV line becomes malformed data, not a silent 1970-dated event.
    with pytest.raises(ValidationError, match="ISO-8601"):
        make_event(timestamp=epoch)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_value_rejected(bad: float) -> None:
    # Non-finite floats are not JSON-serializable and corrupt storage; the
    # normalizer turns these into MalformedRecords instead.
    with pytest.raises(ValidationError):
        make_event(value=bad)


def test_json_round_trip_preserves_everything() -> None:
    event = make_event(metadata={"line_number": 42})
    restored = NormalizedEvent.model_validate_json(event.model_dump_json())
    assert restored == event


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        make_event(devcie_id="typo")


def test_defaults() -> None:
    a, b = make_event(), make_event()
    assert a.severity == Severity.INFO
    assert a.metric is not None and a.value is not None
    assert a.event_id != b.event_id  # fresh UUID per event


def test_empty_asset_id_rejected() -> None:
    with pytest.raises(ValidationError):
        make_event(asset_id="")
