"""Model-agnostic feature attribution — vendored from ``meterdetect/attribution.py``.

Upstream: Panayiotis Cyprus, *ML-Based Anomaly and Attack Detection for Smart Meter
Telemetry*, commit ``d3c61a6``. Copied with **one deliberate adaptation**: the per-feature
statistics are carried as plain arrays aligned to the model's column order (rather than a
dict keyed by column name), so they can be persisted in the fitted artifact alongside the
threshold and cannot drift out of alignment with it.

Why this exists, and why it is the explanation an analyst reads
--------------------------------------------------------------
An Isolation Forest answers "is this hour anomalous?" and nothing more. This answers
"**which** features made it stand out" — and it does so *without* consulting the model:
each feature is standardized against that meter's own robust distribution (median / MAD),
and the largest deviations are reported. That makes it honest about what it is (a
statement about the data, not a claim about the model's reasoning) and it makes it
reusable: the same routine explains an Isolation Forest, an LOF, or a rule.

This is what fills ``DetectionResult.evidence`` for the meter lane.

The MAD scale is floored relative to the feature's own magnitude, because a near-constant
feature has ``MAD ~ 0`` and would otherwise manufacture astronomical z-scores.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

MAD_TO_SIGMA = 1.4826
"""Scales a median-absolute-deviation to a standard deviation for a normal distribution."""

MIN_ABS_Z = 2.0
"""Below this, a feature sits inside its own normal range and did not drive the alert."""

MAX_ABS_Z = 999.0
"""Reporting ceiling for a standardized deviation.

Past this the number stops being information: "55,000 standard deviations" and "999" say
the same thing — the feature was never remotely like this during training — and the larger
figure falsely implies precision. Clamping keeps ``evidence`` readable and keeps a
near-degenerate feature from dominating the ranking purely through its scale.
"""

_DEGENERATE = 1e-9
"""Relative tolerance below which a MAD is treated as collapsed rather than small."""

# Plain-language phrase per (feature, direction). The evidence should explain the flag in
# terms an analyst can act on, not dump feature names and z-scores. Each names the dominant
# SIGNAL only — deliberately not a root cause, which this method cannot know.
PHRASES: dict[str, dict[str, str]] = {
    "consumption": {
        "high": "unusually high consumption",
        "low": "unusually low consumption",
    },
    "deviation": {
        "high": "consumption far above the usual level for this hour of the week",
        "low": "consumption far below the usual level for this hour of the week",
    },
    "roll_mean_168": {
        "high": "consumption elevated versus the past week",
        "low": "consumption depressed versus the past week",
    },
    "roll_std_168": {
        "high": "unusually volatile usage recently",
        "low": "unusually flat usage recently",
    },
}


def robust_stats(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-column (median, robust scale) over the training rows.

    Robust to the anomalies themselves, which is the point: a mean/std baseline computed
    over data containing the events we hunt would widen to accommodate them.

    **Adaptation (a bug fix over upstream).** A MAD collapses whenever more than half the
    rows share a value — which is the *normal* case for a meter, where ``deviation`` is
    zero through every idle hour. Upstream guards this with a floor proportional to the
    feature's own median, and tests the MAD against ``> 0``. Both fail on real data: the
    residual of ``consumption - profile_expected`` is not exactly zero but ~1e-14 of
    floating-point residue, so it passes ``> 0``, the standard-deviation fallback never
    fires, and the median-relative floor (1e-6 for a zero-median feature) turns a 0.055
    deviation into **z = 55,000**. Measured on the demo meter before this fix.

    So a MAD is treated as *collapsed* when it is negligible **relative to the column's own
    spread**, not when it is exactly zero, and the fallback is the standard deviation —
    which is what actually describes variation in a zero-inflated column.
    """
    values = np.asarray(matrix, dtype=float)
    median = np.median(values, axis=0)
    mad = np.median(np.abs(values - median), axis=0)
    mad_scale = MAD_TO_SIGMA * mad
    spread = values.std(axis=0)

    collapsed = mad_scale <= _DEGENERATE * (np.abs(median) + spread)
    scale = np.where(collapsed, spread, mad_scale)
    # A column genuinely constant in training has no scale at all; keep it finite so the
    # arithmetic stays defined, and let MAX_ABS_Z report any departure as off-scale.
    floor = _DEGENERATE * (np.abs(median) + 1.0)
    scale = np.maximum(scale, floor)
    return median, scale


def attribute_row(
    row: np.ndarray,
    columns: Sequence[str],
    median: np.ndarray,
    scale: np.ndarray,
    *,
    top_k: int,
    min_abs_z: float = MIN_ABS_Z,
) -> list[dict[str, Any]]:
    """The top contributing features for one hour, ranked by ``|robust z|``.

    Only features at or past ``min_abs_z`` are reported: listing a feature sitting at its
    own normal would imply it contributed when it did not.
    """
    z_scores = np.clip((np.asarray(row, dtype=float) - median) / scale, -MAX_ABS_Z, MAX_ABS_Z)
    ranked = sorted(
        (
            {
                "feature": name,
                "value": round(float(value), 4),
                "z": round(float(z), 2),
                "direction": "high" if z >= 0 else "low",
            }
            for name, value, z in zip(columns, row, z_scores, strict=True)
            if np.isfinite(z) and abs(z) >= min_abs_z
        ),
        key=lambda item: abs(float(item["z"])),
        reverse=True,
    )
    return ranked[:top_k]


def summarise(top_features: Sequence[dict[str, Any]]) -> str:
    """Plain-language reason for the flag: no feature names, no z-scores.

    Summarises whatever it is given — the caller already chose how many features to report
    via ``attribute_row``'s ``top_k``, and a second limit here could only silently disagree
    with the first.
    """
    if not top_features:
        return "no single feature stood out; flagged on the combination"
    return "; ".join(
        PHRASES.get(str(item["feature"]), {}).get(
            str(item["direction"]), f"{item['direction']} {item['feature']}"
        )
        for item in top_features
    )
