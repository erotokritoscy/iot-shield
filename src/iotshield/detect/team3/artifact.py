"""The trained z-score artifact: a pure-JSON, versioned model parameter bundle.

Detection is inference-only; training happens once, offline (``train.py``), and
its parameters are committed as JSON and loaded here by id (ADR-0012 D2). Pure
JSON — not a pickle — so it is human-readable, git-diffable, and free of sklearn
version-fragility. Loaded through ``importlib.resources`` so the path is
package-relative, not CWD-relative.
"""

from __future__ import annotations

import importlib.resources
from typing import Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

_ARTIFACTS_PACKAGE = "iotshield.detect.team3.artifacts"
_SCHEMA_VERSION = 1  # bump when the artifact layout changes; load_artifact rejects mismatches


class _ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FftConfig(_ArtifactModel):
    enabled: bool = False
    include_dc: bool = False


class Team3Artifact(_ArtifactModel):
    """Everything ``detect()`` needs to reproduce training-time scoring.

    ``channels`` is the raw channel order used to pivot events into a matrix and to
    validate an asset's coverage; ``feature_columns`` is the engineered-feature
    order that ``median``/``mad`` are aligned to. The two ``list`` parameters are
    the robust baseline; ``threshold`` is the frozen decision boundary.
    """

    schema_version: int = _SCHEMA_VERSION
    method: str = Field(min_length=1)
    provenance: str = Field(min_length=1)

    window_size: int = Field(ge=2, description="Rolling-feature window length")
    lag_steps: list[int] = Field(default_factory=list)
    fft: FftConfig = Field(default_factory=FftConfig)

    channels: list[str] = Field(min_length=1, description="Raw channel order (pivot/validation)")
    feature_columns: list[str] = Field(min_length=1, description="Engineered order (median/mad)")

    median: list[float] = Field(min_length=1)
    mad: list[float] = Field(min_length=1)
    threshold: float
    percentile: float = Field(gt=0.0, lt=100.0)

    @model_validator(mode="after")
    def _lengths_agree(self) -> Self:
        n = len(self.feature_columns)
        if not (len(self.median) == len(self.mad) == n):
            raise ValueError(
                "median/mad length must match feature_columns"
                f" (feature_columns={n}, median={len(self.median)}, mad={len(self.mad)})"
            )
        if any(lag >= self.window_size for lag in self.lag_steps):
            raise ValueError("every lag step must be smaller than window_size")
        return self

    @property
    def baseline(self) -> dict[str, np.ndarray]:
        """The robust baseline as numpy arrays, for ``_zscore.score_robust_z``."""
        return {
            "median": np.asarray(self.median, dtype=float),
            "mad": np.asarray(self.mad, dtype=float),
        }


def load_artifact(artifact_id: str) -> Team3Artifact:
    """Load ``artifacts/<artifact_id>.json`` from this package.

    Raises ``ValueError`` (not FileNotFoundError) for an unknown id or an
    unsupported schema version, so the registry surfaces either as a configuration
    error, consistent with every other adapter.
    """
    resource = importlib.resources.files(_ARTIFACTS_PACKAGE).joinpath(f"{artifact_id}.json")
    if not resource.is_file():
        raise ValueError(f"unknown team3 artifact id: {artifact_id!r}")
    artifact = Team3Artifact.model_validate_json(resource.read_text(encoding="utf-8"))
    if artifact.schema_version != _SCHEMA_VERSION:
        raise ValueError(
            f"unsupported team3 artifact schema_version {artifact.schema_version}"
            f" (expected {_SCHEMA_VERSION})"
        )
    return artifact
