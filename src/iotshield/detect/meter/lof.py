"""Local Outlier Factor on the same features as the forest (ADR-0048 D5).

The forest asks how easily a row is isolated; LOF asks how sparse its neighbourhood is compared
with its neighbours'. Upstream ran both for that reason, then measured LOF as the weakest single
detector on every fleet, and on LEAD's real labels its synthetic-harness competence of about 1.0
fell to about 0.3. So this is an option to benchmark rather than a recommendation.

Two things differ from upstream's ``run_local_outlier_factor``, and both follow from the
fit-then-judge shape ADR-0030 D5 gave the forest:

- **``novelty=True``.** Upstream calls ``fit_predict`` on the batch under judgment, so "normal"
  included the anomalies, and sklearn's plain LOF cannot score a row it has not seen at all.
- **The threshold is read from ``negative_outlier_factor_``, not ``score_samples(training)``.**
  In novelty mode a training row passed back in counts itself as its own nearest neighbour, which
  biases its score low, so a threshold taken from those scores sits below what unseen clean rows
  score and spends the alarm budget several times over. ``negative_outlier_factor_`` is sklearn's
  leave-self-out score for exactly those rows. See the test that pins the difference.

Upstream's calibrated numbers do not transfer through either change, so they are not quoted
here as this adapter's performance. Provenance: Panayiotis Cyprus's ``meterdetect/models.py``
(see ADR-0048 on why no commit can be cited). The ``RobustScaler`` is his and is needed: LOF is a
distance method, so an unscaled column on a larger scale would decide every neighbourhood.
"""

from __future__ import annotations

import importlib.resources
from functools import cache, cached_property, lru_cache
from pathlib import Path

import numpy as np
from sklearn.neighbors import LocalOutlierFactor
from sklearn.preprocessing import RobustScaler

from iotshield.detect._artifacts import packaged_artifact_ids
from iotshield.detect.meter.artifact import (
    _SHELF_CACHE_SIZE,
    FittedMeter,
    MetersArtifact,
    _version_checked,
    shelved,
)
from iotshield.detect.meter.detector import PerMeterDetector
from iotshield.detect.meter.params import LOF_METHOD, LofParams, PerMeterParams

_ARTIFACTS_PACKAGE = "iotshield.detect.meter.artifacts_lof"


class FittedLofMeter(FittedMeter):
    def _learn(self, params: PerMeterParams) -> np.ndarray:
        if not isinstance(params, LofParams):
            raise TypeError(f"LOF needs LofParams, got {type(params).__name__}")
        self._scaler = RobustScaler().fit(self.training_rows)
        self._lof = LocalOutlierFactor(
            # sklearn caps this itself with a warning; capping here keeps a short meter quiet.
            n_neighbors=min(params.n_neighbors, max(1, self.training_rows.shape[0] - 1)),
            metric=params.metric,
            novelty=True,
        ).fit(self._scaler.transform(self.training_rows))
        return -self._lof.negative_outlier_factor_

    def score(self, rows: np.ndarray) -> np.ndarray:
        scaled = self._scaler.transform(np.asarray(rows, dtype=float))
        return -self._lof.score_samples(scaled)


class MeterLofDetector(PerMeterDetector[LofParams]):
    _method = LOF_METHOD
    _model_type = FittedLofMeter


class LofArtifact(MetersArtifact):
    params: LofParams

    @cached_property
    def fitted(self) -> dict[str, FittedMeter]:
        return self._refit(FittedLofMeter, self.params)


def parse_lof_artifact(text: str, label: str = "artifact") -> LofArtifact:
    return LofArtifact.model_validate(_version_checked(text, label))


@cache
def _load_packaged(artifact_id: str) -> LofArtifact | None:
    resource = importlib.resources.files(_ARTIFACTS_PACKAGE).joinpath(f"{artifact_id}.json")
    if not resource.is_file():
        return None
    return parse_lof_artifact(resource.read_text(encoding="utf-8"), artifact_id)


@lru_cache(maxsize=_SHELF_CACHE_SIZE)
def _load_shelved(path: Path, _modified_ns: int) -> LofArtifact:
    return parse_lof_artifact(path.read_text(encoding="utf-8"), path.stem)


def load_lof_artifact(artifact_id: str) -> LofArtifact:
    """As ``artifact.load_artifact``: packaged first, then the shelf, by membership only."""
    if artifact_id in packaged_artifact_ids(_ARTIFACTS_PACKAGE):
        packaged = _load_packaged(artifact_id)
        if packaged is not None:
            return packaged
    path = shelved(LOF_METHOD).get(artifact_id)
    if path is None:
        known = ", ".join(available_lof_artifacts()) or "none"
        raise ValueError(f"unknown LOF artifact id: {artifact_id!r}; known: {known}")
    return _load_shelved(path, path.stat().st_mtime_ns)


def available_lof_artifacts() -> tuple[str, ...]:
    return tuple(sorted({*packaged_artifact_ids(_ARTIFACTS_PACKAGE), *shelved(LOF_METHOD)}))
