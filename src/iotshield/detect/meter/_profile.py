"""The per-meter hour-of-week PROFILE and its deviation feature — vendored.

Copied from ``meterdetect/meter_profile.py`` (Panayiotis Cyprus, *ML-Based Anomaly and
Attack Detection for Smart Meter Telemetry*, upstream commit ``d3c61a6``) with **one
deliberate adaptation**: ``build_profile`` takes the consumption series and its index
rather than a frame with a ``'consumption'`` column, because our events arrive as a
series and the column-name coupling bought nothing. The statistics are unchanged.

The idea, which is the domain asset: a meter's normal is not a level, it is a **weekly
fingerprint**. The profile is a 24x7 table holding the MEDIAN consumption for each
(hour-of-day, day-of-week) bucket — "what this meter typically uses at 3am on a Tuesday".
Median, not mean, so an occasional burst does not inflate the baseline it is supposed to
stand out from. Zeros are KEPT (a genuinely idle hour is real data); only NaN is excluded,
so gaps do not bias the estimate.

``deviation = consumption - profile_expected`` is then the feature that catches the case a
magnitude threshold cannot: ordinary usage at an extraordinary *time*.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

HOURS = 24
DAYS = 7


def as_datetime_index(index: pd.Index) -> pd.DatetimeIndex:
    """Narrow an index to ``DatetimeIndex``, or say so.

    Everything in this package is indexed by timestamp, but pandas' stubs type ``.index``
    as the base ``Index``, so the narrowing has to happen somewhere. It happens here, once,
    and the sibling modules import it.
    """
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(f"expected a DatetimeIndex, got {type(index).__name__}")
    return index


def _buckets(index: pd.Index) -> tuple[np.ndarray, np.ndarray]:
    """The (hour-of-day, day-of-week) coordinates of each timestamp.

    The vectorized accessors are used rather than iterating the index: they exist at
    runtime and return plain int arrays, and this runs twice per meter over its whole
    history. The ``type: ignore`` is for the installed pandas stubs, which do not model
    them — the same trade already made in ``_iforest.py``.
    """
    stamps = as_datetime_index(index)
    return (
        np.asarray(stamps.hour, dtype=int),  # type: ignore[attr-defined]
        np.asarray(stamps.dayofweek, dtype=int),  # type: ignore[attr-defined]
    )


def build_profile(consumption: pd.Series) -> np.ndarray:
    """The 24x7 median-consumption profile for one meter.

    Returns an ``(24, 7)`` array indexed ``[hour_of_day, day_of_week]`` (0 = Monday).
    A bucket with no observed reading is ``NaN`` — meaning "unknown", never zero, which
    would claim the meter is idle then.
    """
    hours, days = _buckets(consumption.index)
    frame = pd.DataFrame(
        {"consumption": consumption.to_numpy(dtype=float), "hour": hours, "dow": days}
    )
    table = frame.pivot_table(index="hour", columns="dow", values="consumption", aggfunc="median")
    return table.reindex(index=range(HOURS), columns=range(DAYS)).to_numpy(dtype=float)


def expected_from_profile(index: pd.Index, profile: np.ndarray) -> np.ndarray:
    """Each timestamp's profile-expected value, by (hour, dow) lookup."""
    hours, days = _buckets(index)
    return profile[hours, days]


def add_deviation(frame: pd.DataFrame, profile: np.ndarray) -> pd.DataFrame:
    """Attach the ``deviation`` column: consumption minus its hour-of-week expectation.

    Mutates ``frame`` rather than copying it — its only caller (``_features.build_features``)
    builds the frame immediately before this and owns it outright, so a defensive copy was
    two n-length float copies per meter for nothing. ``profile_expected`` is deliberately
    not materialized as a column: no model column uses it, so it would be built and then
    dropped by ``feature_matrix``.
    """
    frame["deviation"] = frame["consumption"] - expected_from_profile(frame.index, profile)
    return frame
