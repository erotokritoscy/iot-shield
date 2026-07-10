"""Robust median/MAD z-score — the anomaly model, vendored from team3.

Copied verbatim (numpy-only) from
``reference/team3-telemetry-ad/src/telemetry_ad/models/zscore.py``. This is the
reusable domain asset; the orchestration around it is rewritten against our
contract (ADR-0012). The MAD floor (1e-8) keeps the score finite when a feature is
constant, which is what lets every emitted score satisfy the contract's
finite-float rule.

``fit_robust_baseline`` learns per-feature median and MAD; ``score_robust_z``
returns one score per row (the max standardized deviation across features), so a
single loud channel is enough to flag a row.
"""

import numpy as np


def fit_robust_baseline(x: np.ndarray) -> dict[str, np.ndarray]:
    arr = np.asarray(x, dtype=float)
    med = np.median(arr, axis=0)
    mad = np.median(np.abs(arr - med), axis=0)
    mad = np.maximum(mad, 1e-8)
    return {"median": med, "mad": mad}


def featurewise_robust_z(x: np.ndarray, params: dict[str, np.ndarray]) -> np.ndarray:
    arr = np.asarray(x, dtype=float)
    return 0.6745 * np.abs((arr - params["median"]) / params["mad"])


def score_robust_z(x: np.ndarray, params: dict[str, np.ndarray]) -> np.ndarray:
    z = featurewise_robust_z(x, params)
    if z.ndim == 1:
        return z
    return z.max(axis=1)
