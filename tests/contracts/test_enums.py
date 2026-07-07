"""Enum values are wire format (JSON + DB). These tests freeze them: a failure
here means stored data would break — that change needs an ADR, not a rename."""

from iotshield.contracts import (
    AlertSeverity,
    AlertStatus,
    Criticality,
    EventKind,
    Lane,
    Severity,
)


def test_lane_wire_values() -> None:
    assert {e.value for e in Lane} == {"telemetry", "log", "flow"}


def test_severity_wire_values() -> None:
    assert {e.value for e in Severity} == {"debug", "info", "warning", "error", "critical"}


def test_criticality_wire_values() -> None:
    assert {e.value for e in Criticality} == {"low", "medium", "high", "critical"}


def test_event_kind_wire_values() -> None:
    assert {e.value for e in EventKind} == {"point", "contextual", "collective"}


def test_alert_severity_wire_values() -> None:
    assert {e.value for e in AlertSeverity} == {"low", "medium", "high", "critical", "emergency"}


def test_alert_status_wire_values() -> None:
    assert {e.value for e in AlertStatus} == {"new", "acknowledged", "closed"}
