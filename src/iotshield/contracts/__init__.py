"""Frozen data contracts: NormalizedEvent, DetectionResult, PrioritizedAlert, Asset.

These are the product's load-bearing walls. Every port speaks only these objects.
Changing anything here requires an ADR in docs/adr/ (see CLAUDE.md golden rule 1).
Contract v1 rationale: docs/adr/0003-data-contracts-v1.md.
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
    "NormalizedEvent",
    "PrioritizedAlert",
    "Severity",
    "UtcDatetime",
]
