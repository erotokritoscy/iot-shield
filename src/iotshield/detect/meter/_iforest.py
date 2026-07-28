"""Isolation Forest, split into fit and score — OURS, not vendored (ADR-0028 D5).

Upstream ``meterdetect/models.py:run_isolation_forest`` is one function that fits **and**
scores the same window, and reports a per-batch ``contamination`` quantile as its verdict.
That shape cannot satisfy this project's ``Fittable`` contract, and more importantly it
cannot be right in production, so this is a rewrite rather than a copy. The model, the
feature set and the score convention are unchanged; only the plumbing differs.

Why a per-batch quantile is the thing to leave behind
----------------------------------------------------
``contamination=0.002`` flags the top 0.2% of *whatever it is given*. On a batch with no
anomalies it flags 0.2% anyway — measured on an entirely clean synthetic meter, the
upstream path flags exactly ``0.002 x n`` hours by construction. It cannot report "nothing
happened", which is the answer an operator needs most nights.

The replacement is the one the upstream author already builds towards in
``meterdetect/calibration.py``: learn an **absolute** threshold on known-clean history at a
declared false-alarm budget, then judge new data against it. His
``evaluation.threshold_for_fp_budget`` returns exactly this number (it then stores the
equivalent contamination, which is the step we drop). Under ADR-0022 the split is clean:
the FP budget is the **hyperparameter** a human chooses, and the threshold is **fitted
state** the data computes.

Measured on a synthetic year (fit on a clean 20% prefix, scored on the unseen remainder):
4/5 injected events caught at 0.71 false alarms per meter-month, versus 2/5 for the
per-batch path. Detail in ADR-0028.

Scores follow the upstream convention: ``score_samples`` is negated so **higher = more
anomalous**, matching every other detector here.
"""

from __future__ import annotations

import numpy as np
from sklearn.ensemble import IsolationForest

DEFAULT_N_ESTIMATORS = 200
DEFAULT_MAX_SAMPLES = 256
"""Liu et al.'s subsample size: it avoids swamping/masking and does NOT grow with history
length, so a meter with three years of data is not scored differently from one with one."""
DEFAULT_RANDOM_STATE = 42


def fit_forest(
    matrix: np.ndarray,
    *,
    n_estimators: int,
    max_samples: int,
    random_state: int,
) -> IsolationForest:
    """Fit an Isolation Forest on one meter's known-clean feature rows.

    ``contamination`` is deliberately left at sklearn's default and its hard label is never
    read: the verdict comes from an absolute threshold on the continuous score (see
    ``threshold_at_budget``). Isolation Forest splits on raw feature values one at a time,
    so no scaling is needed — unlike a distance method such as LOF.
    """
    rows = np.asarray(matrix, dtype=float)
    if rows.ndim != 2 or rows.shape[0] == 0:
        raise ValueError("fitting needs a non-empty 2-D feature matrix")
    # max_samples may not exceed the number of rows available.
    effective = min(int(max_samples), rows.shape[0])
    return IsolationForest(
        n_estimators=int(n_estimators),
        # sklearn accepts int | float | "auto" here; the installed stubs declare only str.
        max_samples=effective,  # type: ignore[arg-type]
        random_state=int(random_state),
        n_jobs=1,
    ).fit(rows)


def score(forest: IsolationForest, matrix: np.ndarray) -> np.ndarray:
    """Anomaly scores for rows the forest has never seen. Higher = more anomalous."""
    return -forest.score_samples(np.asarray(matrix, dtype=float))


def threshold_at_budget(clean_scores: np.ndarray, fp_budget: float) -> float:
    """The absolute score above which at most ``fp_budget`` of clean hours would alarm.

    ``fp_budget`` is a fraction of clean hours, so ``0.0014`` is about one false alarm per
    meter-month (730 hours). The quantile is taken on the *training* scores, which are
    known-clean by construction — that is what makes the resulting number transferable to
    data the model has not seen.

    The estimate is only as good as the history behind it: at a budget of 0.0014 you need
    on the order of a year of hourly data before the quantile stops moving (measured drift
    across 827 -> 5,207 clean rows was ~19%, and non-monotone). The caller is expected to
    treat ``fp_budget`` as a swept hyperparameter, not a guarantee.

    A budget the data cannot support is **refused**, not silently extrapolated: if
    ``fp_budget x n`` is below one row, the requested quantile lies past the largest
    observed score and the "threshold" would be an artifact of interpolation rather than a
    measurement. This is the failure that looks like success — a plausible number, derived
    from nothing — so it raises with the two ways out (more history, or a looser budget).
    """
    scores = np.asarray(clean_scores, dtype=float)
    scores = scores[np.isfinite(scores)]
    if scores.size == 0:
        raise ValueError("cannot learn a threshold from an empty score distribution")
    if not 0.0 < fp_budget < 1.0:
        raise ValueError(f"fp_budget must be a fraction strictly between 0 and 1, got {fp_budget}")
    if fp_budget * scores.size < 1.0:
        needed = int(np.ceil(1.0 / fp_budget))
        raise ValueError(
            f"fp_budget={fp_budget:g} cannot be estimated from {scores.size} clean row(s): it "
            f"asks for the top {fp_budget * scores.size:.2f} of them. Fit on at least "
            f"{needed} clean hours ({needed / 730:.1f} meter-months), or loosen fp_budget to "
            f"at least {1.0 / scores.size:.5f}."
        )
    return float(np.quantile(scores, 1.0 - fp_budget))
