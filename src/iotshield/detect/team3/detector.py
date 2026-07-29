"""Team3 telemetry detector: robust z-score over engineered features.

The Detector port adapter (ADR-0012). It does the work team3's orchestration did
around the model, but rewritten to our contract:

- filters to its lane and groups events by asset (the runner routes by lane;
  this stays defensive per the port);
- **pivots** univariate events back into a ``(time x channel)`` matrix (ADR-0003
  D1) — the inverse of the normalizer's fan-out;
- **looks up that asset's own baseline**, and skips an asset it has no model for
  (ADR-0034);
- **validates channel coverage** before scoring: an asset missing any of *its*
  trained channels is skipped, never scored against a misaligned matrix (ADR-0012 D4);
- scores each row with the vendored robust z-score, thresholds it, and **merges
  consecutive anomalous rows into episodes** — one ``DetectionResult`` per run,
  with ``event_kind`` from run length and top-channel ``evidence`` (ADR-0012 D5).

Constructed from two things, never one (ADR-0022): ``Team3Params``, the hyperparameters a
human chose, and an optional **map of ``Team3State`` per asset**, the baselines a previous
fit computed. Without them it is an untrained model — legal to build, legal to ``fit()``,
and refusing to ``detect()``. No dependency beyond numpy/pandas.

**A baseline is about one device (ADR-0034).** Rolling, lag and FFT features read the row
before, so they only mean anything within one asset's contiguous history. ``fit`` therefore
learns one model per asset and ``detect`` judges each asset against its own — the shape
``meter_iforest`` already uses (ADR-0030 D7). Previously ``fit`` refused a fleet outright
while ``detect`` silently applied one device's median/MAD to every other: the same rule
enforced on one side and violated on the other.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from iotshield.capabilities import DETECTIONS, Capability, LaneToken
from iotshield.contracts import DetectionResult, Lane, NormalizedEvent
from iotshield.detect._fitted import NotFitted
from iotshield.detect._group import group_by_asset
from iotshield.detect._runs import classify_run, merge_runs
from iotshield.detect.team3._features import build_multivariate_features
from iotshield.detect.team3._zscore import featurewise_robust_z
from iotshield.detect.team3.artifact import Team3State
from iotshield.detect.team3.params import METHOD, Team3Params
from iotshield.detect.team3.train import train_state

# Fixed telemetry-lane detector; declared for the compatibility guard.
CAPABILITY = Capability.of(consumes={LaneToken(Lane.TELEMETRY)}, produces={DETECTIONS})

_TOP_CHANNELS = 3


def _has_reading(event: NormalizedEvent) -> bool:
    """Only a named channel with a numeric value can enter the (time x channel) matrix."""
    return event.metric is not None and event.value is not None


def wide_frame(events: Sequence[NormalizedEvent]) -> pd.DataFrame:
    """Pivot univariate events back into the ``(time x channel)`` matrix the model wants.

    The inverse of the normalizer's fan-out (ADR-0003 D1). Shared by inference (per asset)
    and by ``fit`` (over the whole training slice), so the two can never disagree about how
    events become a matrix. The index is the tz-aware timestamp; callers narrow the columns.
    """
    records = [
        (event.timestamp, event.metric, float(event.value))
        for event in events
        if event.metric is not None and event.value is not None
    ]
    frame = pd.DataFrame(records, columns=["ts", "metric", "value"])
    return frame.pivot_table(index="ts", columns="metric", values="value", aggfunc="mean")


def _channel_of(feature_name: str, channels: Sequence[str]) -> str:
    # Longest channel prefix the feature name starts with (robust to channel names
    # that themselves contain separators, e.g. "Volume Flow RateRMS").
    matches = [c for c in channels if feature_name.startswith(f"{c}_")]
    return max(matches, key=len) if matches else feature_name


def _uncovered(state: Team3State, present: Collection[str]) -> list[str]:
    """Trained channels this data does not carry — the coverage guard, written once.

    ``detect`` skips an asset when this is non-empty (ADR-0012 D4) and ``why_silent``
    reports the same assets by name. The port requires those two to answer identically
    (``why_silent`` is a pure predicate over the events ``detect`` would see, ADR-0033 D3),
    so they read the rule from here rather than each encoding it — the failure ADR-0034 was
    written about was exactly one rule with two encodings that disagreed.

    ``present`` is a collection of channel names, not a frame, so a caller that already has
    the pivoted matrix passes its columns and one that does not need a matrix passes the
    metrics directly.
    """
    return sorted(set(state.channels) - set(present))


class Team3ZScoreDetector:
    def __init__(self, params: Team3Params, models: dict[str, Team3State] | None = None) -> None:
        self._params = params
        self._models = models

    @property
    def is_fitted(self) -> bool:
        return self._models is not None

    @property
    def fitted_assets(self) -> tuple[str, ...]:
        """Which devices this detector has a baseline for — empty when untrained."""
        return tuple(sorted(self._models)) if self._models else ()

    def model_for(self, asset_id: str) -> Team3State:
        """One asset's fitted baseline — its channels, feature schema and threshold.

        A read accessor rather than a private reach-in, for the same reason
        ``MeterIForestDetector.model_for`` is one: "what did this detector learn about that
        device" is a fair question from a test, a notebook, or a future explainability view.
        """
        if self._models is None or asset_id not in self._models:
            raise NotFitted(f"{METHOD} has no fitted model for asset {asset_id!r}")
        return self._models[asset_id]

    @property
    def lane(self) -> Lane:
        return Lane.TELEMETRY

    @property
    def method(self) -> str:
        return METHOD

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        # Foreign-lane isolation and deterministic asset order are the port's laws, shared
        # by every detector; only the "is this event judgeable" predicate is ours.
        if self._models is None:
            raise NotFitted(
                f"{METHOD} was built from hyperparameters with no trained baseline; name an "
                "'artifact' in its params, or fit it first (--fit-protocol prefix)"
            )
        detections: list[DetectionResult] = []
        for asset_id, asset_events in group_by_asset(events, lane=self.lane, keep=_has_reading):
            model = self._models.get(asset_id)
            if model is None:
                # No baseline for this device: skip rather than judge it against another
                # device's normal, which measures the difference between two machines and
                # calls it an anomaly (ADR-0034). `why_silent` reports it.
                continue
            detections.extend(self._detect_asset(asset_id, asset_events, model))
        return detections

    def fit(self, events: Sequence[NormalizedEvent]) -> Team3ZScoreDetector:
        """Learn one baseline **per asset** from ``events`` (assumed normal) (ADR-0034).

        The optional ``Fittable`` capability (ADR-0021 D3). Never called by the pipeline —
        it exists so an evaluation can train on an asset's own clean history rather than
        measuring domain shift, which ADR-0012 D3 found is what sinks a foreign baseline.

        Every hyperparameter comes from ``self._params`` — this instance's *construction* —
        and never from a previously loaded artifact (ADR-0022 rule 2). What is learned is
        the baseline, the threshold, and the feature schema, including ``channels`` when
        they were left unpinned — learned **per asset**, which is what lets one artifact
        cover a fleet whose devices do not report the same channels.

        Rolling and lag features assume one asset's contiguous history, so the fleet is
        split before anything is computed. Pooling them was not merely imprecise: the pivot
        keys on ``(timestamp, metric)`` with no asset, so shared channels were *averaged*
        into a device that does not exist and unshared ones became a mostly-empty column
        that ``ffill`` turned into a staircase. Refusing that was correct; splitting it is
        better.

        One asset that cannot be fitted does not sink the rest (a short history, or a
        missing pinned channel): it is skipped and named, and only a fleet where *nothing*
        could be fitted raises — a statement about the data rather than about one device.
        """
        groups = list(group_by_asset(events, lane=self.lane, keep=_has_reading))
        if not groups:
            raise ValueError("fit needs telemetry readings with a metric and value; got none")

        models: dict[str, Team3State] = {}
        refused: list[str] = []
        for asset_id, asset_events in groups:
            try:
                models[asset_id] = self._fit_asset(asset_id, asset_events)
            except ValueError as exc:
                refused.append(f"{asset_id} ({exc})")
        if not models:
            raise ValueError(f"no asset could be fitted: {'; '.join(refused)}")
        return Team3ZScoreDetector(self._params, models)

    def _fit_asset(self, asset_id: str, events: list[NormalizedEvent]) -> Team3State:
        """One device's baseline. ``events`` are already this asset's, in time order."""
        wide = wide_frame(events)
        # Pinned channels are a promise the data must keep; unpinned ones are learned from
        # it. pivot_table sorts its columns, so the learned order is deterministic.
        channels = tuple(self._params.channels or wide.columns)
        missing = [channel for channel in channels if channel not in wide.columns]
        if missing:
            raise ValueError(f"fit input is missing pinned channels: {missing}")
        frame = wide.reindex(columns=list(channels)).sort_index().ffill().dropna()
        return train_state(
            frame.reset_index(drop=True),
            self._params,
            asset_id=asset_id,
            channels=channels,
            provenance=f"refit on {len(frame)} normal rows of {asset_id}",
        )

    def why_silent(self, events: Sequence[NormalizedEvent]) -> str | None:
        """The optional ``Diagnosable`` capability: which assets this detector cannot judge.

        Two different silences, reported separately because they call for different actions
        (ADR-0033, ADR-0034). **No model** means this device was never trained on — fit it,
        or accept that it is out of scope. **Uncovered channels** means it was trained on a
        device reporting something this data does not carry — the model is for a different
        rig, or the source changed shape. Both are otherwise indistinguishable from a clean
        recording, which is the one thing a detector must never let them look like.
        """
        models = self._models
        if models is None:
            # Untrained entirely: detect() raises NotFitted and compute_run refuses the run
            # before this is ever asked, so there is no silence to explain here.
            return None
        unmodelled: list[str] = []
        uncovered: list[str] = []
        # Same filter detect() uses, so this asks about exactly the assets it would judge.
        for asset_id, asset_events in group_by_asset(events, lane=self.lane, keep=_has_reading):
            model = models.get(asset_id)
            if model is None:
                unmodelled.append(asset_id)
                continue
            # The metrics present, taken straight off the events rather than by pivoting
            # them: `group_by_asset(keep=_has_reading)` has already guaranteed every event
            # here carries a metric and a value, which is exactly the filter `wide_frame`
            # applies before pivoting, so the two produce the same channel set. A
            # `pivot_table` costs ~2.5 ms per asset however small the frame, and this runs
            # for every asset on both the run and the benchmark path (measured at 9.5% of a
            # SKAB benchmark, 104x cheaper this way) — for a set difference.
            absent = _uncovered(model, {event.metric for event in asset_events if event.metric})
            if absent:
                uncovered.append(f"{asset_id} (missing {', '.join(absent)})")
        reasons: list[str] = []
        if unmodelled:
            # Deliberately does not assert WHY there is no model. The detector genuinely
            # does not know: it may never have been trained on this asset, or `fit` may have
            # refused it (too short a history, a missing pinned channel). Naming a cause it
            # cannot see is the failure this capability exists to prevent, one level in.
            reasons.append(
                f"has no fitted model for {', '.join(unmodelled)} - a baseline is fitted per "
                f"asset, so an asset absent from the fit is not judged"
            )
        if uncovered:
            reasons.append(
                f"has a model for {', '.join(uncovered)} whose fitted channels are not all "
                f"present, so scoring would use a misaligned matrix"
            )
        if not reasons:
            return None
        return f"{self.method} judged nothing on some assets: it {' and it '.join(reasons)}"

    def _detect_asset(
        self, asset_id: str, events: list[NormalizedEvent], state: Team3State
    ) -> list[DetectionResult]:
        # State is a parameter rather than re-read from self: detect() has already refused
        # the unfitted case, so passing it makes the narrowing a signature instead of a
        # runtime assertion.
        wide = wide_frame(events)

        # Feature columns are sacred: skip an asset that lacks any trained channel
        # rather than score it against a misaligned matrix (ADR-0012 D4).
        if _uncovered(state, wide.columns):
            return []

        wide = wide.reindex(columns=state.channels).sort_index().ffill().dropna()
        if wide.empty:
            return []
        # The tz-aware DatetimeIndex carries the real timestamps; feature-building
        # runs on an integer index and surviving rows are mapped back through it, so
        # the datetimes never pass through pandas' tz-lossy reset machinery.
        timestamps = wide.index
        wide = wide.reset_index(drop=True)

        params = self._params
        features = build_multivariate_features(
            wide,
            window=params.window_size,
            lag_steps=params.lag_steps,
            fft_enabled=params.fft_enabled,
            fft_include_dc=params.fft_include_dc,
        )
        if features.empty:
            return []
        # Enforce the trained order; identical builder + validated channels means
        # the columns already match, but reindex makes the positional contract explicit.
        features = features.reindex(columns=state.feature_columns)

        positions = features.index.to_numpy()
        # Select only the surviving rows' timestamps, then convert (iterating the
        # index is what keeps the datetimes tz-aware through pandas).
        row_ts = [ts.to_pydatetime() for ts in timestamps[positions]]

        # One z-matrix pass; the row score is its per-row max (the evidence step
        # reuses the same matrix, so it is never recomputed).
        z_matrix = featurewise_robust_z(features.to_numpy(dtype=float), state.baseline)
        scores = z_matrix.max(axis=1)
        anomaly = np.isfinite(scores) & (scores > state.threshold)
        if not anomaly.any():
            return []

        # Each engineered feature's source channel, resolved once per asset so evidence is
        # a lookup rather than a per-detection re-scan. Built here rather than at
        # construction because it is needed only by an asset that actually fired, and a
        # fleet artifact would otherwise pay for every model on every detector build.
        by_feature = {name: _channel_of(name, state.channels) for name in state.feature_columns}
        detections: list[DetectionResult] = []
        for start, end in merge_runs(anomaly):
            length = end - start + 1
            if length < params.min_segment_length:
                continue
            peak = start + int(np.argmax(scores[start : end + 1]))
            detections.append(
                DetectionResult(
                    asset_id=asset_id,
                    window_start=row_ts[start],
                    window_end=row_ts[end],
                    lane=self.lane,
                    method=self.method,
                    score=float(scores[peak]),
                    threshold=float(state.threshold),
                    event_kind=classify_run(length),
                    evidence=self._evidence(
                        state, by_feature, z_matrix[peak], row_ts[peak], scores[peak], length
                    ),
                )
            )
        return detections

    def _evidence(
        self,
        state: Team3State,
        by_feature: dict[str, str],
        z_row: np.ndarray,
        peak_ts: datetime,
        peak_score: float,
        length: int,
    ) -> dict[str, Any]:
        # Zipped against the state's own column order, which is what z_row is aligned to;
        # the lookup dict is a pure cache and must not be the thing defining the order.
        per_channel: dict[str, float] = {}
        for name, z in zip(state.feature_columns, z_row, strict=True):
            channel = by_feature[name]
            per_channel[channel] = max(per_channel.get(channel, 0.0), float(z))
        top = sorted(per_channel.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_CHANNELS]
        leader = f"{top[0][0]} (z={top[0][1]:.1f})" if top else "no dominant channel"
        return {
            # The analyst-readable headline every detector emits (ADR-0032 D7); the fields
            # below are its supporting detail.
            "explanation": (
                f"{length} consecutive reading(s) far from this asset's baseline, led by {leader}"
            ),
            "peak_timestamp": peak_ts.isoformat(),
            "peak_score": float(peak_score),
            "num_anomalous_points": length,
            "top_channels": [{"channel": c, "z": round(z, 4)} for c, z in top],
        }
