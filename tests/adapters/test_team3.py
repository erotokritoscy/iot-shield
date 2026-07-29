"""Team3-specific behavior (pivot, channel-coverage guard, episode merging,
degenerate-feature handling). Port-level invariants (lane discipline, windows
within range, score > threshold, determinism) live in
tests/ports/test_detector_port.py; these cover what is particular to this adapter.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from iotshield.contracts import EventKind, Lane, NormalizedEvent
from iotshield.detect._fitted import NotFitted
from iotshield.detect._runs import classify_run
from iotshield.detect.team3.artifact import (
    Team3Artifact,
    Team3State,
    load_artifact,
    parse_artifact,
)
from iotshield.detect.team3.detector import Team3ZScoreDetector
from iotshield.detect.team3.params import Team3Params
from iotshield.detect.team3.train import DEFAULT_ASSET_ID, train_state
from tests.team3_fixtures import make_detector, make_state, normal_frame

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)

ASSET = "skab-testbed"
"""The device these cases build events for, and therefore the one models are filed under."""


def events_from(series: dict[str, list[float]], asset: str = ASSET) -> list[NormalizedEvent]:
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


def normal_events(
    channels: Sequence[str], *, asset: str = ASSET, rows: int = 120
) -> list[NormalizedEvent]:
    """A quiet multi-channel recording for one asset — what "normal" looks like to fit()."""
    frame = normal_frame(channels, rows, seed=7)
    return events_from({c: list(frame[c]) for c in channels}, asset=asset)


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
        det = make_detector(["Pressure"])
        dets = det.detect(events_from({"Pressure": flat_with_spike(25, range(10, 18))}))
        assert len(dets) == 1
        (d,) = dets
        assert d.event_kind == EventKind.COLLECTIVE
        assert d.lane == Lane.TELEMETRY and d.method == "team3_zscore"
        assert d.score > d.threshold
        # window lies inside the sustained excursion's span
        assert d.window_start <= d.window_end

    def test_min_segment_length_suppresses_short_episodes(self) -> None:
        events = events_from({"Pressure": flat_with_spike(25, range(10, 14))})
        assert make_detector(["Pressure"], min_segment_length=1).detect(events)
        assert make_detector(["Pressure"], min_segment_length=1000).detect(events) == []

    def test_classify_maps_run_length_to_event_kind(self) -> None:
        # The rule now lives in detect/_runs.py so both telemetry detectors share it;
        # tests/detect/test_runs.py owns the boundaries. This keeps team3's own coverage
        # of the mapping it depends on.
        assert classify_run(1) == EventKind.POINT
        assert classify_run(2) == EventKind.CONTEXTUAL
        assert classify_run(3) == EventKind.COLLECTIVE
        assert classify_run(50) == EventKind.COLLECTIVE


class TestPivotAndGuards:
    def test_top_channel_is_the_one_that_spiked(self) -> None:
        det = make_detector(["Accel", "Pressure"])
        series = {"Accel": [10.0] * 25, "Pressure": flat_with_spike(25, range(10, 18))}
        (d,) = det.detect(events_from(series))
        assert d.evidence["top_channels"][0]["channel"] == "Pressure"

    def test_asset_missing_a_trained_channel_is_skipped(self) -> None:
        det = make_detector(["A", "B"])
        # Only channel A present -> cannot form the matrix -> skip, never misalign.
        assert det.detect(events_from({"A": [10.0] * 25})) == []

    def test_input_shorter_than_window_yields_nothing(self) -> None:
        det = make_detector(["Pressure"], window=10)
        assert det.detect(events_from({"Pressure": [10.0, 11.0, 60.0]})) == []

    def test_each_asset_scored_independently(self) -> None:
        det = make_detector(["Pressure"], assets=("calm", "faulty"))
        calm = events_from({"Pressure": [10.0] * 25}, asset="calm")
        faulty = events_from({"Pressure": flat_with_spike(25, range(10, 18))}, asset="faulty")
        assets = {d.asset_id for d in det.detect(calm + faulty)}
        assert assets == {"faulty"}


class TestNumericalSafety:
    def test_extreme_spike_stays_finite(self) -> None:
        # A huge spike must not produce inf/nan (the contract rejects those).
        det = make_detector(["Pressure"])
        dets = det.detect(events_from({"Pressure": flat_with_spike(25, range(10, 16), spike=1e9)}))
        assert dets and all(np.isfinite(d.score) for d in dets)

    def test_all_constant_training_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="degenerate"):
            train_state(
                pd.DataFrame({"A": [5.0] * 100}),
                Team3Params(window_size=5, lag_steps=(1,)),
                asset_id="rig-01",
                channels=["A"],
                provenance="p",
            )


class TestHyperparametersAndFittedState:
    """ADR-0022, as it lands on this adapter: what fitting may and may not carry over."""

    def _quiet(self, channels: tuple[str, ...] = ("Pressure",)) -> list[NormalizedEvent]:
        return normal_events(channels)

    def test_fit_uses_the_constructed_window_not_the_previous_one(self) -> None:
        """The regression this ADR exists for.

        ``fit()`` used to read window/lags/percentile/channels out of the artifact it was
        loaded from, so two detectors built with different hyperparameters produced
        *identical* fitted models — the override silently did nothing.
        """
        training = self._quiet()
        narrow = make_detector(["Pressure"], window=4).fit(training).model_for(ASSET)
        wide = make_detector(["Pressure"], window=30).fit(training).model_for(ASSET)
        assert len(narrow.feature_columns) != len(wide.feature_columns) or (
            narrow.threshold != wide.threshold
        )

    def test_fit_keeps_the_hyperparameters_it_was_built_with(self) -> None:
        detector = make_detector(["Pressure"], window=7, percentile=95.0)
        fitted = detector.fit(self._quiet())
        assert fitted._params == detector._params

    def test_unpinned_channels_are_learned_from_the_training_data(self) -> None:
        # No `channels` in the params: the detector adopts whatever the rig actually
        # carries, which is what lets one adapter fit a rig it has never seen.
        detector = Team3ZScoreDetector(Team3Params(window_size=5, lag_steps=(1,)))
        assert detector.is_fitted is False
        fitted = detector.fit(self._quiet(("Current", "Pressure")))
        assert fitted.is_fitted is True
        assert fitted.model_for(ASSET).channels == ["Current", "Pressure"]

    def test_pinned_channels_are_a_promise_the_data_must_keep(self) -> None:
        detector = Team3ZScoreDetector(
            Team3Params(window_size=5, lag_steps=(1,), channels=("Pressure", "Torque"))
        )
        with pytest.raises(ValueError, match="missing pinned channels"):
            detector.fit(self._quiet(("Pressure",)))

    def test_an_untrained_detector_refuses_to_judge(self) -> None:
        # Not an empty list: "no anomalies" is a successful verdict, and the most
        # dangerous wrong answer a detector can give.
        detector = Team3ZScoreDetector(Team3Params())
        with pytest.raises(NotFitted, match="no trained baseline"):
            detector.detect(events_from({"Pressure": [10.0] * 25}))

    def test_fitting_needs_readings(self) -> None:
        with pytest.raises(ValueError, match="got none"):
            Team3ZScoreDetector(Team3Params()).fit([])


class TestArtifact:
    def test_json_round_trip_preserves_scoring(self) -> None:
        detector = make_detector(["Pressure", "Current"])
        state = detector.model_for(ASSET)
        reloaded = Team3State.model_validate_json(state.model_dump_json())
        assert reloaded.feature_columns == state.feature_columns
        assert reloaded.median == state.median and reloaded.mad == state.mad
        events = events_from(
            {"Pressure": flat_with_spike(25, range(10, 18)), "Current": [10.0] * 25}
        )
        assert fingerprint(detector.detect(events)) == fingerprint(
            Team3ZScoreDetector(detector._params, {ASSET: reloaded}).detect(events)
        )

    def test_length_mismatch_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="median/mad length"):
            Team3State(
                asset_id="rig-01",
                provenance="p",
                channels=["A"],
                feature_columns=["A_mean", "A_std"],
                median=[0.0],
                mad=[1.0],
                threshold=1.0,
            )

    def test_committed_skab_artifact_loads(self) -> None:
        """The one place the shipped model's own numbers are pinned.

        Every other suite builds a hermetic artifact, so nothing else would notice if this
        file were reshaped, truncated, or retrained by accident — and it is the model the
        demos and the SKAB benchmark actually run.
        """
        art = load_artifact("skab_zscore")
        assert art.params.channels is not None and len(art.params.channels) == 8
        assert (art.params.window_size, art.params.percentile) == (60, 99.0)
        # One model, and WHICH asset it is for is part of what ships: filed under any other
        # id it would silently judge nothing (ADR-0034).
        assert [state.asset_id for state in art.models] == [DEFAULT_ASSET_ID]
        state = art.models[0]
        assert state.channels == list(art.params.channels)
        assert len(state.median) == len(state.mad) == len(state.feature_columns) == 45
        assert state.threshold == pytest.approx(24.539536363633378)

    def test_unknown_artifact_id_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="unknown team3 artifact"):
            load_artifact("does-not-exist")

    def test_a_stale_schema_version_says_to_regenerate(self) -> None:
        """A v1 file must fail on the fact that matters, not on a list of unexpected keys.

        ``extra="forbid"`` would otherwise report ten unknown fields, burying the one
        actionable thing: this artifact predates the params/state split (ADR-0022).
        """
        v1 = '{"schema_version": 1, "method": "team3_zscore", "window_size": 60}'
        with pytest.raises(ValueError, match="regenerate"):
            parse_artifact(v1, "stale")

    def test_a_v2_artifact_says_to_regenerate(self) -> None:
        """v2 is refused even though wrapping its one `state` in a list would "work".

        The upgrade would have to invent the asset id that state belongs to, and inventing
        it wrong is the quietest failure available: the detector skips every real asset and
        reports no detections, which reads as "nothing was wrong" (ADR-0034).
        """
        v2 = '{"schema_version": 2, "params": {}, "state": {"provenance": "p"}}'
        with pytest.raises(ValueError, match="regenerate"):
            parse_artifact(v2, "stale")

    def test_one_asset_cannot_appear_twice(self) -> None:
        state = make_state(Team3Params(window_size=5, lag_steps=(1,)), asset_id=ASSET)
        with pytest.raises(ValueError, match="duplicate asset_id"):
            Team3Artifact(params=Team3Params(window_size=5, lag_steps=(1,)), models=[state, state])


class TestPerAssetModels:
    """ADR-0034: a baseline describes ONE device, on both sides of the adapter.

    The reasoning lives in `Team3ZScoreDetector`'s docstring and the ADR; these pin the
    behaviour, so each case below names what breaks rather than re-deriving why.
    """

    def _untrained(self) -> Team3ZScoreDetector:
        """Unpinned channels, so each asset's schema is genuinely learned from its own data."""
        return Team3ZScoreDetector(Team3Params(window_size=5, lag_steps=(1,)))

    def test_fit_produces_one_model_per_asset(self) -> None:
        training = normal_events(["Pressure"], asset="rig-a") + normal_events(
            ["Pressure"], asset="rig-b"
        )
        fitted = self._untrained().fit(training)
        assert fitted.fitted_assets == ("rig-a", "rig-b")
        # Each model names its own device, so a model read out of the artifact is
        # self-describing rather than meaningful only in its container.
        assert fitted.model_for("rig-a").asset_id == "rig-a"

    def test_channels_are_learned_per_asset_on_a_heterogeneous_fleet(self) -> None:
        """D3: a fleet with no common channel set is still fully judgeable.

        Breaking this means a pooled union schema, and the coverage guard then skips every
        asset for missing a channel it never had.
        """
        training = normal_events(["FrameRate", "Temperature"], asset="camera") + normal_events(
            ["CpuLoad", "Temperature"], asset="gateway"
        )
        fitted = self._untrained().fit(training)
        assert fitted.model_for("camera").channels == ["FrameRate", "Temperature"]
        assert fitted.model_for("gateway").channels == ["CpuLoad", "Temperature"]
        # And both are judgeable: neither is asked to satisfy the other's schema.
        assert fitted.why_silent(training) is None

    def test_an_asset_with_no_model_is_skipped_not_scored(self) -> None:
        """D2: a device with no model is skipped, not judged against another's baseline."""
        fitted = self._untrained().fit(normal_events(["Pressure"], asset="rig-a"))
        spike = events_from({"Pressure": flat_with_spike(25, range(10, 18))}, asset="stranger")
        assert fitted.detect(spike) == []

    def test_why_silent_separates_its_two_reasons(self) -> None:
        """Never trained here, and trained on a different shape, call for different actions."""
        fitted = self._untrained().fit(normal_events(["Pressure", "Current"], asset="rig-a"))
        events = events_from({"Pressure": [10.0] * 25}, asset="rig-a") + events_from(
            {"Pressure": [10.0] * 25}, asset="stranger"
        )
        reason = fitted.why_silent(events)
        assert reason is not None
        assert "no fitted model for stranger" in reason
        assert "rig-a (missing Current)" in reason

    def test_one_unfittable_asset_does_not_sink_the_fleet(self) -> None:
        """A fleet fit is not all-or-nothing: 29 good pumps must not be lost to one short one."""
        training = normal_events(["Pressure"], asset="rig-a") + events_from(
            {"Pressure": [10.0, 11.0, 10.5]}, asset="too-short"
        )
        fitted = self._untrained().fit(training)
        assert fitted.fitted_assets == ("rig-a",)

    def test_fit_refuses_only_when_no_asset_could_be_fitted(self) -> None:
        """...and names each refusal, so "nothing fitted" is diagnosable rather than blank."""
        training = events_from({"Pressure": [10.0, 11.0]}, asset="a") + events_from(
            {"Pressure": [10.0, 11.0]}, asset="b"
        )
        with pytest.raises(ValueError, match="no asset could be fitted") as excinfo:
            self._untrained().fit(training)
        assert "a (" in str(excinfo.value) and "b (" in str(excinfo.value)

    def test_a_missing_pinned_channel_skips_that_asset_only(self) -> None:
        detector = Team3ZScoreDetector(
            Team3Params(window_size=5, lag_steps=(1,), channels=("Pressure", "Current"))
        )
        training = normal_events(["Pressure", "Current"], asset="complete") + normal_events(
            ["Pressure"], asset="partial"
        )
        assert detector.fit(training).fitted_assets == ("complete",)

    def test_two_assets_fitted_together_get_genuinely_different_baselines(self) -> None:
        """The fleet is split before any feature is computed, not averaged into one device.

        Pooled, the pivot keys on (timestamp, metric) with no asset, so two devices reporting
        the same channel at the same instant were averaged into a device that does not exist.
        Different underlying levels must therefore produce different learned medians.
        """
        quiet = normal_events(["Pressure"], asset="quiet")
        loud = [
            event.model_copy(update={"asset_id": "loud", "value": (event.value or 0.0) * 10.0})
            for event in quiet
        ]
        fitted = self._untrained().fit(quiet + loud)
        assert fitted.model_for("quiet").median != fitted.model_for("loud").median
