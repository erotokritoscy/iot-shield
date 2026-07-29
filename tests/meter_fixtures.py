"""Synthetic meter events for the meter detector's tests.

Deliberately never loads the committed artifact: a port case that depended on
``artifacts/demo_meter.json`` would fail for two unrelated reasons (a broken adapter, or a
regenerated artifact) and could not tell them apart. Everything here is built in-process.

The parameters are also deliberately small — a 24-hour rolling window and a loose
false-alarm budget — so the fixtures stay fast and so a few hundred hours is enough clean
history to calibrate against. Production defaults are a 168-hour window and a budget that
needs ten weeks; both are exercised by the demo artifact instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import numpy as np

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.detect.meter import MeterIForestDetector, MeterParams
from iotshield.meter import METADATA_KEY, MeterReading, MeterType, metric_for

METER_ID = "meter-01"
SOURCE_FORMAT = "eoa-meter-csv"
T0 = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)

# A domestic water profile in m3/h: idle overnight, a morning and an evening peak. Small
# and smooth on purpose — the fixtures test the port's laws, not detection quality.
SHAPE = np.array(
    [
        *[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.004, 0.018, 0.026, 0.014, 0.009, 0.008],
        *[0.011, 0.009, 0.006, 0.005, 0.007, 0.012, 0.022, 0.028, 0.019, 0.010, 0.004, 0.0],
    ]
)

# Small window + loose budget: see the module docstring.
FIXTURE_PARAMS: dict[str, object] = {
    "profile_window_hours": 24,
    "min_train_hours": 168,
    "fp_budget": 0.02,
    "max_samples": 64,
}


def meter_event(stamp: datetime, consumption: float, counter: float) -> NormalizedEvent:
    reading = MeterReading(
        meter_type=MeterType.WATER, unit="m3", counter=counter, interval_hours=1.0
    )
    return NormalizedEvent(
        timestamp=stamp,
        asset_id=METER_ID,
        source_type=Lane.TELEMETRY,
        source_format=SOURCE_FORMAT,
        metric=metric_for("m3"),
        value=consumption,
        raw=f"{stamp.isoformat()},{METER_ID},{counter:.3f}",
        metadata={METADATA_KEY: reading.to_metadata()},
    )


def meter_events(
    hours: int,
    *,
    seed: int = 0,
    start: datetime = T0,
    leak_at: int | None = None,
    leak_hours: int = 3,
    leak_rate: float = 0.06,
) -> list[NormalizedEvent]:
    """``hours`` of hourly consumption events, optionally with an overnight leak.

    ``leak_at`` is an hour offset from ``start``; the leak holds ``leak_rate`` m3/h, well
    above the profile's evening peak, so it is anomalous in magnitude *and* in timing.
    """
    rng = np.random.default_rng(seed)
    events: list[NormalizedEvent] = []
    counter = 100.0
    for hour in range(hours):
        stamp = start + timedelta(hours=hour)
        base = float(SHAPE[stamp.hour])
        if base <= 0.0:
            used = 0.0 if rng.random() < 0.94 else float(abs(rng.normal(0.001, 0.0004)))
        else:
            used = max(0.0, base * (1.0 + float(rng.normal(0.0, 0.10))))
        if leak_at is not None and leak_at <= hour < leak_at + leak_hours:
            used = leak_rate
        counter = round(counter + used, 4)
        events.append(meter_event(stamp, round(used, 4), counter))
    return events


def clean_events(hours: int = 24 * 21, seed: int = 7) -> list[NormalizedEvent]:
    """Three weeks of anomaly-free history — enough to fit and to calibrate on."""
    return meter_events(hours, seed=seed)


def leaking_events(hours: int = 24 * 10, seed: int = 11) -> list[NormalizedEvent]:
    """Ten days ending in a three-hour overnight leak, guaranteed to fire."""
    # Hour 24*9 + 2 is 02:00 on the last-but-one day: past the 24h rolling warm-up, and
    # in a bucket whose profile expects nothing.
    return meter_events(hours, seed=seed, leak_at=24 * 9 + 2)


def make_params(**overrides: object) -> MeterParams:
    return MeterParams.model_validate({**FIXTURE_PARAMS, **overrides})


def make_detector(events: Sequence[NormalizedEvent] | None = None) -> MeterIForestDetector:
    """A detector fitted in-process on clean fixture history."""
    return MeterIForestDetector(make_params()).fit(list(events) if events else clean_events())
