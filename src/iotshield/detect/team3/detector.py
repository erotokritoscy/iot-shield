"""Team3 telemetry detector: robust z-score over engineered features.

The Detector port adapter (ADR-0012). It does the work team3's orchestration did
around the model, but rewritten to our contract:

- filters to its lane and groups events by asset (the runner routes by lane;
  this stays defensive per the port);
- **pivots** univariate events back into a ``(time x channel)`` matrix (ADR-0003
  D1) — the inverse of the normalizer's fan-out;
- **validates channel coverage** before scoring: an asset missing any trained
  channel is skipped, never scored against a misaligned matrix (ADR-0012 D4);
- scores each row with the vendored robust z-score, thresholds it, and **merges
  consecutive anomalous rows into episodes** — one ``DetectionResult`` per run,
  with ``event_kind`` from run length and top-channel ``evidence`` (ADR-0012 D5).

Constructed from two things, never one (ADR-0022): ``Team3Params``, the hyperparameters a
human chose, and an optional ``Team3State``, the baseline a previous fit computed. Without
state it is an untrained model — legal to build, legal to ``fit()``, and refusing to
``detect()``. No dependency beyond numpy/pandas.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from iotshield.capabilities import DETECTIONS, Capability, LaneToken
from iotshield.contracts import DetectionResult, EventKind, Lane, NormalizedEvent
from iotshield.detect._fitted import NotFitted
from iotshield.detect._group import group_by_asset
from iotshield.detect._runs import merge_runs
from iotshield.detect.team3._features import build_multivariate_features
from iotshield.detect.team3._zscore import featurewise_robust_z
from iotshield.detect.team3.artifact import Team3State
from iotshield.detect.team3.params import METHOD, Team3Params
from iotshield.detect.team3.train import train_state

# Fixed telemetry-lane detector; declared for the compatibility guard.
CAPABILITY = Capability.of(consumes={LaneToken(Lane.TELEMETRY)}, produces={DETECTIONS})

_MIN_COLLECTIVE = 3  # run length at/above which an episode is "collective" (team3 postprocess)
_TOP_CHANNELS = 3


def _classify(run_length: int) -> EventKind:
    # Mirrors team3 evaluation/postprocess.py:classify_event_type.
    if run_length <= 1:
        return EventKind.POINT
    if run_length >= _MIN_COLLECTIVE:
        return EventKind.COLLECTIVE
    return EventKind.CONTEXTUAL


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


class Team3ZScoreDetector:
    def __init__(self, params: Team3Params, state: Team3State | None = None) -> None:
        self._params = params
        self._state = state
        # Resolve each engineered feature's source channel once, so evidence is a
        # lookup rather than a per-detection re-scan.
        self._channel_by_feature = (
            {name: _channel_of(name, state.channels) for name in state.feature_columns}
            if state is not None
            else {}
        )

    @property
    def is_fitted(self) -> bool:
        return self._state is not None

    @property
    def lane(self) -> Lane:
        return Lane.TELEMETRY

    @property
    def method(self) -> str:
        return METHOD

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        # Foreign-lane isolation and deterministic asset order are the port's laws, shared
        # by every detector; only the "is this event judgeable" predicate is ours.
        if self._state is None:
            raise NotFitted(
                f"{METHOD} was built from hyperparameters with no trained baseline; name an "
                "'artifact' in its params, or fit it first (--fit-protocol prefix)"
            )
        detections: list[DetectionResult] = []
        for asset_id, asset_events in group_by_asset(events, lane=self.lane, keep=_has_reading):
            detections.extend(self._detect_asset(asset_id, asset_events, self._state))
        return detections

    def fit(self, events: Sequence[NormalizedEvent]) -> Team3ZScoreDetector:
        """Relearn the baseline from ``events`` (assumed normal); return a fitted detector.

        The optional ``Fittable`` capability (ADR-0021 D3). Never called by the pipeline —
        it exists so an evaluation can train on an asset's own clean history rather than
        measuring domain shift, which ADR-0012 D3 found is what sinks a foreign baseline.

        Every hyperparameter comes from ``self._params`` — this instance's *construction* —
        and never from a previously loaded artifact (ADR-0022 rule 2). What is learned is
        the baseline, the threshold, and the feature schema, including ``channels`` when
        they were left unpinned: an unpinned detector adopts whatever channels the training
        data carries, which is what lets one adapter fit a rig it has never seen.

        Rolling and lag features assume one asset's contiguous history, so several assets
        would interleave into a meaningless matrix; that is refused rather than fitted.
        """
        lane_events = [
            event for event in events if event.source_type is self.lane and _has_reading(event)
        ]
        if not lane_events:
            raise ValueError("fit needs telemetry readings with a metric and value; got none")
        assets = {event.asset_id for event in lane_events}
        if len(assets) > 1:
            raise ValueError(
                f"fit expects one asset's history; got {len(assets)} ({', '.join(sorted(assets))})"
            )
        wide = wide_frame(sorted(lane_events, key=lambda event: event.timestamp))
        # Pinned channels are a promise the data must keep; unpinned ones are learned from
        # it. pivot_table sorts its columns, so the learned order is deterministic.
        channels = tuple(self._params.channels or wide.columns)
        missing = [channel for channel in channels if channel not in wide.columns]
        if missing:
            raise ValueError(f"fit input is missing pinned channels: {missing}")
        frame = wide.reindex(columns=list(channels)).sort_index().ffill().dropna()

        state = train_state(
            frame.reset_index(drop=True),
            self._params,
            channels=channels,
            provenance=f"refit on {len(frame)} normal rows of {', '.join(sorted(assets))}",
        )
        return Team3ZScoreDetector(self._params, state)

    def _detect_asset(
        self, asset_id: str, events: list[NormalizedEvent], state: Team3State
    ) -> list[DetectionResult]:
        # State is a parameter rather than re-read from self: detect() has already refused
        # the unfitted case, so passing it makes the narrowing a signature instead of a
        # runtime assertion.
        wide = wide_frame(events)

        # Feature columns are sacred: skip an asset that lacks any trained channel
        # rather than score it against a misaligned matrix (ADR-0012 D4).
        if not set(state.channels).issubset(wide.columns):
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
                    event_kind=_classify(length),
                    evidence=self._evidence(
                        state, z_matrix[peak], row_ts[peak], scores[peak], length
                    ),
                )
            )
        return detections

    def _evidence(
        self,
        state: Team3State,
        z_row: np.ndarray,
        peak_ts: datetime,
        peak_score: float,
        length: int,
    ) -> dict[str, Any]:
        # Zipped against the state's own column order, which is what z_row is aligned to;
        # the lookup dict is a pure cache and must not be the thing defining the order.
        per_channel: dict[str, float] = {}
        for name, z in zip(state.feature_columns, z_row, strict=True):
            channel = self._channel_by_feature[name]
            per_channel[channel] = max(per_channel.get(channel, 0.0), float(z))
        top = sorted(per_channel.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_CHANNELS]
        return {
            "peak_timestamp": peak_ts.isoformat(),
            "peak_score": float(peak_score),
            "num_anomalous_points": length,
            "top_channels": [{"channel": c, "z": round(z, 4)} for c, z in top],
        }
