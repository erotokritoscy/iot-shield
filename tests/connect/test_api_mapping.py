"""The JSON-to-events mapping of an HTTP API source (ADR-0049 D3).

The mapper writes ``normalized-jsonl`` lines and the ``passthrough`` normalizer reads them, so
each test here runs both: what matters is the *events and rejects* a mapping produces, not
the intermediate text.
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from iotshield.connect.api.mapping import ApiMapping, MappingError, lookup, map_document
from iotshield.contracts import Lane, Severity
from iotshield.normalize.passthrough import SOURCE_FORMAT, PassThroughNormalizer
from iotshield.normalize.port import NormalizeResult

TELEMETRY = ApiMapping(
    lane="telemetry",
    records="data.items",
    timestamp="ts",
    asset_id="device.id",
    metrics={"Temperature": "readings.temp", "Pressure": "readings.p"},
)
LOGS = ApiMapping(
    lane="log",
    records="",
    timestamp="time",
    timestamp_format="epoch_ms",
    asset_id="host",
    message="msg",
    severity="level",
)


def _normalize(document: Any, mapping: ApiMapping) -> NormalizeResult:
    text = "".join(line + "\n" for _, line in map_document(document, mapping))
    return PassThroughNormalizer(lane=Lane(mapping.lane)).normalize(
        text, source_format=SOURCE_FORMAT, origin="test"
    )


class TestLookup:
    def test_dotted_keys_and_list_indices(self) -> None:
        doc = {"a": {"b": [{"c": 1}, {"c": 2}]}}
        assert lookup(doc, "a.b.1.c") == 2
        assert lookup(doc, "") is doc

    def test_a_missing_segment_is_missing_not_none(self) -> None:
        doc = {"a": {"b": None}}
        assert lookup(doc, "a.b") is None
        missing = lookup(doc, "a.x")
        assert missing is not None and missing is lookup(doc, "a.b.c")
        assert lookup({"a": [1]}, "a.5") is missing


class TestTelemetry:
    def test_one_event_per_metric_in_utc(self) -> None:
        doc = {
            "data": {
                "items": [
                    {
                        "ts": "2026-11-01T10:00:00+02:00",
                        "device": {"id": 17},
                        "readings": {"temp": 21.5, "p": 3},
                    }
                ]
            }
        }
        result = _normalize(doc, TELEMETRY)
        assert result.malformed == []
        events = sorted(result.events, key=lambda event: event.metric or "")
        assert [(e.metric, e.value) for e in events] == [("Pressure", 3.0), ("Temperature", 21.5)]
        assert {e.asset_id for e in events} == {"17"}  # a numeric id becomes text
        assert events[0].timestamp.isoformat() == "2026-11-01T08:00:00+00:00"
        assert events[0].source_type is Lane.TELEMETRY

    def test_a_missing_field_is_a_reject_that_names_it(self) -> None:
        doc = {"data": {"items": [{"device": {"id": "a"}, "readings": {"temp": 1, "p": 2}}]}}
        result = _normalize(doc, TELEMETRY)
        assert result.events == []
        assert len(result.malformed) == 2
        assert all("timestamp" in record.error for record in result.malformed)

    def test_a_missing_metric_rejects_only_that_metric(self) -> None:
        doc = {
            "data": {
                "items": [
                    {"ts": "2026-11-01T00:00:00Z", "device": {"id": "a"}, "readings": {"temp": 1}}
                ]
            }
        }
        result = _normalize(doc, TELEMETRY)
        assert [e.metric for e in result.events] == ["Temperature"]
        assert len(result.malformed) == 1
        assert '"missing": "readings.p"' in result.malformed[0].raw  # says which path

    def test_a_non_numeric_value_is_refused(self) -> None:
        doc = {
            "data": {
                "items": [
                    {
                        "ts": "2026-11-01T00:00:00Z",
                        "device": {"id": "a"},
                        "readings": {"temp": "hot", "p": 1},
                    }
                ]
            }
        }
        result = _normalize(doc, TELEMETRY)
        assert [e.metric for e in result.events] == ["Pressure"]
        assert len(result.malformed) == 1


class TestTimestamps:
    def test_a_naive_iso_time_is_refused_unless_a_zone_is_declared(self) -> None:
        doc = {
            "data": {
                "items": [
                    {
                        "ts": "2026-07-01T12:00:00",
                        "device": {"id": "a"},
                        "readings": {"temp": 1, "p": 1},
                    }
                ]
            }
        }
        assert _normalize(doc, TELEMETRY).events == []  # rule 4: no guessing the zone
        cyprus = TELEMETRY.model_copy(update={"timezone": "Asia/Nicosia"})
        events = _normalize(doc, cyprus).events
        assert events and events[0].timestamp.isoformat() == "2026-07-01T09:00:00+00:00"

    @pytest.mark.parametrize(
        ("fmt", "value"), [("epoch_s", 1_762_000_000), ("epoch_ms", 1_762_000_000_000)]
    )
    def test_epochs_are_utc(self, fmt: str, value: int) -> None:
        mapping = TELEMETRY.model_copy(update={"timestamp_format": fmt})
        doc = {
            "data": {
                "items": [{"ts": value, "device": {"id": "a"}, "readings": {"temp": 1, "p": 1}}]
            }
        }
        events = _normalize(doc, mapping).events
        assert events[0].timestamp.timestamp() == 1_762_000_000

    def test_an_unknown_zone_is_refused_when_saved(self) -> None:
        with pytest.raises(ValidationError, match="unknown time zone"):
            TELEMETRY.model_copy(update={"timezone": "Mars/Olympus"}).model_validate(
                {**TELEMETRY.model_dump(), "timezone": "Mars/Olympus"}
            )


class TestLogs:
    def test_message_and_severity_with_aliases(self) -> None:
        doc = [
            {"time": 1_762_000_000_000, "host": "gw-1", "msg": "login failed", "level": "WARN"},
            {"time": 1_762_000_001_000, "host": "gw-1", "msg": {"code": 7}, "level": "fatal"},
        ]
        result = _normalize(doc, LOGS)
        assert result.malformed == []
        assert [(e.message, e.severity) for e in result.events] == [
            ("login failed", Severity.WARNING),
            (json.dumps({"code": 7}), Severity.CRITICAL),
        ]

    def test_an_unknown_level_is_a_reject(self) -> None:
        doc = [{"time": 1_762_000_000_000, "host": "gw", "msg": "x", "level": "spicy"}]
        result = _normalize(doc, LOGS)
        assert result.events == [] and "severity" in result.malformed[0].error


class TestShape:
    def test_records_must_be_a_list(self) -> None:
        with pytest.raises(MappingError, match=r"'data\.items'"):
            list(map_document({"data": {"items": {}}}, TELEMETRY))

    def test_a_record_that_is_not_an_object_reaches_data_health(self) -> None:
        result = _normalize([42], LOGS)
        assert result.events == [] and "42" in result.malformed[0].raw

    @pytest.mark.parametrize(
        "body",
        [
            {"lane": "telemetry", "timestamp": "t", "asset_id": "a"},  # no metrics
            {"lane": "log", "timestamp": "t", "asset_id": "a"},  # no message
            {"lane": "flow", "timestamp": "t", "asset_id": "a", "message": "m"},  # flow: no
            {"lane": "log", "timestamp": "", "asset_id": "a", "message": "m"},
        ],
    )
    def test_refuses_a_mapping_that_could_never_produce_events(self, body: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            ApiMapping.model_validate(body)
