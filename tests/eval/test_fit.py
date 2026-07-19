"""Fit protocols: what a detector learns from, and what happens when it cannot learn.

The product stance under test is "optional, never enforced" — every path that cannot fit
degrades to evaluating as configured and *says so*, rather than raising. A benchmark that
died because one of several detectors was not fittable would be far less useful than one
that reports which ones were.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.detect import Fittable, LogBurstDetector, StubThresholdDetector, Team3ZScoreDetector
from iotshield.detect.team3.train import train_artifact
from iotshield.eval import GroundTruth, prepare_detector
from iotshield.eval.fit import FitProtocol, normal_prefix

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def at(second: int) -> datetime:
    return T0 + timedelta(seconds=second)


def event(second: int, value: float = 10.0, asset: str = "pump-02") -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=at(second),
        asset_id=asset,
        source_type=Lane.TELEMETRY,
        source_format="test",
        metric="Pressure",
        value=value,
        raw="x",
    )


def truth_of(flags: list[bool]) -> GroundTruth:
    return GroundTruth.from_timeline([(at(i), flag) for i, flag in enumerate(flags)])


def fittable_detector() -> Team3ZScoreDetector:
    rng = np.random.default_rng(0)
    artifact = train_artifact(
        pd.DataFrame({"Pressure": rng.normal(10.0, 0.5, 300)}),
        channels=["Pressure"],
        window_size=3,
        lag_steps=[1],
        fft_enabled=False,
        percentile=99.0,
    )
    return Team3ZScoreDetector(artifact)


class TestNormalPrefix:
    def test_prefix_stops_at_the_first_incident(self) -> None:
        events = [event(i) for i in range(10)]
        truth = truth_of([i >= 6 for i in range(10)])
        assert [e.timestamp for e in normal_prefix(events, truth)] == [at(i) for i in range(6)]

    def test_a_clean_recording_is_all_prefix(self) -> None:
        events = [event(i) for i in range(5)]
        assert len(normal_prefix(events, truth_of([False] * 5))) == 5

    def test_a_recording_opening_mid_fault_has_no_prefix(self) -> None:
        events = [event(i) for i in range(5)]
        assert normal_prefix(events, truth_of([True] * 5)) == []


class TestOptionality:
    def test_unfittable_detectors_are_used_as_configured(self) -> None:
        for detector in (StubThresholdDetector(threshold=80.0), LogBurstDetector()):
            assert not isinstance(detector, Fittable)
            outcome = prepare_detector(
                detector,
                protocol=FitProtocol.PREFIX,
                events=[event(i) for i in range(5)],
                truth=truth_of([False] * 5),
            )
            assert outcome.detector is detector
            assert outcome.fitted is False
            assert outcome.note is not None and "not fittable" in outcome.note

    def test_protocol_none_never_fits_even_a_fittable_detector(self) -> None:
        detector = fittable_detector()
        outcome = prepare_detector(
            detector,
            protocol=FitProtocol.NONE,
            events=[event(i) for i in range(50)],
            truth=truth_of([False] * 50),
        )
        assert outcome.detector is detector and outcome.fitted is False
        assert outcome.note is None  # not fitting was the instruction, not a fallback

    def test_no_clean_prefix_degrades_with_a_note(self) -> None:
        outcome = prepare_detector(
            fittable_detector(),
            protocol=FitProtocol.PREFIX,
            events=[event(i) for i in range(5)],
            truth=truth_of([True] * 5),
        )
        assert outcome.fitted is False
        assert outcome.note is not None and "no clean prefix" in outcome.note

    def test_a_refused_fit_degrades_with_the_reason(self) -> None:
        # team3 refuses several assets at once: rolling/lag features assume one asset's
        # contiguous history. That is a statement about the data, not a crash.
        events = [event(i, asset="a") for i in range(30)] + [event(i, asset="b") for i in range(30)]
        outcome = prepare_detector(
            fittable_detector(),
            protocol=FitProtocol.PREFIX,
            events=events,
            truth=truth_of([False] * 30),
        )
        assert outcome.fitted is False
        assert outcome.note is not None and "fit refused" in outcome.note

    def test_holdout_without_data_degrades(self) -> None:
        outcome = prepare_detector(
            fittable_detector(),
            protocol=FitProtocol.HOLDOUT,
            events=[event(i) for i in range(50)],
            truth=truth_of([False] * 50),
            holdout_events=None,
        )
        assert outcome.fitted is False
        assert outcome.note is not None and "no holdout" in outcome.note


class TestFitting:
    def test_a_successful_fit_returns_a_new_detector(self) -> None:
        original = fittable_detector()
        events = [event(i, value=10.0 + (i % 3) * 0.1) for i in range(80)]
        outcome = prepare_detector(
            original,
            protocol=FitProtocol.PREFIX,
            events=events,
            truth=truth_of([False] * 80),
        )
        assert outcome.fitted is True
        # A NEW object: the port's determinism law forbids hidden fitted state, and one
        # case's fit must never leak into the next.
        assert outcome.detector is not original

    def test_fitting_leaves_the_original_untouched(self) -> None:
        original = fittable_detector()
        before = original._artifact.threshold
        events = [event(i, value=10.0 + (i % 5) * 2.0) for i in range(80)]
        prepare_detector(
            original, protocol=FitProtocol.PREFIX, events=events, truth=truth_of([False] * 80)
        )
        assert original._artifact.threshold == before
