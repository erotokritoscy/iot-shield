"""Contract v1 models (see docs/adr/0003-data-contracts-v1.md).

Design rules enforced here, not merely documented:
- All timestamps are timezone-aware and normalized to UTC (naive datetimes are rejected).
- Unknown fields are rejected (``extra="forbid"``) so construction typos fail loudly.
- Detections and alerts describe time *intervals*; a point anomaly is a zero-length window.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Self
from uuid import UUID, uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from iotshield.contracts.enums import (
    AlertSeverity,
    AlertStatus,
    Criticality,
    EventKind,
    Lane,
    Severity,
)


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware (naive datetimes are rejected)")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_aware_utc)]
"""Timezone-aware datetime, normalized to UTC on validation."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _ContractModel(BaseModel):
    """Base for all contracts: reject unknown fields so typos fail at construction."""

    model_config = ConfigDict(extra="forbid")


class _WindowedContract(_ContractModel):
    """A claim about a time interval on an asset's timeline.

    The shared "window" concept for detections and alerts: ordered UTC bounds,
    where a point anomaly is a zero-length window (start == end). Window rules
    live here, once.
    """

    window_start: UtcDatetime
    window_end: UtcDatetime

    @model_validator(mode="after")
    def _window_ordered(self) -> Self:
        if self.window_end < self.window_start:
            raise ValueError("window_end must not precede window_start")
        return self


class Asset(_ContractModel):
    """A monitored device or system — the 'who' everything is correlated on."""

    asset_id: str = Field(min_length=1, description="Natural key as it appears in raw data")
    name: str = Field(min_length=1)
    asset_type: str = Field(min_length=1, description='e.g. "pump", "gateway", "plc"')
    location: str | None = None
    criticality: Criticality = Criticality.MEDIUM
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedEvent(_ContractModel):
    """One normalized observation: the shared envelope every raw record becomes.

    One numeric value per event (ADR-0003 D1): a multivariate reading fans out
    into one event per metric; detector adapters pivot back as needed.
    """

    event_id: UUID = Field(default_factory=uuid4)
    timestamp: UtcDatetime
    asset_id: str = Field(min_length=1)
    source_type: Lane
    source_format: str = Field(min_length=1, description='Fine-grained origin, e.g. "syslog"')
    metric: str | None = Field(default=None, description='Channel/action name, e.g. "Pressure"')
    value: float | None = None
    severity: Severity = Severity.INFO
    message: str | None = None
    raw: str = Field(description="Original record verbatim, for evidence and audit")
    metadata: dict[str, Any] = Field(default_factory=dict)


class DetectionResult(_WindowedContract):
    """A detector's positive judgment about an asset over a time window.

    ``score`` and ``threshold`` are the method's raw values; scores are not
    comparable across methods — downstream consumers reason about the margin.
    Contributing events are recoverable by query on (asset_id, window, lane);
    they are not stored inline (ADR-0003 D4).
    """

    detection_id: UUID = Field(default_factory=uuid4)
    asset_id: str = Field(min_length=1)
    lane: Lane
    method: str = Field(min_length=1, description='Detector name, e.g. "lstm_ae"')
    score: float
    threshold: float
    event_kind: EventKind | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PrioritizedAlert(_WindowedContract):
    """The analyst-facing decision: ranked, labeled, explained.

    ``detection_ids`` is the lineage; more than one entry means corroboration.
    Every detection bucket becomes an alert — ranking, not a hidden gate,
    decides what analysts look at first (ADR-0003 D3).
    """

    alert_id: UUID = Field(default_factory=uuid4)
    asset_id: str = Field(min_length=1)
    severity: AlertSeverity
    priority_score: float = Field(ge=0.0, le=100.0)
    threat_class: str = "unknown"
    rationale: str = Field(min_length=1, description="Human-readable why, always present")
    score_components: dict[str, Any] = Field(default_factory=dict)
    detection_ids: list[UUID] = Field(min_length=1)
    recommended_action: str | None = None
    status: AlertStatus = AlertStatus.NEW
    created_at: UtcDatetime = Field(default_factory=_utc_now)
