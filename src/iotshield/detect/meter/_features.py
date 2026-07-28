"""Meter feature matrix — vendored from ``meterdetect/features.py``.

Upstream: Panayiotis Cyprus, *ML-Based Anomaly and Attack Detection for Smart Meter
Telemetry*, commit ``d3c61a6``. Copied with **two deliberate adaptations**, each noted at
its definition below:

1. ``build_features`` takes the profile array from ``_profile.py`` instead of a frame, to
   match this package's series-in/frame-out shape.
2. Rolling statistics are computed on the **reindexed hourly grid** rather than on whatever
   rows happen to be present, so a 168-hour window means 168 hours of wall clock and not
   168 surviving readings (see ``regrid``).

Only the lean-four path is vendored. Upstream's calendar features (hour-of-day, day-of-week,
holidays) are **not** here, because the model does not use them — carrying them would mean
maintaining a Cyprus holiday table for a feature that never reaches the forest. Upstream's
table is also hardcoded for 2024-2026 and silently empties from 2027, which is reported to
him rather than fixed in a copy we do not call (ADR-0028 D8.2).

THE FEATURE SET, and why it is small
------------------------------------
Four features: ``consumption``, ``deviation`` (the hour-of-week residual),
``roll_mean_168`` and ``roll_std_168``. Upstream compared 4 / 8 / 12 / 16-feature sets and
the lean four won on its own data, for two reasons worth keeping: fewer features means
fewer rows dropped for a NaN, so gap-adjacent hours survive; and extra features gave the
model spurious combinations to isolate, which *buried* the genuine spikes (they ranked
~21st with four features and ~55-120th with sixteen). Raw counters are excluded because a
monotonic counter would let the model split trivially on "counter is large".

Rolling statistics **lag by one hour** (``shift(1)``) so the hour being judged never
contributes to the description of its own normal.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from iotshield.detect.meter._profile import add_deviation

WEEK_HOURS = 168


def model_columns(window: int) -> tuple[str, ...]:
    """The lean-4 column names for a given rolling window.

    The window is a hyperparameter, so two of the four column names depend on it. Deriving
    them in one place is what stops a fit at ``window=336`` being scored against columns
    named for 168 — which would not raise, it would silently mean something else.
    """
    return ("consumption", "deviation", f"roll_mean_{window}", f"roll_std_{window}")


def regrid(consumption: pd.Series, interval_hours: float) -> pd.Series:
    """Reindex a consumption series onto its continuous hourly grid.

    Adaptation 2, and the reason it matters: the normalizer emits **no event** for a gap
    (consumption across it is unknown), so the series arriving here has holes. Rolling a
    168-row window over a holed series would silently mix in readings from weeks earlier.
    Reindexing restores the missing hours as NaN, which the rolling statistics are already
    NaN-aware about and which the model drops before fitting — never imputed, only absent.
    """
    index = consumption.index
    if not isinstance(index, pd.DatetimeIndex) or len(consumption) < 2:
        return consumption
    # The index is sorted by construction, so the ends are its bounds — min()/max() would
    # be two extra full scans per meter.
    grid = pd.date_range(
        index[0], index[-1], freq=pd.Timedelta(hours=interval_hours), name=index.name
    )
    return consumption.reindex(grid)


def build_features(
    consumption: pd.Series,
    profile: np.ndarray,
    *,
    window: int,
    interval_hours: float,
) -> pd.DataFrame:
    """The lean-4 feature matrix for one meter, on its continuous hourly grid.

    ``profile`` is the meter's hour-of-week table (``_profile.build_profile``), passed in
    rather than rebuilt so a caller can learn "normal" on a clean stretch and apply it to
    a dirty one — which is exactly what fitting on clean history requires.
    """
    gridded = regrid(consumption, interval_hours)
    frame = pd.DataFrame({"consumption": gridded})
    # Lag by one hour first: the hour under judgment must not describe its own normal.
    lagged = gridded.shift(1).rolling(window, min_periods=max(2, window // 4))
    frame[f"roll_mean_{window}"] = lagged.mean()
    frame[f"roll_std_{window}"] = lagged.std()
    return add_deviation(frame, profile)


def feature_matrix(
    features: pd.DataFrame, columns: Sequence[str]
) -> tuple[np.ndarray, pd.DatetimeIndex]:
    """The model columns as a float array, with non-finite rows dropped, plus their stamps.

    Rows are dropped rather than imputed: an invented value that scores as an anomaly would
    be a detection about data that never existed.

    Returns numpy directly because every caller wants an array — going via
    ``DataFrame.replace().dropna()`` cost two full frame copies to hand back something that
    was then converted anyway.
    """
    missing = [name for name in columns if name not in features.columns]
    if missing:
        raise KeyError(f"feature matrix is missing column(s): {missing}")
    index = features.index
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(f"expected a DatetimeIndex, got {type(index).__name__}")
    values = features.loc[:, list(columns)].to_numpy(dtype=float)
    keep = np.isfinite(values).all(axis=1)
    return values[keep], index[keep]
