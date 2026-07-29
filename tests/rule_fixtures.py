"""Shared builders for the rule-detector suites.

Three suites need "an event a rule can judge" and "a rule file": the language tests, the
adapter tests, and the enrichment-promotion tests. Each was growing its own copy of the
same twenty lines — the situation ``team3_fixtures`` records as "how they quietly drifted
apart", and the two flow-event builders had already drifted on their signature.

Events are built through ``FlowRecord``/``orient`` rather than assembled by hand, because a
rule about ports is only meaningful against records oriented the way the normalizer
orients them (ADR-0023 D2).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from iotshield.contracts import Lane, NormalizedEvent, Severity
from iotshield.flow import METADATA_KEY, Direction, FlowRecord, orient

T0 = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)

ASSET = "10.0.0.24"


def _flow_event(record: FlowRecord, *, offset_s: int, asset_id: str) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id=asset_id,
        source_type=Lane.FLOW,
        source_format="zeek-conn",
        metric="bytes_out",
        value=float(record.bytes_out),
        message=record.summary(),
        raw="flow",
        metadata={METADATA_KEY: record.to_metadata()},
    )


def flow_event(offset_s: int = 0, *, asset: str = ASSET, **overrides: Any) -> NormalizedEvent:
    """A FLOW event whose record fields are set directly — for rules about bytes/services."""
    fields: dict[str, Any] = {
        "asset_ip": asset,
        "peer_ip": "203.0.113.5",
        "protocol": "tcp",
        "direction": Direction.OUTBOUND,
        "peer_port": 443,
        "bytes_out": 1200,
        "service": "ssl",
    }
    fields.update(overrides)
    return _flow_event(FlowRecord(**fields), offset_s=offset_s, asset_id=asset)


def oriented_event(
    *, src_ip: str, dst_ip: str, dst_port: int, offset_s: int = 0
) -> NormalizedEvent:
    """A FLOW event built the way the normalizer builds one — through ``orient``.

    Constructing a ``FlowRecord`` by hand lets a test assert a shape the pipeline can never
    produce. Orientation decides which endpoint is the asset and therefore which side each
    port lands on, so any rule about ports must be tested against its output.
    """
    record = orient(src_ip=src_ip, dst_ip=dst_ip, dst_port=dst_port, src_port=51000, protocol="tcp")
    return _flow_event(record, offset_s=offset_s, asset_id=record.asset_ip)


def log_event(
    offset_s: int = 0,
    message: str = "hello",
    severity: Severity = Severity.INFO,
    *,
    asset: str = ASSET,
) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id=asset,
        source_type=Lane.LOG,
        source_format="syslog-rfc5424",
        severity=severity,
        message=message,
        raw=message,
    )


def telemetry_event(
    offset_s: int = 0, metric: str = "Pressure", value: float = 42.0, *, asset: str = "pump-02"
) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id=asset,
        source_type=Lane.TELEMETRY,
        source_format="stub-csv",
        metric=metric,
        value=value,
        raw="row",
    )


def rule_document(**overrides: Any) -> dict[str, Any]:
    """A minimal valid rule file, as a mapping; override any key."""
    document: dict[str, Any] = {
        "id": "test-rule",
        "title": "Test Rule",
        "lane": "flow",
        "description": "A rule for tests.",
        "severity_hint": "medium",
        "confidence": "high",
        "detection": {
            "selection": {"flow.bytes_out": {"operator": ">", "value": 1000}},
            "condition": "selection",
        },
    }
    document.update(overrides)
    return document


def one_selection(field: str, operator: str, value: Any, **overrides: Any) -> dict[str, Any]:
    """A rule document whose single selection tests one field — the common shape."""
    return rule_document(
        detection={
            "selection": {field: {"operator": operator, "value": value}},
            "condition": "selection",
        },
        **overrides,
    )


def write_ruleset(root: Path, *documents: dict[str, Any]) -> str:
    """Write rule documents into ``root`` and return it as a ``ruleset`` path."""
    for index, document in enumerate(documents):
        (root / f"rule{index}.yml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return str(root)
