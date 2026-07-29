"""PassThroughNormalizer: validate-and-envelope, and the param-driven lane (ADR-0025).

The port suite (tests/ports/test_normalizer_port.py) pins the shared laws (conservation,
verbatim raw, unsupported-format raises). These are the pass-through-specific behaviours:
that a NormalizedEvent round-trips through it, that the configured lane is filled in when
absent and enforced when present, that upstream provenance is preserved, and that the
failures peculiar to a JSON envelope are captured rather than fatal.
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.normalize import NormalizeResult, PassThroughNormalizer
from iotshield.normalize.passthrough import SOURCE_FORMAT, capability

ORIGIN = "test://passthrough"


def run(text: str, lane: Lane = Lane.TELEMETRY) -> NormalizeResult:
    return PassThroughNormalizer(lane=lane).normalize(
        text, source_format=SOURCE_FORMAT, origin=ORIGIN
    )


def event_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "timestamp": "2026-07-07T12:00:00+00:00",
        "asset_id": "pump-02",
        "source_type": "telemetry",
        "source_format": "partner-scada",
        "metric": "Pressure",
        "value": 0.38,
    }
    base.update(overrides)
    return base


def line(**overrides: Any) -> str:
    return json.dumps(event_dict(**overrides))


class TestValidateAndEnvelope:
    def test_a_wellformed_record_becomes_one_event(self) -> None:
        result = run(line())
        assert len(result.events) == 1 and result.malformed == []

    def test_raw_is_the_verbatim_json_line(self) -> None:
        text = line()
        (event,) = run(text).events
        assert event.raw == text

    def test_a_real_event_round_trips_through_the_normalizer(self) -> None:
        # The pass-through is the inverse of serializing a NormalizedEvent to JSONL: a dumped
        # event comes back equal on every field, except raw (reset to the line, per the port).
        original = NormalizedEvent(
            timestamp=datetime(2026, 7, 7, 12, tzinfo=UTC),
            asset_id="pump-02",
            source_type=Lane.TELEMETRY,
            source_format="partner-scada",
            metric="Pressure",
            value=0.38,
            raw="original upstream wire record",
        )
        dumped = original.model_dump_json()
        (event,) = run(dumped).events
        assert event.event_id == original.event_id  # preserved from the JSON
        assert event.timestamp == original.timestamp
        assert event.asset_id == original.asset_id
        assert event.source_type is original.source_type
        assert event.metric == original.metric and event.value == original.value
        assert event.raw == dumped  # not the upstream raw: the port requires this stage's input


class TestLaneParameter:
    def test_source_type_is_filled_from_the_configured_lane_when_absent(self) -> None:
        obj = event_dict()
        del obj["source_type"]
        (event,) = run(json.dumps(obj)).events
        assert event.source_type is Lane.TELEMETRY

    def test_a_record_declaring_a_different_lane_is_quarantined(self) -> None:
        result = run(line(source_type="log"), lane=Lane.TELEMETRY)
        assert result.events == []
        (reject,) = result.malformed
        assert "configured for 'telemetry'" in reject.error

    def test_a_flow_source_carries_flow_events(self) -> None:
        (event,) = run(line(source_type="flow"), lane=Lane.FLOW).events
        assert event.source_type is Lane.FLOW


class TestProvenance:
    def test_an_upstream_source_format_is_preserved(self) -> None:
        (event,) = run(line(source_format="partner-scada")).events
        assert event.source_format == "partner-scada"

    def test_source_format_defaults_to_the_adapter_format_when_absent(self) -> None:
        obj = event_dict()
        del obj["source_format"]
        (event,) = run(json.dumps(obj)).events
        assert event.source_format == SOURCE_FORMAT


class TestContractValidation:
    def test_a_naive_timestamp_is_rejected_by_the_contract(self) -> None:
        result = run(line(timestamp="2026-07-07T12:00:00"))
        assert result.events == []
        assert "timezone-aware" in result.malformed[0].error

    def test_an_unknown_field_is_rejected(self) -> None:
        # extra="forbid" on the contract: a typo or an unexpected field fails loudly rather
        # than being silently ignored.
        result = run(line(unexpected_field=1))
        assert result.events == []
        assert result.malformed

    def test_a_non_finite_value_is_rejected(self) -> None:
        # value is FiniteFloat; NaN/inf break JSON storage. Python's json accepts the literal
        # NaN, so simulate a source that wrote it — the contract's finite guard rejects it.
        result = run(line(value=float("nan")))
        assert result.events == []


class TestJsonShapeFailures:
    def test_invalid_json_is_captured_not_fatal(self) -> None:
        result = run("this is not json")
        assert result.events == []
        assert "not valid JSON" in result.malformed[0].error

    @pytest.mark.parametrize("text", ["[1, 2, 3]", "42", "null"])
    def test_a_non_object_json_value_is_not_an_event(self, text: str) -> None:
        result = run(text)
        assert result.events == []
        assert "expected a JSON object" in result.malformed[0].error


class TestCapabilityDeclaration:
    # The declared lane's full sweep (and its agreement with a built instance) lives in
    # tests/pipeline/test_capabilities.py, which owns the declaration-matches-reality law for
    # every adapter — the registry wires this very function, so asserting it here too would be
    # the same values in two files. Only the failure mode is adapter-local.
    def test_a_bad_lane_value_raises(self) -> None:
        with pytest.raises(ValueError):
            capability({"lane": "bogus"})
