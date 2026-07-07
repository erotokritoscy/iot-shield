import pytest
from pydantic import ValidationError

from iotshield.contracts import Asset, Criticality


def make_asset(**overrides: object) -> Asset:
    defaults: dict[str, object] = {
        "asset_id": "pump-02",
        "name": "Feed pump 2",
        "asset_type": "pump",
    }
    defaults.update(overrides)
    return Asset(**defaults)  # type: ignore[arg-type]


def test_criticality_defaults_to_medium() -> None:
    assert make_asset().criticality == Criticality.MEDIUM


@pytest.mark.parametrize("field", ["asset_id", "name", "asset_type"])
def test_required_strings_reject_empty(field: str) -> None:
    with pytest.raises(ValidationError):
        make_asset(**{field: ""})


def test_json_round_trip_preserves_everything() -> None:
    asset = make_asset(
        location="line-1",
        criticality=Criticality.HIGH,
        metadata={"vendor": "SKF", "commissioned": "2024-02-08"},
    )
    assert Asset.model_validate_json(asset.model_dump_json()) == asset


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        make_asset(criticallity="high")
