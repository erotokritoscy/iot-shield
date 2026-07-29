"""The trained meter model: a cached fit, as pure versioned JSON (ADR-0030 D6).

Like team3's artifact (ADR-0012 D2) this is JSON, not a pickle — human-readable,
git-diffable, and free of sklearn version fragility. Unlike team3's, what it stores is
**the clean history that was fitted on**, not the fitted weights, and loading re-fits.

Why store the data instead of the model
---------------------------------------
An Isolation Forest is 200 trees of opaque arrays. Serialized as JSON it is megabytes of
unreviewable numbers; serialized as a pickle it is a binary blob whose validity is tied to
one sklearn version. Neither is a thing a reviewer can read, and "human-readable and
git-diffable" was the whole point of the format.

The training matrix is smaller than the model it produces (a demo meter is a few thousand
floats), it is exactly what a reviewer would want to see, and re-fitting from it is
deterministic given ``random_state``. Critically, the **threshold is re-derived on load**
from those same rows and the params' ``fp_budget``, so model and threshold are recomputed
together: a future sklearn that builds slightly different trees produces a matching
threshold rather than silently mismatched ones. Nothing to migrate, nothing to pin.

The cost is a fit at load — ~120 ms per meter, measured, and ``load_artifact`` is cached
per *process*, so a 1,656-meter fleet is minutes of CPU on every cold start (each API
worker, each container restart) — and an artifact that carries data. That second point is
a real constraint, not a footnote: a
committed artifact is only appropriate for data we may commit. Our demo meter is
synthetic, so it is; a customer's meter would be fitted at deployment instead, which is
the deployment shape for metering anyway (see the module docstring in ``detector.py``).

Per-meter, by construction
--------------------------
A meter's model is fitted to *that meter* — upstream fits one model per meter because
meters range from 25% to 94% idle and a pooled model is dominated by the idle majority. So
an artifact holds a **list** of fitted meters, and the detector scores an asset only if it
has that asset's model. One artifact per fleet, not one per product.
"""

from __future__ import annotations

import importlib.resources
import json
import math
from functools import cache, cached_property
from typing import Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from iotshield.detect._artifacts import packaged_artifact_ids
from iotshield.detect.meter._attribution import robust_stats
from iotshield.detect.meter._iforest import fit_forest, score, threshold_at_budget
from iotshield.detect.meter.params import MeterParams

_ARTIFACTS_PACKAGE = "iotshield.detect.meter.artifacts"
_SCHEMA_VERSION = 1


class _ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MeterState(_ArtifactModel):
    """One meter's fit: the schema it learned, its profile, and its clean history.

    ``profile`` and ``training_rows`` are both *learned* — the hour-of-week table is
    computed from the clean stretch, and the rows are the feature matrix that stretch
    produced. Neither is a human choice, which is why they live here and not in
    ``MeterParams``.
    """

    asset_id: str = Field(min_length=1, description="The meter this model was fitted to")
    provenance: str = Field(min_length=1, description="What data this was fitted on")
    feature_columns: list[str] = Field(min_length=1, description="Model column order")
    profile: list[list[float | None]] = Field(
        min_length=1,
        description="24x7 hour-of-week median consumption; null = a bucket never observed",
    )
    training_rows: list[list[float]] = Field(
        min_length=1, description="The known-clean feature matrix this fit learned from"
    )

    @model_validator(mode="after")
    def _shapes_agree(self) -> Self:
        # Validated through numpy rather than nested Python loops: a fleet artifact holds
        # millions of values, and `np.isfinite(...).all()` is one pass instead of one call
        # per value. A ragged matrix makes np.asarray raise or produce an object array,
        # which the ndim/shape check catches.
        try:
            matrix = np.asarray(self.training_rows, dtype=float)
        except ValueError as exc:
            raise ValueError(
                f"training_rows must be rectangular with {len(self.feature_columns)} value(s) "
                f"per row to match feature_columns: {exc}"
            ) from exc
        width = len(self.feature_columns)
        if matrix.ndim != 2 or matrix.shape[1] != width:
            raise ValueError(
                f"training_rows must all have {width} value(s) to match feature_columns; "
                f"got shape {matrix.shape}"
            )
        if not np.isfinite(matrix).all():
            # A non-finite training value would poison the median/MAD baseline and the
            # forest alike, and it can only come from a hand-edited artifact.
            raise ValueError("training_rows must be finite; found nan or inf")
        if len(self.profile) != 24 or any(len(row) != 7 for row in self.profile):
            raise ValueError(
                f"profile must be 24 rows of 7 (hour x day-of-week); got {len(self.profile)} row(s)"
            )
        return self

    @staticmethod
    def profile_rows(profile: np.ndarray) -> list[list[float | None]]:
        """A profile array as JSON rows, ``NaN`` (a never-observed bucket) becoming ``null``.

        The inverse of ``profile_array``, kept beside it: the two are a codec and drift
        immediately if they live in different modules.
        """
        return [
            [None if not np.isfinite(value) else float(value) for value in row] for row in profile
        ]

    @cached_property
    def profile_array(self) -> np.ndarray:
        """The profile as a ``(24, 7)`` float array, ``null`` restored to ``NaN``."""
        return np.array(
            [[math.nan if value is None else value for value in row] for row in self.profile],
            dtype=float,
        )

    @cached_property
    def matrix(self) -> np.ndarray:
        """The training matrix as floats. Cached — every access rebuilt it from the
        Python lists, and a fleet artifact makes that the dominant allocation."""
        return np.asarray(self.training_rows, dtype=float)


