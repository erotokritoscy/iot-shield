"""The trained z-score artifact: a cached fit, as pure versioned JSON.

Detection is inference-only; training happens offline (``train.py``) and its output is
committed as JSON and loaded here by id (ADR-0012 D2). Pure JSON — not a pickle — so it is
human-readable, git-diffable, and free of sklearn version-fragility. Loaded through
``importlib.resources`` so the path is package-relative, not CWD-relative.

**The file's shape is the ADR-0022 split, made visible.** ``params`` is what a human chose;
``state`` is what fitting computed, including the schema it learned from the data. You can
read the file and tell the two apart, which is the whole point — schema v1 interleaved them
and ``fit()`` consequently inherited stale hyperparameters from whatever file was loaded.

An artifact is **atomic**: its ``state`` is aligned to the features its ``params``
produced, so the two travel together and a config may not override one while keeping the
other (``detect/_fitted.py:resolve_params``).
"""

from __future__ import annotations

import importlib.resources
import json
from functools import cache
from typing import Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from iotshield.detect.team3.params import Team3Params

_ARTIFACTS_PACKAGE = "iotshield.detect.team3.artifacts"
_SCHEMA_VERSION = 2  # bump when the artifact layout changes; load_artifact rejects mismatches


class _ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Team3State(_ArtifactModel):
    """Everything ``fit()`` computes — and nothing a human chose.

    Two kinds of learned thing, both genuinely learned from the training data:

    - the **schema**: ``channels`` (the raw channel order used to pivot events into a
      matrix and to validate an asset's coverage) and ``feature_columns`` (the engineered
      order ``median``/``mad`` are aligned to);
    - the **parameters**: the robust baseline, and ``threshold``, the frozen decision
      boundary at the configured percentile.
    """

    provenance: str = Field(min_length=1, description="What data this was fitted on")
    channels: list[str] = Field(min_length=1, description="Raw channel order (pivot/validation)")
    feature_columns: list[str] = Field(min_length=1, description="Engineered order (median/mad)")
    median: list[float] = Field(min_length=1)
    mad: list[float] = Field(min_length=1)
    threshold: float

    @model_validator(mode="after")
    def _lengths_agree(self) -> Self:
        n = len(self.feature_columns)
        if not (len(self.median) == len(self.mad) == n):
            raise ValueError(
                "median/mad length must match feature_columns"
                f" (feature_columns={n}, median={len(self.median)}, mad={len(self.mad)})"
            )
        return self

    @property
    def baseline(self) -> dict[str, np.ndarray]:
        """The robust baseline as numpy arrays, for ``_zscore.score_robust_z``."""
        return {
            "median": np.asarray(self.median, dtype=float),
            "mad": np.asarray(self.mad, dtype=float),
        }


class Team3Artifact(_ArtifactModel):
    """A cached fit: the hyperparameters used, and the state they produced."""

    schema_version: int = _SCHEMA_VERSION
    params: Team3Params
    state: Team3State


def parse_artifact(text: str, label: str = "artifact") -> Team3Artifact:
    """Validate artifact JSON, version-checking *before* the shape.

    Order matters: an older layout would otherwise fail on ``extra="forbid"`` with a list
    of ten unexpected keys, burying the one actionable fact — that the file predates the
    params/state split (ADR-0022) and must be regenerated.
    """
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        version = raw.get("schema_version") if isinstance(raw, dict) else None
        raise ValueError(
            f"unsupported team3 artifact schema_version {version!r} in {label!r} "
            f"(expected {_SCHEMA_VERSION}); regenerate it with"
            " `python -m iotshield.detect.team3.train`"
        )
    return Team3Artifact.model_validate(raw)


@cache
def load_artifact(artifact_id: str) -> Team3Artifact:
    """Load ``artifacts/<artifact_id>.json`` from this package.

    Raises ``ValueError`` (not FileNotFoundError) for an unknown id or an unsupported
    schema version, so the registry surfaces either as a configuration error, consistent
    with every other adapter.

    Cached: artifacts ship inside the package and are read-only at runtime, and a
    benchmark builds its detectors once per case — so an uncached load re-read and
    re-validated the same 4.5 KB of JSON sixteen times per run. Safe to share because
    every field is read-only to the detector (``Team3Params`` is frozen outright).
    """
    resource = importlib.resources.files(_ARTIFACTS_PACKAGE).joinpath(f"{artifact_id}.json")
    if not resource.is_file():
        raise ValueError(f"unknown team3 artifact id: {artifact_id!r}")
    return parse_artifact(resource.read_text(encoding="utf-8"), artifact_id)
