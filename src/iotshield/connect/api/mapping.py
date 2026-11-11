"""Turn an HTTP API's JSON records into ``normalized-jsonl`` lines (ADR-0049 D3).

An API source returns JSON of a shape nobody here chose, so the admin who registers it
declares a **mapping**: where the records are, and which path in each record is the
timestamp, the asset, and the readings (telemetry) or the message (log). The mapping does
**not** produce events. It produces one ``normalized-jsonl`` line per event, and the run
reads those with the ``passthrough`` normalizer. That keeps the four-port model intact and
gives the mapping every port law for free: a record without a timestamp, a naive timestamp
or a value that is not a number is a ``MalformedRecord`` on Data Health, not an exception
and not a silent drop.

So the mapper is deliberately forgiving: a path that is missing in one record leaves that
field out of its line, and the contract's validation names what is missing. A missing
*reading* is the one exception, because the contract lets an event carry no value: the line
gets a ``missing`` key naming the path instead, which the contract refuses as unknown.

The only thing it decides itself is how to read a timestamp. An epoch number is UTC by
definition. An ISO string with no offset is a local time in a zone nobody named, so it is
passed through naive and the contract rejects it (rule 4) — unless the mapping declares the
zone the API speaks.

Paths are dotted (``data.items``, ``sensor.0.id``): a segment is a key, or an index into a
list. The empty path is the document itself.
"""

import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from iotshield.contracts import Lane

_MISSING: Any = object()

TimestampFormat = Literal["iso", "epoch_s", "epoch_ms"]
MappedLane = Literal["telemetry", "log"]

SOURCE_FORMAT = "normalized-jsonl"
"""What the mapper writes and the ``passthrough`` normalizer reads."""


class MappingError(ValueError):
    """The response cannot be mapped at all — its records path does not hold a list."""


class ApiMapping(BaseModel):
    """Where an API's records and fields are. Validated when an admin saves the source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lane: MappedLane = Field(description="Which lane the events go to: readings or log lines")
    records: str = Field(
        default="", max_length=200, description="Dotted path to the list of records; blank = root"
    )
    timestamp: str = Field(min_length=1, max_length=200)
    timestamp_format: TimestampFormat = "iso"
    timezone: str | None = Field(
        default=None,
        max_length=64,
        description="IANA zone for ISO timestamps that carry no offset; blank refuses them",
    )
    asset_id: str = Field(min_length=1, max_length=200)
    metrics: dict[str, str] = Field(
        default_factory=dict,
        description="Telemetry: metric name -> path of its value; one event per metric",
    )
    message: str | None = Field(default=None, max_length=200, description="Log: path of the text")
    severity: str | None = Field(default=None, max_length=200, description="Log: path of the level")

    @field_validator("timezone", "message", "severity")
    @classmethod
    def _blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _lane_fields(self) -> "ApiMapping":
        if self.lane == "telemetry":
            if not self.metrics:
                raise ValueError("a telemetry mapping needs at least one metric")
            if len(self.metrics) > 50:
                raise ValueError("a telemetry mapping may name at most 50 metrics")
            for name, path in self.metrics.items():
                if not name.strip() or not path.strip():
                    raise ValueError("every metric needs a name and a path")
        elif self.message is None:
            raise ValueError("a log mapping needs the path of the message")
        return self


def lookup(document: Any, path: str) -> Any:
    """The value at a dotted path, or ``_MISSING``. A digit segment indexes a list."""
    current = document
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def records_of(document: Any, mapping: ApiMapping) -> list[Any]:
    found = lookup(document, mapping.records)
    if not isinstance(found, list):
        where = f"at {mapping.records!r}" if mapping.records else "at the top of the response"
        raise MappingError(f"expected a list of records {where}")
    return found


def _timestamp(raw: Any, mapping: ApiMapping) -> Any:
    """The timestamp as the contract reads it, or the raw value for the contract to refuse."""
    if mapping.timestamp_format == "iso":
        if not isinstance(raw, str):
            return raw
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
        if parsed.tzinfo is None:
            if mapping.timezone is None:
                return raw  # naive and no zone declared: the contract rejects it (rule 4)
            parsed = parsed.replace(tzinfo=ZoneInfo(mapping.timezone))
        return parsed.astimezone(UTC).isoformat()
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        return raw
    seconds = raw / 1000 if mapping.timestamp_format == "epoch_ms" else raw
    try:
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return raw


def _asset(raw: Any) -> Any:
    # A numeric device id is common in APIs; the contract wants text.
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return str(raw)
    return raw


_SEVERITY_ALIASES = {
    "warn": "warning",
    "err": "error",
    "crit": "critical",
    "fatal": "critical",
    "emerg": "critical",
    "alert": "critical",
    "notice": "info",
    "information": "info",
    "trace": "debug",
}


def _severity(raw: Any) -> Any:
    """A level in the contract's vocabulary; an unknown word is passed on to be refused."""
    if not isinstance(raw, str):
        return raw
    word = raw.strip().lower()
    return _SEVERITY_ALIASES.get(word, word)


def _base(record: Any, mapping: ApiMapping) -> dict[str, Any]:
    line: dict[str, Any] = {"source_type": Lane(mapping.lane)}
    stamp = lookup(record, mapping.timestamp)
    if stamp is not _MISSING:
        line["timestamp"] = _timestamp(stamp, mapping)
    asset = lookup(record, mapping.asset_id)
    if asset is not _MISSING:
        line["asset_id"] = _asset(asset)
    return line


def map_record(record: Any, mapping: ApiMapping) -> Iterator[str]:
    """One record as ``normalized-jsonl`` lines: one per metric, or one log line.

    A record that is not an object becomes one line holding it under ``record``, which the
    contract refuses as an unknown field — so it reaches Data Health with its own bytes.
    """
    if not isinstance(record, dict):
        yield json.dumps({"record": record}, default=str)
        return
    base = _base(record, mapping)
    if mapping.lane == "telemetry":
        for metric, path in mapping.metrics.items():
            line = {**base, "metric": metric}
            value = lookup(record, path)
            if value is _MISSING or value is None:
                # The contract allows an event without a value, so a missing reading would
                # pass as a silent blank; marked, it is refused with the path in its line.
                line["missing"] = path
            else:
                line["value"] = value
            yield json.dumps(line, default=str, allow_nan=False) if _finite(value) else _nan(line)
        return
    line = dict(base)
    assert mapping.message is not None  # the model validator requires it for a log mapping
    message = lookup(record, mapping.message)
    if message is not _MISSING:
        line["message"] = message if isinstance(message, str) else json.dumps(message)
    if mapping.severity is not None:
        severity = lookup(record, mapping.severity)
        if severity is not _MISSING:
            line["severity"] = _severity(severity)
    yield json.dumps(line, default=str)


def _finite(value: Any) -> bool:
    return not (isinstance(value, float) and not math.isfinite(value))


def _nan(line: dict[str, Any]) -> str:
    # JSON has no NaN; a non-finite reading is written as text so the contract refuses it
    # visibly ("value: Input should be a finite number") rather than the dump raising.
    return json.dumps({**line, "value": str(line["value"])}, default=str)


def map_document(document: Any, mapping: ApiMapping) -> Iterator[tuple[str | None, str]]:
    """Every line of one response page, as a connector stream's ``(group, line)`` pairs.

    The group is ``None``: a mapped line stands alone, so a chunk may end after any of them.
    """
    for record in records_of(document, mapping):
        for line in map_record(record, mapping):
            yield None, line
