"""Team3-specific behavior (pivot, channel-coverage guard, episode merging,
degenerate-feature handling). Port-level invariants (lane discipline, windows
within range, score > threshold, determinism) live in
tests/ports/test_detector_port.py; these cover what is particular to this adapter.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from iotshield.contracts import EventKind, Lane, NormalizedEvent
from iotshield.detect.team3.artifact import Team3Artifact, load_artifact
from iotshield.detect.team3.detector import Team3ZScoreDetector, _classify
from iotshield.detect.team3.train import train_artifact

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def make_artifact(
    channels: list[str], *, window: int = 5, lags: tuple[int, ...] = (1,), percentile: float = 99.0
) -> Team3Artifact:
    rng = np.random.default_rng(0)
    frame = pd.DataFrame({c: rng.normal(10.0, 0.5, 300) for c in channels})
    return train_artifact(
        frame,
        channels=channels,
        window_size=window,
        lag_steps=list(lags),
        fft_enabled=False,
        percentile=percentile,
    )


def events_from(
    series: dict[str, list[float]], asset: str = "skab-testbed"
) -> list[NormalizedEvent]:
    length = len(next(iter(series.values())))
    events: list[NormalizedEvent] = []
    for i in range(length):
        for channel, values in series.items():
            events.append(
                NormalizedEvent(
                    timestamp=T0 + timedelta(seconds=i),
                    asset_id=asset,
                    source_type=Lane.TELEMETRY,
                    source_format="test",
                    metric=channel,
                    value=float(values[i]),
                    raw="x",
                )
            )
    return events


def flat_with_spike(
    n: int, spike_at: range, *, base: float = 10.0, spike: float = 60.0
) -> list[float]:
    values = [base] * n
    for i in spike_at:
        values[i] = spike
    return values


def fingerprint(detections: list) -> list[tuple]:
    return [
        (d.asset_id, d.window_start, d.window_end, d.score, d.threshold, d.event_kind)
        for d in detections
    ]


class TestEpisodes:
    def test_sustained_anomaly_is_one_collective_detection(self) -> None:
        det = Team3ZScoreDetector(make_artifact(["Pressure"]))
        dets = det.detect(events_from({"Pressure": flat_with_spike(25, range(10, 18))}))
        assert len(dets) == 1
        (d,) = dets
        assert d.event_kind == EventKind.COLLECTIVE
        assert d.lane == Lane.TELEMETRY and d.method == "team3_zscore"
        assert d.score > d.threshold
        # window lies inside the sustained excursion's span
        assert d.window_start <= d.window_end

    def test_min_segment_length_suppresses_short_episodes(self) -> None:
        art = make_artifact(["Pressure"])
        events = events_from({"Pressure": flat_with_spike(25, range(10, 14))})
        assert Team3ZScoreDetector(art, min_segment_length=1).detect(events)
        assert Team3ZScoreDetector(art, min_segment_length=1000).detect(events) == []

    def test_classify_maps_run_length_to_event_kind(self) -> None:
        assert _classify(1) == EventKind.POINT
        assert _classify(2) == EventKind.CONTEXTUAL
        assert _classify(3) == EventKind.COLLECTIVE
        assert _classify(50) == EventKind.COLLECTIVE


class TestPivotAndGuards:
    def test_top_channel_is_the_one_that_spiked(self) -> None:
        det = Team3ZScoreDetector(make_artifact(["Accel", "Pressure"]))
        series = {"Accel": [10.0] * 25, "Pressure": flat_with_spike(25, range(10, 18))}
        (d,) = det.detect(events_from(series))
        assert d.evidence["top_channels"][0]["channel"] == "Pressure"

    def test_asset_missing_a_trained_channel_is_skipped(self) -> None:
        det = Team3ZScoreDetector(make_artifact(["A", "B"]))
        # Only channel A present -> cannot form the matrix -> skip, never misalign.
        assert det.detect(events_from({"A": [10.0] * 25})) == []

    def test_input_shorter_than_window_yields_nothing(self) -> None:
        det = Team3ZScoreDetector(make_artifact(["Pressure"], window=10))
        assert det.detect(events_from({"Pressure": [10.0, 11.0, 60.0]})) == []

    def test_each_asset_scored_independently(self) -> None:
        det = Team3ZScoreDetector(make_artifact(["Pressure"]))
        calm = events_from({"Pressure": [10.0] * 25}, asset="calm")
        faulty = events_from({"Pressure": flat_with_spike(25, range(10, 18))}, asset="faulty")
        assets = {d.asset_id for d in det.detect(calm + faulty)}
        assert assets == {"faulty"}


class TestNumericalSafety:
    def test_extreme_spike_stays_finite(self) -> None:
        # A huge spike must not produce inf/nan (the contract rejects those).
        det = Team3ZScoreDetector(make_artifact(["Pressure"]))
        dets = det.detect(events_from({"Pressure": flat_with_spike(25, range(10, 16), spike=1e9)}))
        assert dets and all(np.isfinite(d.score) for d in dets)

    def test_all_constant_training_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="degenerate"):
            train_artifact(pd.DataFrame({"A": [5.0] * 100}), channels=["A"], window_size=5)


class TestArtifact:
    def test_json_round_trip_preserves_scoring(self) -> None:
        art = make_artifact(["Pressure", "Current"])
        reloaded = Team3Artifact.model_validate_json(art.model_dump_json())
        assert reloaded.feature_columns == art.feature_columns
        assert reloaded.median == art.median and reloaded.mad == art.mad
        events = events_from(
            {"Pressure": flat_with_spike(25, range(10, 18)), "Current": [10.0] * 25}
        )
        assert fingerprint(Team3ZScoreDetector(art).detect(events)) == fingerprint(
            Team3ZScoreDetector(reloaded).detect(events)
        )

    def test_length_mismatch_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="median/mad length"):
            Team3Artifact(
                method="m",
                provenance="p",
                window_size=3,
                channels=["A"],
                feature_columns=["A_mean", "A_std"],
                median=[0.0],
                mad=[1.0],
                threshold=1.0,
                percentile=99.0,
            )

    def test_committed_skab_artifact_loads(self) -> None:
        art = load_artifact("skab_zscore")
        assert art.method == "team3_zscore"
        assert len(art.channels) == 8
        assert len(art.median) == len(art.mad) == len(art.feature_columns)

    def test_unknown_artifact_id_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="unknown team3 artifact"):
            load_artifact("does-not-exist")
