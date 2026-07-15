"""Contract v1 models (see docs/adr/0003-data-contracts-v1.md).

Design rules enforced here, not merely documented:
- All timestamps are timezone-aware and normalized to UTC (naive datetimes are rejected).
- Unknown fields are rejected (``extra="forbid"``) so construction typos fail loudly.
- Detections and alerts describe time *intervals*; a point anomaly is a zero-length window.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from iotshield.contracts.enums import (
    AlertSeverity,
    AlertStatus,
    Criticality,
    EventKind,
    Lane,
    Severity,
)


def _reject_epoch_number(value: Any) -> Any:
    # Pydantic's lax mode reads a bare number (or numeric string) as epoch
    # seconds, so a column-shifted CSV line silently becomes a 1970-dated event
    # instead of a MalformedRecord. A real ISO-8601 string never parses as a
    # float (it carries '-'/':'/'T'), so "float() accepts it" cleanly detects a
    # bare number — reject it so it lands in the malformed audit view.
    msg = "timestamp must be an ISO-8601 datetime, not an epoch number"
    if isinstance(value, (int, float)):
        raise ValueError(msg)
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return value  # not a bare number -> a genuine ISO string, allow it
        raise ValueError(msg)  # float() accepted it -> it's an epoch number
    return value


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware (naive datetimes are rejected)")
    return value.astimezone(UTC)


UtcDatetime = Annotated[
    datetime, BeforeValidator(_reject_epoch_number), AfterValidator(_ensure_aware_utc)
]
"""Timezone-aware datetime, normalized to UTC on validation.

The before-validator rejects epoch numbers (which pydantic would otherwise
silently accept); the after-validator requires aware input and normalizes to UTC.
"""

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
"""A float that rejects nan/inf — non-finite values break JSON serialization
(the API 500s) and storage (SQLite binds NaN as NULL). Named once so every
numeric contract field is finite-by-default (mirrors UtcDatetime for time)."""


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


def discovered_asset(asset_id: str) -> Asset:
    """A stub inventory entry for an asset id first seen in data (ADR-0005).

    The single definition of what auto-discovery produces, so the storage layer
    (filling absent rows on insert) and the store-agnostic compute phase (resolving
    asset criticalities in memory, no database) can never disagree about what a
    discovered asset looks like.
    """
    return Asset(
        asset_id=asset_id,
        name=asset_id,
        asset_type="unknown",
        criticality=Criticality.MEDIUM,  # neutral policy default, stated explicitly
        metadata={"auto_discovered": True},
    )


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
    value: FiniteFloat | None = None
    severity: Severity = Severity.INFO
    message: str | None = None
    raw: str = Field(description="Original record verbatim, for evidence and audit")
    metadata: dict[str, Any] = Field(default_factory=dict)


class MalformedRecord(_ContractModel):
    """An input record that failed normalization — captured, never dropped (ADR-0004).

    Normalization is the trust boundary: unparseable input is expected, frequent,
    and disproportionately suspicious, so failure is modeled as data, not as logs.
    """

    record_id: UUID = Field(default_factory=uuid4)
    raw: str = Field(description="Original record verbatim")
    error: str = Field(min_length=1, description="Why normalization rejected it")
    source_format: str = Field(min_length=1)
    origin: str = Field(min_length=1, description="Provenance, e.g. file path or stream name")
    created_at: UtcDatetime = Field(default_factory=_utc_now)


class DetectionResult(_WindowedContract):
    """A detector's positive judgment about an asset over a time window.

    ``score`` and ``threshold`` are the method's raw values; scores are not
    comparable across methods — downstream consumers reason about the margin.
    Contributing events are recoverable by an inclusive-window query on
    (asset_id, window_start..window_end, lane) — the bound must be closed, since
    a point anomaly is a zero-length window; they are not stored inline
    (ADR-0003 D4).
    """

    detection_id: UUID = Field(default_factory=uuid4)
    asset_id: str = Field(min_length=1)
    lane: Lane
    method: str = Field(min_length=1, description='Detector name, e.g. "lstm_ae"')
    score: FiniteFloat
    threshold: FiniteFloat
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
    priority_score: FiniteFloat = Field(ge=0.0, le=100.0)
    threat_class: str = "unknown"
    rationale: str = Field(min_length=1, description="Human-readable why, always present")
    score_components: dict[str, Any] = Field(default_factory=dict)
    detection_ids: list[UUID] = Field(min_length=1)
    recommended_action: str | None = None
    status: AlertStatus = AlertStatus.NEW
    created_at: UtcDatetime = Field(default_factory=_utc_now)

    @field_validator("detection_ids")
    @classmethod
    def _unique_detection_ids(cls, value: list[UUID]) -> list[UUID]:
        # Lineage is a set of distinct detections; de-dupe order-preserving so a
        # repeated id can't crash the alert<->detection junction's composite PK.
        return list(dict.fromkeys(value))
