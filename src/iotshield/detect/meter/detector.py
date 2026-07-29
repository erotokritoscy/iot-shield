"""Meter anomaly detector: Isolation Forest over hour-of-week features (ADR-0030).

The Detector port adapter. It does the work the upstream project's orchestration did
around the model, rewritten to our contract:

- filters to its lane and groups events by asset (the port's laws, shared);
- rebuilds each meter's **consumption series** from the event stream and re-grids it to
  the hourly clock, so a 168-hour window means a week of wall clock;
- builds features against the **profile learned during fitting**, never one rebuilt from
  the data under judgment — otherwise "normal" is defined partly by the anomalies it is
  meant to catch;
- scores each hour with that meter's own forest, thresholds it **absolutely**, and merges
  consecutive anomalous hours into episodes — one ``DetectionResult`` per run, carrying
  feature attribution as its ``evidence``.

One model per meter, and what that means for deployment
------------------------------------------------------
Upstream fits one Isolation Forest per meter, because meters range from 25% to 94% idle
and a pooled model is dominated by the idle majority. We keep that: fitted state is a map
from ``asset_id`` to that meter's model, and an asset with no model is **skipped, never
scored** — the same discipline as team3's channel-coverage guard, for the same reason
(scoring against a foreign baseline measures domain shift, not anomalies).

The consequence is worth stating plainly, because it differs from every other detector
here: there is no universal shippable model for metering. A fleet of 1,656 SensiPro meters
needs 1,656 fits, produced by fitting on each meter's own clean history at deployment (and
periodically after). The committed artifact exists so the demo and the tests have a fitted
model to run; it is not a product-wide default and does not pretend to be.

Contrast with ``fit`` elsewhere: team3's ``fit`` **refuses** more than one asset, because
its rolling features assume one contiguous history. Ours accepts a fleet and fits each
meter separately — the same underlying rule (never interleave two assets), reached from
the other side.

Provenance: the features, profile and attribution are vendored from Panayiotis Cyprus's
*ML-Based Anomaly and Attack Detection for Smart Meter Telemetry* (commit ``d3c61a6``) —
see ``_features.py``, ``_profile.py``, ``_attribution.py``. The fit/score split and the
absolute threshold are ours (``_iforest.py``), because his single function fits and scores
the same window.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from iotshield.capabilities import DETECTIONS, Capability, LaneToken
from iotshield.contracts import DetectionResult, Lane, NormalizedEvent
from iotshield.detect._fitted import NotFitted
from iotshield.detect._group import group_by_asset
from iotshield.detect._runs import classify_run, merge_runs
from iotshield.detect.meter._attribution import attribute_row, summarise
from iotshield.detect.meter._features import build_features, feature_matrix, model_columns
from iotshield.detect.meter._iforest import score
from iotshield.detect.meter._profile import as_datetime_index, build_profile
from iotshield.detect.meter.artifact import FittedMeter, MeterState
from iotshield.detect.meter.params import METHOD, MeterParams
from iotshield.meter import METADATA_KEY, reading_of

CAPABILITY = Capability.of(consumes={LaneToken(Lane.TELEMETRY)}, produces={DETECTIONS})

_TOP_FEATURES = 2


def _at(index: pd.DatetimeIndex, position: int) -> datetime:
    """One timestamp as a plain tz-aware ``datetime``.

    Indexing then converting, rather than converting the whole index: the callers want two
    or three stamps out of a year of hourly readings, and materializing every one of them
    to read the ends was the single largest piece of pure waste on the detect path.

    The ``type: ignore`` is for the installed pandas stubs, which model integer indexing
    into a ``DatetimeIndex`` as returning the index rather than a ``Timestamp``.
    """
    return index[position].to_pydatetime()  # type: ignore[attr-defined]


def _is_meter_reading(event: NormalizedEvent) -> bool:
    """Only a consumption event carrying a meter payload can be judged.

    Both halves matter: ``value`` is the consumption, and the metadata record is what says
    this is a meter at all (rather than some other telemetry that happens to be numeric).
    """
    return event.value is not None and reading_of(event.metadata) is not None


def consumption_series(events: Sequence[NormalizedEvent]) -> tuple[pd.Series, float]:
    """One meter's events -> (consumption indexed by timestamp, interval in hours).

    The inverse of the normalizer's per-interval emission. Duplicated timestamps keep the
    first reading, so a re-ingested file cannot make one hour count twice.
    """
    # One pass. The port guarantees events arrive sorted by (asset_id, timestamp) and
    # group_by_asset sorts again, so a repeated timestamp can only be the previous one —
    # which makes the first-wins dedup a comparison rather than a dict plus a re-sort.
    stamps: list[datetime] = []
    values: list[float] = []
    interval = 1.0
    for event in events:
        if event.value is None or event.timestamp in stamps[-1:]:
            continue
        payload = event.metadata.get(METADATA_KEY)
        if not isinstance(payload, dict):
            continue
        stamps.append(event.timestamp)
        values.append(float(event.value))
        if not values[1:]:
            # The clock is a property of the source, so the first reading settles it; a
            # later disagreement is a cadence problem the normalizer already reports.
            width = payload.get("interval_hours", 1.0)
            interval = float(width) if isinstance(width, int | float) else 1.0
    series = pd.Series(values, index=pd.DatetimeIndex(stamps, name="timestamp"), dtype=float)
    return series, interval


class MeterIForestDetector:
    def __init__(self, params: MeterParams, fitted: dict[str, FittedMeter] | None = None) -> None:
        self._params = params
        self._fitted = fitted

    @property
    def is_fitted(self) -> bool:
        return self._fitted is not None

    @property
    def lane(self) -> Lane:
        return Lane.TELEMETRY

    @property
    def method(self) -> str:
        return METHOD

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        if self._fitted is None:
            raise NotFitted(
                f"{METHOD} was built from hyperparameters with no fitted meters; name an "
                "'artifact' in its params, or fit it first (--fit-protocol prefix)"
            )
        detections: list[DetectionResult] = []
        for asset_id, asset_events in group_by_asset(
            events, lane=self.lane, keep=_is_meter_reading
        ):
            model = self._fitted.get(asset_id)
            if model is None:
                # No model for this meter: skip rather than score it against another
                # meter's normal, which would measure the difference between two houses.
                continue
            detections.extend(self._detect_asset(asset_id, asset_events, model))
        return detections

    def why_silent(self, events: Sequence[NormalizedEvent]) -> str | None:
        """The optional ``Diagnosable`` capability: which meters have no model to judge them.

        One model per meter is the design (ADR-0030 D7), so a meter absent from the artifact
        is skipped — correctly, since scoring it against another meter's normal would measure
        the difference between two households. But the run reports only "no detections",
        which reads as "these meters are fine" (ADR-0033).
        """
        fitted = self._fitted
        if fitted is None:
            # Untrained entirely: detect() raises NotFitted and compute_run refuses the run
            # before this is ever asked, so there is no silence to explain here.
            return None
        unmodelled = sorted(
            {
                asset_id
                for asset_id, _ in group_by_asset(events, lane=self.lane, keep=_is_meter_reading)
                if asset_id not in fitted
            }
        )
        if not unmodelled:
            return None
        return (
            f"{METHOD} has no fitted model for {', '.join(unmodelled)}: those meters were "
            f"skipped, not scored - one model per meter means each needs its own clean history"
        )

    def fit(self, events: Sequence[NormalizedEvent]) -> MeterIForestDetector:
        """Fit one model per meter from ``events`` (assumed clean); return a new detector.

        The optional ``Fittable`` capability. Never called by the pipeline — only by a
        benchmark, so it can learn each meter's normal from that meter's own clean history
        instead of measuring domain shift.

        Every hyperparameter comes from ``self._params`` — this instance's *construction* —
        and never from a previously loaded artifact (ADR-0022 rule 2). What is learned is
        the hour-of-week profile, the forest, the absolute threshold and the attribution
        baseline, per meter.
        """
        # The same routing detect() uses: foreign-lane isolation, deterministic asset order
        # and — the part a hand-rolled grouping here was missing — the per-asset time sort
        # that every rolling feature depends on.
        groups = list(group_by_asset(events, lane=self.lane, keep=_is_meter_reading))
        if not groups:
            raise ValueError(
                "fit needs telemetry meter events carrying a consumption value and a "
                "meter payload; got none"
            )

        fitted: dict[str, FittedMeter] = {}
        too_short: list[str] = []
        for asset_id, asset_events in groups:
            model = self._fit_one(asset_id, asset_events)
            if model is None:
                too_short.append(asset_id)
                continue
            fitted[asset_id] = model
        if not fitted:
            raise ValueError(
                "no meter had enough clean history to fit "
                f"(need {self._params.min_train_hours}h each); short: {', '.join(too_short)}"
            )
        return MeterIForestDetector(self._params, fitted)

    def _fit_one(self, asset_id: str, events: list[NormalizedEvent]) -> FittedMeter | None:
        """Fit one meter, or ``None`` when its clean history is too short to learn from."""
        series, interval = consumption_series(events)
        if len(series) < self._params.min_train_hours:
            return None
        profile = build_profile(series)
        features = build_features(
            series,
            profile,
            window=self._params.profile_window_hours,
            interval_hours=interval,
        )
        columns = model_columns(self._params.profile_window_hours)
        rows, _stamps_kept = feature_matrix(features, columns)
        if not rows.size:
            return None
        span = as_datetime_index(series.index)
        return FittedMeter(
            asset_id=asset_id,
            feature_columns=list(columns),
            profile=profile,
            matrix=rows,
            params=self._params,
            provenance=(
                f"fitted on {len(rows)} clean hour(s) of meter {asset_id} "
                f"({_at(span, 0).isoformat()} -> {_at(span, -1).isoformat()})"
            ),
        )

    def state_for(self, asset_id: str) -> MeterState:
        """The persistable state of one fitted meter (used by ``train.py``)."""
        model = self.model_for(asset_id)
        return MeterState(
            asset_id=model.asset_id,
            provenance=model.provenance,
            feature_columns=list(model.feature_columns),
            profile=MeterState.profile_rows(model.profile),
            # The forest is not stored; these rows are what reproduce it (see artifact.py).
            training_rows=model.training_rows.tolist(),
        )

    @property
    def fitted_assets(self) -> tuple[str, ...]:
        return tuple(sorted(self._fitted)) if self._fitted else ()

    @property
    def params(self) -> MeterParams:
        """The hyperparameters this instance was constructed with (ADR-0022)."""
        return self._params

    def model_for(self, asset_id: str) -> FittedMeter:
        """One meter's fitted model — its profile, forest, threshold and baseline.

        A read accessor rather than a private reach-in, because "what did this detector
        actually learn about that meter" is a fair question from a test, a notebook, or a
        future explainability view.
        """
        if self._fitted is None or asset_id not in self._fitted:
            raise NotFitted(f"{METHOD} has no fitted model for meter {asset_id!r}")
        return self._fitted[asset_id]

    def _detect_asset(
        self, asset_id: str, events: list[NormalizedEvent], model: FittedMeter
    ) -> list[DetectionResult]:
        series, interval = consumption_series(events)
        if series.empty:
            return []
        # The profile is the FITTED one: "normal" was learned on clean history and is now
        # applied to data under judgment, not re-derived from it.
        features = build_features(
            series,
            model.profile,
            window=self._params.profile_window_hours,
            interval_hours=interval,
        )
        try:
            rows, stamps = feature_matrix(features, model.feature_columns)
        except KeyError:
            # The fit's column order cannot be produced from this input (a different
            # window). Skip rather than score a misaligned matrix.
            return []
        if not rows.size:
            return []

        scores = score(model.forest, rows)
        anomaly = np.isfinite(scores) & (scores > model.threshold)
        if not anomaly.any():
            return []

        detections: list[DetectionResult] = []
        for start, end in merge_runs(anomaly):
            length = end - start + 1
            if length < self._params.min_episode_hours:
                continue
            peak = start + int(np.argmax(scores[start : end + 1]))
            detections.append(
                DetectionResult(
                    asset_id=asset_id,
                    # The interval-end convention travels with the events, so a window
                    # runs from the first flagged hour's stamp to the last one's.
                    window_start=_at(stamps, start),
                    window_end=_at(stamps, end),
                    lane=self.lane,
                    method=self.method,
                    score=float(scores[peak]),
                    threshold=float(model.threshold),
                    event_kind=classify_run(length),
                    evidence=self._evidence(
                        model, rows[peak], _at(stamps, peak), scores[peak], length
                    ),
                )
            )
        return detections

    def _evidence(
        self,
        model: FittedMeter,
        peak_row: np.ndarray,
        peak_stamp: datetime,
        peak_score: float,
        length: int,
    ) -> dict[str, Any]:
        top = attribute_row(
            peak_row,
            model.feature_columns,
            model.median,
            model.scale,
            top_k=_TOP_FEATURES,
        )
        return {
            "peak_timestamp": peak_stamp.isoformat(),
            "peak_score": float(peak_score),
            "num_anomalous_points": length,
            # The analyst-readable headline; the features below are the supporting detail.
            "explanation": summarise(top),
            "top_features": top,
        }