class FittedMeter:
    """One meter's usable model: the forest, its threshold, and its attribution baseline.

    Built either by re-fitting a ``MeterState`` off disk or directly by ``fit()``, so the
    two paths cannot diverge in what "fitted" means. Not a pydantic model — it holds a
    live estimator, and it is never serialized (the ``MeterState`` beside it is).
    """

    def __init__(
        self,
        *,
        asset_id: str,
        feature_columns: list[str],
        profile: np.ndarray,
        matrix: np.ndarray,
        params: MeterParams,
        provenance: str,
    ) -> None:
        """Fitting **is** construction: the forest, threshold and baseline are derived here.

        The single definition of "fit one meter" — ``fit()`` and the artifact loader both
        come through it, so a model off disk cannot mean something different from a freshly
        fitted one. Taking the derived values as constructor arguments instead would let a
        caller supply a threshold that does not belong to the forest beside it.
        """
        self.asset_id = asset_id
        self.feature_columns = feature_columns
        self.profile = profile
        self.provenance = provenance
        # Retained because sklearn keeps no training data and this artifact format persists
        # the data rather than the weights: it is what makes fit -> persist -> load
        # reproduce the same model (see the module docstring).
        self.training_rows = np.asarray(matrix, dtype=float)
        self.forest = fit_forest(
            self.training_rows,
            n_estimators=params.n_estimators,
            max_samples=params.max_samples,
            random_state=params.random_state,
        )
        self.threshold = threshold_at_budget(
            score(self.forest, self.training_rows), params.fp_budget
        )
        self.median, self.scale = robust_stats(self.training_rows)


class MeterArtifact(_ArtifactModel):
    """A cached fit: the hyperparameters used, and the per-meter states they produced."""

    schema_version: int = _SCHEMA_VERSION
    params: MeterParams
    meters: list[MeterState] = Field(min_length=1)

    @model_validator(mode="after")
    def _asset_ids_unique(self) -> Self:
        seen = [state.asset_id for state in self.meters]
        if len(set(seen)) != len(seen):
            raise ValueError("each meter must appear once; duplicate asset_id(s) in the artifact")
        return self

    @cached_property
    def fitted(self) -> dict[str, FittedMeter]:
        """Re-fit every stored meter. Cached, so the cost is paid once per artifact."""
        return {
            state.asset_id: FittedMeter(
                asset_id=state.asset_id,
                feature_columns=list(state.feature_columns),
                profile=state.profile_array,
                matrix=state.matrix,
                params=self.params,
                provenance=state.provenance,
            )
            for state in self.meters
        }


def parse_artifact(text: str, label: str = "artifact") -> MeterArtifact:
    """Validate artifact JSON, version-checking *before* the shape.

    Order matters for the same reason it does in team3's loader: an older layout would
    otherwise fail on ``extra="forbid"`` with a list of unexpected keys, burying the one
    actionable fact — that the file needs regenerating.
    """
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        version = raw.get("schema_version") if isinstance(raw, dict) else None
        raise ValueError(
            f"unsupported meter artifact schema_version {version!r} in {label!r} "
            f"(expected {_SCHEMA_VERSION}); regenerate it with"
            " `python -m iotshield.detect.meter.train`"
        )
    return MeterArtifact.model_validate(raw)


@cache
def load_artifact(artifact_id: str) -> MeterArtifact:
    """Load ``artifacts/<artifact_id>.json`` from this package.

    Raises ``ValueError`` (not FileNotFoundError) for an unknown id, so the registry
    surfaces it as a configuration error like every other adapter.
    """
    resource = importlib.resources.files(_ARTIFACTS_PACKAGE).joinpath(f"{artifact_id}.json")
    if not resource.is_file():
        raise ValueError(f"unknown meter artifact id: {artifact_id!r}")
    return parse_artifact(resource.read_text(encoding="utf-8"), artifact_id)


def available_artifacts() -> tuple[str, ...]:
    """Every id ``load_artifact`` will accept — the registry publishes these as choices.

    One meter model is one *meter*, not one fleet (ADR-0030 D7), so this list is short by
    design and says so: the shipped ``demo_meter`` covers a single synthetic meter, which a
    one-item dropdown communicates and an empty text box does not.
    """
    return packaged_artifact_ids(_ARTIFACTS_PACKAGE)
