"""Engineered rolling/lag/FFT features — vendored from team3.

``fft_window_energy`` and ``normalize_lag_steps`` are copied verbatim from
``reference/team3-telemetry-ad/src/telemetry_ad/preprocessing/features.py``.

``build_multivariate_features`` is copied from the same file with **one deliberate
adaptation** (ADR-0012, rewrite-the-plumbing): team3 ended with
``reset_index(drop=True)``, which discards the row index. We keep the index so a
surviving feature row still maps to its original timestamp — without that, a
detection could not be given a real ``window_start``/``window_end``. The dropna
warmup (the first ``max(window - 1, max_lag)`` rows carry NaNs and are dropped) is
unchanged, so the caller learns which timestamps survived by reading the returned
frame's index.

Train and inference call this same builder, so the classic feature vector is
consistent by construction — closing team3's positional-mismatch landmine at the
source rather than relying on a saved scaler.
"""

from collections.abc import Sequence

import numpy as np
import pandas as pd


def fft_window_energy(values: np.ndarray, include_dc: bool = False) -> float:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return 0.0

    spectrum_input = arr if include_dc else arr - np.mean(arr)
    spectrum = np.fft.rfft(spectrum_input)
    if not include_dc and spectrum.size > 0:
        spectrum = spectrum[1:]
    if spectrum.size == 0:
        return 0.0

    power = np.abs(spectrum) ** 2
    return float(np.mean(power))


def normalize_lag_steps(lag_steps: Sequence[int] | None) -> list[int]:
    if lag_steps is None:
        return []
    out: list[int] = []
    for lag in lag_steps:
        lag_int = int(lag)
        if lag_int <= 0:
            continue
        if lag_int not in out:
            out.append(lag_int)
    return sorted(out)


def build_multivariate_features(
    df: pd.DataFrame,
    window: int = 60,
    lag_steps: Sequence[int] | None = None,
    fft_enabled: bool = False,
    fft_include_dc: bool = False,
) -> pd.DataFrame:
    """Per numeric column: rolling mean/std, delta, lags, and FFT energy.

    Column order of the output follows the column order of ``df`` (the caller
    passes channels already in the artifact's order). The row index is preserved
    (see module docstring); rows with any NaN/inf feature are dropped as warmup.
    """
    numeric = df.select_dtypes(include=["number"]).copy()
    lags = normalize_lag_steps(lag_steps)
    parts = []
    for col in numeric.columns:
        s = numeric[col]
        part = pd.DataFrame(
            {
                f"{col}_mean": s.rolling(window).mean(),
                f"{col}_std": s.rolling(window).std(),
                f"{col}_delta": s.diff(),
            }
        )
        for lag in lags:
            part[f"{col}_lag_{lag}"] = s.shift(lag)
        if fft_enabled:
            part[f"{col}_fft_energy"] = s.rolling(window).apply(
                lambda values: fft_window_energy(values, include_dc=fft_include_dc),
                raw=True,
            )
        parts.append(part)
    feat = pd.concat(parts, axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    return feat
