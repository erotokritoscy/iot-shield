"""METER vocabulary: what crosses normalize -> detect for a utility meter (ADR-0030).

The second lane vocabulary module, for the same reason ``flow.py`` is the first: a
utility meter's payload outgrows the contract's own fields. A meter reports a
**cumulative counter**, but the meaningful observation is **consumption over an
interval** — and the contract's ``value`` holds one number, not a counter and a delta.

Three facts have to survive the normalize->detect boundary, and none of them fits
``NormalizedEvent``'s typed fields:

- the **raw counter**, so the boundary stays *invertible* (a renderer is the inverse of
  a normalizer — CLAUDE.md) and so a future data-quality detector can see a rollback
  without a second ingest path;
- the **interval width**, because a consumption number is meaningless without one;
- the **utility** (water vs electricity), because one detector serves both and branches
  only where it must — it is carried through rather than split into two adapters.

So they ride in ``NormalizedEvent.metadata`` behind this typed record, exactly as
``FlowRecord`` does for the FLOW lane.

Timestamp convention (ADR-0030 D3)
----------------------------------
A consumption event is stamped at the interval's **END**: ``consumption[t] = counter[t]
- counter[t-1]``, which is the upstream convention (``meterdetect.cleaning``) and the
one an hourly meter export already implies. Note this is the *opposite* choice from the
FLOW lane, which stamps a flow at its START. Both are defensible for their modality and
neither is inferable from the timestamp alone, so ``interval_hours`` travels with every
event and the convention is stated per lane rather than assumed globally.

Provenance: the canonical contract this mirrors is
``meterdetect/canonical.py`` (Panayiotis Cyprus, ML-Based Anomaly and Attack Detection
for Smart Meter Telemetry, upstream commit d3c61a6). His frame requires ``consumption``,
``meter_id``, ``meter_type`` and ``unit`` and treats the cumulative counter as a
passthrough column; this module is the same agreement expressed as event metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Any

METADATA_KEY = "meter"
"""Key under which a ``MeterReading`` rides in ``NormalizedEvent.metadata``."""


class MeterType(StrEnum):
    """The utility a meter measures.

    A str enum rather than a ``Lane`` member: a meter is TELEMETRY like any other
    sensor, and water-vs-electricity is a property of the *device*, not of the
    modality. Adding a third utility (gas) is a member here plus its unit and metric
    below, and costs **no contract change** — which is the whole reason this is not
    baked into the lane vocabulary. It does still cost the registry's
    ``Literal["water", "electricity"]`` (``pipeline/registry.py``), which is what
    ``/modules`` offers as choices; miss that and a ``meter_type: gas`` config works
    while the compose page keeps offering two.
    """

    WATER = "water"
    ELECTRICITY = "electricity"


UNIT_BY_TYPE: dict[MeterType, str] = {
    MeterType.WATER: "m3",
    MeterType.ELECTRICITY: "kwh",
}
"""The unit each utility's counter is denominated in."""

TYPE_BY_UNIT: dict[str, MeterType] = {unit: kind for kind, unit in UNIT_BY_TYPE.items()}

METRIC_BY_UNIT: dict[str, str] = {
    "m3": "hourly_consumption_m3",
    "kwh": "hourly_consumption_kwh",
}
"""``NormalizedEvent.metric`` per unit.

The unit is in the metric name rather than only in metadata because the metric is what
the detector pivots on and what the dashboard labels an axis with — an electricity
reading mislabelled ``_m3`` is the kind of error that survives every type check.
"""


class MeterFieldError(ValueError):
    """A meter payload is present but unusable; the message names the offending field."""


def metric_for(unit: str) -> str:
    """The metric name for a unit. Units come from ``UNIT_BY_TYPE``, so this is total."""
    return METRIC_BY_UNIT[unit]


@dataclass(frozen=True, slots=True)
class MeterReading:
    """One meter's consumption over one closed interval.

    ``consumption`` is the event's ``value``; everything else here is what the contract
    cannot type. Frozen because it is a record of an observation, not a builder.
    """

    meter_type: MeterType
    unit: str
    counter: float
    """The cumulative counter reading at the interval's END.

    What makes this boundary invertible: cumulating the consumption series reproduces
    the counter only up to an unknown constant, and a rollback destroys even that. With
    the counter carried per event, a renderer can reproduce the source bytes exactly and
    a data-quality detector can see the negative jump.
    """
    interval_hours: float = 1.0

    def to_metadata(self) -> dict[str, Any]:
        """JSON-safe mapping for ``NormalizedEvent.metadata[METADATA_KEY]``.

        Plain JSON types only: this crosses into Postgres JSONB, which rejects the
        bare ``NaN``/``Infinity`` tokens Python's json module is willing to emit.
        """
        return {
            "meter_type": str(self.meter_type),
            "unit": self.unit,
            "counter": float(self.counter),
            "interval_hours": float(self.interval_hours),
        }

    @classmethod
    def from_metadata(cls, payload: object) -> MeterReading:
        """Rebuild a reading from a metadata payload, naming what is wrong if it fails.

        The **strict** half, mirroring ``flow.FlowRecord.from_metadata``. A payload that is
        present but malformed is writer/reader schema drift, and the only symptom of
        swallowing it is a detector that silently sees nothing — which is precisely the
        failure a typed lane vocabulary exists to prevent. Callers that legitimately expect
        non-meter events use ``reading_of``.
        """
        if not isinstance(payload, dict):
            raise MeterFieldError(f"meter payload must be an object, got {type(payload).__name__}")

        def number(name: str, default: float | None = None) -> float:
            value = payload.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise MeterFieldError(
                    f"meter payload field {name!r} must be a number, got {value!r}"
                )
            return float(value)

        raw_type = payload.get("meter_type")
        unit = payload.get("unit")
        if not isinstance(raw_type, str):
            raise MeterFieldError(
                f"meter payload field 'meter_type' must be a string, got {raw_type!r}"
            )
        if not isinstance(unit, str):
            raise MeterFieldError(f"meter payload field 'unit' must be a string, got {unit!r}")
        try:
            meter_type = MeterType(raw_type)
        except ValueError as exc:
            raise MeterFieldError(f"unknown meter_type {raw_type!r}") from exc
        interval_hours = number("interval_hours", 1.0)
        if interval_hours <= 0 or not isfinite(interval_hours):
            # A width is what consumption is divided by and what the detector's grid is
            # built from, so a non-positive one is not a rounding concern: zero reached
            # pandas' date_range as freq=Timedelta(0) and took the whole run down with a
            # ZeroDivisionError — neither NotFitted nor ValueError, so nothing upstream
            # read it as a data problem — while a negative one produced an empty grid and
            # the meter silently yielded no detections. Refused here so `reading_of` filters
            # the record out and it is quarantined at the boundary, not fatal (ADR-0033).
            raise MeterFieldError(
                f"meter payload field 'interval_hours' must be a positive, finite number of"
                f" hours, got {interval_hours!r}"
            )
        return cls(
            meter_type=meter_type,
            unit=unit,
            counter=number("counter"),
            interval_hours=interval_hours,
        )


def reading_of(event_metadata: dict[str, Any]) -> MeterReading | None:
    """The ``MeterReading`` an event carries, or ``None`` if it carries none.

    The **tolerant** half: a detector is handed whatever the lane pooled, so "this is not a
    meter event" must be an ordinary answer rather than an exception mid-run.
    """
    payload = event_metadata.get(METADATA_KEY)
    if payload is None:
        return None
    try:
        return MeterReading.from_metadata(payload)
    except MeterFieldError:
        return None
