"""Closed vocabularies shared across contracts.

Enum VALUES are wire format: they appear in JSON payloads and database rows.
Renaming a value is a breaking change to stored data and requires an ADR.
"""

from enum import StrEnum


class Lane(StrEnum):
    """Detection modality — which specialist lane an event or detection belongs to."""

    TELEMETRY = "telemetry"
    LOG = "log"
    FLOW = "flow"


class Severity(StrEnum):
    """Normalized event severity (5-level scale both normalizer modules converged on)."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Criticality(StrEnum):
    """How much an asset matters — feeds the prioritizer's device weight."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EventKind(StrEnum):
    """Shape of a detected anomaly episode (from run-length of consecutive windows)."""

    POINT = "point"
    CONTEXTUAL = "contextual"
    COLLECTIVE = "collective"


class AlertSeverity(StrEnum):
    """Analyst-facing alert severity (five labels, driven by absolute priority score)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class AlertStatus(StrEnum):
    """Minimal triage workflow state."""

    NEW = "new"
    ACKNOWLEDGED = "acknowledged"
    CLOSED = "closed"
