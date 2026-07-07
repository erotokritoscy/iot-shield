from datetime import UTC

import pytest
from pydantic import ValidationError

from iotshield.contracts import MalformedRecord


def make_record(**overrides: object) -> MalformedRecord:
    defaults: dict[str, object] = {
        "raw": "garbage;;;line",
        "error": "expected 4 fields, got 1",
        "source_format": "stub-csv",
        "origin": "test://fixture",
    }
    defaults.update(overrides)
    return MalformedRecord.model_validate(defaults)


def test_defaults() -> None:
    a, b = make_record(), make_record()
    assert a.record_id != b.record_id
    assert a.created_at.tzinfo == UTC


def test_empty_error_rejected() -> None:
    # A reject without a reason is useless for debugging — never allowed.
    with pytest.raises(ValidationError):
        make_record(error="")


def test_empty_raw_allowed() -> None:
    # An empty line can itself be the malformed input for some parsers.
    assert make_record(raw="").raw == ""


def test_json_round_trip_preserves_everything() -> None:
    record = make_record()
    assert MalformedRecord.model_validate_json(record.model_dump_json()) == record


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        make_record(reasn="typo")
