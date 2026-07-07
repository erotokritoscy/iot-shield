"""Frozen data contracts: NormalizedEvent, MalformedRecord, DetectionResult,
PrioritizedAlert, Asset.

These are the product's load-bearing walls. Every port speaks only these objects.
Changing anything here requires an ADR in docs/adr/ (see CLAUDE.md golden rule 1).
Rationale: docs/adr/0003-data-contracts-v1.md and docs/adr/0004-ports-and-malformed-record.md.
"""

from iotshield.contracts.enums import (
    AlertSeverity,
    AlertStatus,
    Criticality,
    EventKind,
    Lane,
    Severity,
)
from iotshield.contracts.models import (
    Asset,
    DetectionResult,
    MalformedRecord,
    NormalizedEvent,
    PrioritizedAlert,
    UtcDatetime,
)

__all__ = [
    "AlertSeverity",
    "AlertStatus",
    "Asset",
    "Criticality",
    "DetectionResult",
    "EventKind",
    "Lane",
    "MalformedRecord",
    "NormalizedEvent",
    "PrioritizedAlert",
    "Severity",
    "UtcDatetime",
]
