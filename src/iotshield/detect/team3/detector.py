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

The model parameters live entirely in a ``Team3Artifact``; this class is pure
inference and has no dependency beyond numpy/pandas.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from iotshield.contracts import DetectionResult, EventKind, Lane, NormalizedEvent
from iotshield.detect.team3._features import build_multivariate_features
from iotshield.detect.team3._zscore import featurewise_robust_z
from iotshield.detect.team3.artifact import Team3Artifact, load_artifact

_MIN_COLLECTIVE = 3  # run length at/above which an episode is "collective" (team3 postprocess)
_TOP_CHANNELS = 3


def _classify(run_length: int) -> EventKind:
    # Mirrors team3 evaluation/postprocess.py:classify_event_type.
    if run_length <= 1:
        return EventKind.POINT
    if run_length >= _MIN_COLLECTIVE:
        return EventKind.COLLECTIVE
    return EventKind.CONTEXTUAL


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive [start, end] index ranges of each maximal True run."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for i, flagged in enumerate(mask):
        if flagged and start is None:
            start = i
        elif not flagged and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    return runs


def _channel_of(feature_name: str, channels: Sequence[str]) -> str:
    # Longest channel prefix the feature name starts with (robust to channel names
    # that themselves contain separators, e.g. "Volume Flow RateRMS").
    matches = [c for c in channels if feature_name.startswith(f"{c}_")]
    return max(matches, key=len) if matches else feature_name


class Team3ZScoreDetector:
    def __init__(self, artifact: Team3Artifact, *, min_segment_length: int = 1) -> None:
        if min_segment_length < 1:
            raise ValueError("min_segment_length must be >= 1")
        self._artifact = artifact
        self._min_segment_length = min_segment_length
        # Resolve each engineered feature's source channel once, so evidence is a
        # lookup rather than a per-detection re-scan.
        self._channel_by_feature = {
            name: _channel_of(name, artifact.channels) for name in artifact.feature_columns
        }

    @classmethod
    def from_resource(
        cls, artifact: str = "skab_zscore", *, min_segment_length: int = 1
    ) -> Team3ZScoreDetector:
        return cls(load_artifact(artifact), min_segment_length=min_segment_length)

    @property
    def lane(self) -> Lane:
        return Lane.TELEMETRY

    @property
    def method(self) -> str:
        return self._artifact.method

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        by_asset: dict[str, list[NormalizedEvent]] = defaultdict(list)
        for event in events:
            if event.source_type is not self.lane or event.metric is None or event.value is None:
                continue
            by_asset[event.asset_id].append(event)

        detections: list[DetectionResult] = []
        for asset_id in sorted(by_asset):  # deterministic asset order
            detections.extend(self._detect_asset(asset_id, by_asset[asset_id]))
        return detections

    def _detect_asset(self, asset_id: str, events: list[NormalizedEvent]) -> list[DetectionResult]:
        art = self._artifact
        # detect() already dropped metric/value-less events; the guard here only
        # narrows the type to float and keeps the three columns aligned.
        records = [
            (e.timestamp, e.metric, float(e.value))
            for e in events
            if e.metric is not None and e.value is not None
        ]
        frame = pd.DataFrame(records, columns=["ts", "metric", "value"])
        wide = frame.pivot_table(index="ts", columns="metric", values="value", aggfunc="mean")

        # Feature columns are sacred: skip an asset that lacks any trained channel
        # rather than score it against a misaligned matrix (ADR-0012 D4).
        if not set(art.channels).issubset(wide.columns):
            return []

        wide = wide.reindex(columns=art.channels).sort_index().ffill().dropna()
        if wide.empty:
            return []
        # The tz-aware DatetimeIndex carries the real timestamps; feature-building
        # runs on an integer index and surviving rows are mapped back through it, so
        # the datetimes never pass through pandas' tz-lossy reset machinery.
        timestamps = wide.index
        wide = wide.reset_index(drop=True)

        features = build_multivariate_features(
            wide,
            window=art.window_size,
            lag_steps=art.lag_steps,
            fft_enabled=art.fft.enabled,
            fft_include_dc=art.fft.include_dc,
        )
        if features.empty:
            return []
        # Enforce the trained order; identical builder + validated channels means
        # the columns already match, but reindex makes the positional contract explicit.
        features = features.reindex(columns=art.feature_columns)

        positions = features.index.to_numpy()
        # Select only the surviving rows' timestamps, then convert (iterating the
        # index is what keeps the datetimes tz-aware through pandas).
        row_ts = [ts.to_pydatetime() for ts in timestamps[positions]]

        # One z-matrix pass; the row score is its per-row max (the evidence step
        # reuses the same matrix, so it is never recomputed).
        z_matrix = featurewise_robust_z(features.to_numpy(dtype=float), art.baseline)
        scores = z_matrix.max(axis=1)
        anomaly = np.isfinite(scores) & (scores > art.threshold)
        if not anomaly.any():
            return []

        detections: list[DetectionResult] = []
        for start, end in _runs(anomaly):
            length = end - start + 1
            if length < self._min_segment_length:
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
                    threshold=float(art.threshold),
                    event_kind=_classify(length),
                    evidence=self._evidence(z_matrix[peak], row_ts[peak], scores[peak], length),
                )
            )
        return detections

    def _evidence(
        self, z_row: np.ndarray, peak_ts: datetime, peak_score: float, length: int
    ) -> dict[str, Any]:
        per_channel: dict[str, float] = {}
        for name, z in zip(self._artifact.feature_columns, z_row, strict=True):
            channel = self._channel_by_feature[name]
            per_channel[channel] = max(per_channel.get(channel, 0.0), float(z))
        top = sorted(per_channel.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_CHANNELS]
        return {
            "peak_timestamp": peak_ts.isoformat(),
            "peak_score": float(peak_score),
            "num_anomalous_points": length,
            "top_channels": [{"channel": c, "z": round(z, 4)} for c, z in top],
        }
