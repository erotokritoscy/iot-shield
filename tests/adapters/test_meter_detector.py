"""Meter detector specifics: the threshold, the profile, attribution, and the artifact.

The port suite covers the laws every detector obeys and the ADR-0022 hyperparameter law.
This covers what is true only of *this* detector — and, in particular, pins the three
defects found in the vendored upstream code so a re-sync cannot quietly reintroduce them.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from iotshield.contracts import EventKind
from iotshield.detect._fitted import NotFitted, is_fitted
from iotshield.detect.meter import MeterIForestDetector, MeterParams
from iotshield.detect.meter._attribution import MAX_ABS_Z, attribute_row, robust_stats, summarise
from iotshield.detect.meter._features import model_columns, regrid
from iotshield.detect.meter._iforest import threshold_at_budget
from iotshield.detect.meter.artifact import (
    MeterArtifact,
    MeterState,
    load_artifact,
    parse_artifact,
)
from iotshield.normalize import MeterNormalizer
from iotshield.pipeline.registry import build_detector
from tests import meter_fixtures

_DEMO_DIR = Path(__file__).resolve().parents[2] / "data" / "demo"

# ---------------------------------------------------------------- the detector


def test_a_leak_fires_with_evidence_drawn_from_the_model_columns() -> None:
    detector = meter_fixtures.make_detector()
    detections = detector.detect(meter_fixtures.leaking_events())
    assert detections, "the injected overnight leak should fire"
    top = max(detections, key=lambda d: d.score)
    assert top.score > top.threshold
    assert top.evidence["explanation"]
    # Which feature dominates depends on the window (the fixtures use a short one), so what
    # is pinned here is that the attribution is real: named model columns, past the
    # reporting floor. The demo artifact's 168-hour window is where `deviation` leads, and
    # that is asserted separately below.
    columns = set(model_columns(detector.params.profile_window_hours))
    reported = {item["feature"] for item in top.evidence["top_features"]}
    assert reported
    assert reported <= columns
    assert all(abs(float(item["z"])) >= 2.0 for item in top.evidence["top_features"])
    assert top.evidence["num_anomalous_points"] >= 1


def test_the_demo_meter_leak_is_led_by_the_hour_of_week_residual() -> None:
    """At the production 168-hour window, the residual is what carries this detector: the
    leak's volume is ordinary for an evening and extraordinary for 3am, which is the case a
    magnitude threshold cannot express."""
    detector = build_detector("meter_iforest", {"artifact": "demo_meter"})
    text = (_DEMO_DIR / "demo_meter.csv").read_text(encoding="utf-8")
    result = MeterNormalizer().normalize(text, source_format="eoa-meter-csv", origin="test://demo")
    detections = detector.detect(result.events)
    assert len(detections) == 1, "the demo is tuned to surface exactly the leak"
    leak = detections[0]
    assert leak.event_kind is EventKind.COLLECTIVE
    assert leak.evidence["top_features"][0]["feature"] == "deviation"
    assert "usual level for this hour" in leak.evidence["explanation"]


def test_an_asset_with_no_fitted_model_is_skipped_not_scored() -> None:
    """Scoring a meter against another meter's normal measures the difference between two
    households, not an anomaly — the same discipline as team3's channel-coverage guard."""
    detector = meter_fixtures.make_detector()
    foreign = [
        event.model_copy(update={"asset_id": "some-other-meter"})
        for event in meter_fixtures.leaking_events()
    ]
    assert detector.detect(foreign) == []


def test_fit_produces_one_model_per_meter() -> None:
    """Unlike team3's fit (which refuses >1 asset), this one fits a fleet meter by meter."""
    first = meter_fixtures.clean_events()
    second = [
        event.model_copy(update={"asset_id": "meter-02"})
        for event in meter_fixtures.clean_events(seed=99)
    ]
    fitted = MeterIForestDetector(meter_fixtures.make_params()).fit([*first, *second])
    assert fitted.fitted_assets == ("meter-01", "meter-02")


def test_fit_refuses_when_no_meter_has_enough_clean_history() -> None:
    short = meter_fixtures.meter_events(48)
    with pytest.raises(ValueError, match="enough clean history"):
        MeterIForestDetector(meter_fixtures.make_params()).fit(short)


def test_fit_refuses_input_with_no_meter_events() -> None:
    with pytest.raises(ValueError, match="meter payload"):
        MeterIForestDetector(meter_fixtures.make_params()).fit([])


def test_the_fitted_profile_is_used_not_one_rebuilt_from_the_judged_data() -> None:
    """If the profile were rebuilt from the data under judgment, a long anomaly would raise
    its own bucket's median and hide itself. Fitting on clean history is what prevents that.
    """
    detector = meter_fixtures.make_detector()
    fitted_profile = detector.model_for("meter-01").profile.copy()
    # A leak-heavy window would produce a visibly different profile if rebuilt from it.
    detections = detector.detect(meter_fixtures.leaking_events())
    assert detections
    # The profile the detector holds is unchanged by having judged anything.
    assert np.allclose(detector.model_for("meter-01").profile, fitted_profile, equal_nan=True)


def test_untrained_refuses_to_judge() -> None:
    untrained = build_detector("meter_iforest", dict(meter_fixtures.FIXTURE_PARAMS))
    assert is_fitted(untrained) is False
    with pytest.raises(NotFitted):
        untrained.detect(meter_fixtures.leaking_events())


def test_an_artifact_and_a_hyperparameter_override_together_are_refused() -> None:
    """An artifact is atomic: its fitted state is aligned to its own hyperparameters."""
    with pytest.raises(ValueError, match="artifact"):
        build_detector("meter_iforest", {"artifact": "demo_meter", "n_estimators": 42})


# ------------------------------------------------------------- the threshold


def test_a_budget_the_history_cannot_support_is_refused() -> None:
    """The failure that looks like success: a quantile past the largest observed score is
    an interpolation artifact, not a measurement."""
    scores = np.linspace(0.0, 1.0, 100)
    with pytest.raises(ValueError, match="cannot be estimated from"):
        threshold_at_budget(scores, 0.0014)  # asks for the top 0.14 of 100 rows


def test_a_supported_budget_lands_between_the_observed_scores() -> None:
    scores = np.linspace(0.0, 1.0, 1000)
    threshold = threshold_at_budget(scores, 0.01)
    assert 0.98 < threshold < 1.0


def test_a_looser_budget_gives_a_lower_threshold() -> None:
    scores = np.linspace(0.0, 1.0, 1000)
    assert threshold_at_budget(scores, 0.10) < threshold_at_budget(scores, 0.01)


def test_min_train_hours_below_the_window_is_a_config_error() -> None:
    with pytest.raises(ValueError, match="cannot fill the rolling window"):
        MeterParams(profile_window_hours=336, min_train_hours=168)


# ----------------------------------------------- vendored defects, pinned here


def test_a_collapsed_mad_falls_back_to_the_spread_not_a_tiny_floor() -> None:
    """Upstream defect 1. ``deviation`` is exactly zero through every idle hour, so its MAD
    collapses — but to ~1e-14 of float residue, not to 0. Upstream tests ``mad > 0``, keeps
    the residue, and floors at 1e-6, turning a 0.055 deviation into z = 55,000 (measured on
    the demo meter). The scale must fall back to the column's actual spread instead.
    """
    # 70% of rows identical -> the median absolute deviation is float residue, not zero.
    column = np.array([0.3 - 0.3] * 70 + [0.002, -0.002, 0.004, -0.004] * 7 + [0.01] * 2)
    matrix = column.reshape(-1, 1)
    _median, scale = robust_stats(matrix)
    assert scale[0] > 1e-4, "a collapsed MAD must not leave a 1e-6-scale denominator"
    assert scale[0] == pytest.approx(column.std(), rel=1e-6)


def test_reported_z_is_clamped_so_evidence_stays_meaningful() -> None:
    matrix = np.zeros((50, 1))
    median, scale = robust_stats(matrix)
    top = attribute_row(np.array([1.0]), ["consumption"], median, scale, top_k=1)
    assert top and abs(float(top[0]["z"])) <= MAX_ABS_Z


def test_regrid_restores_gap_hours_so_a_window_means_wall_clock() -> None:
    """Upstream defect 3 (a divergence, not a bug): rolling on surviving rows only would let
    a 168-row window reach back weeks when a meter has gaps."""
    events = meter_fixtures.meter_events(10)
    stamps = [event.timestamp for event in events]
    series = pd.Series([1.0] * 8, index=pd.DatetimeIndex(stamps[:4] + stamps[6:], name="timestamp"))
    gridded = regrid(series, 1.0)
    assert len(gridded) == 10
    assert int(gridded.isna().sum()) == 2


# ----------------------------------------------------------------- the artifact


def test_the_committed_artifact_loads_and_covers_the_demo_meter() -> None:
    artifact = load_artifact("demo_meter")
    assert artifact.params.fp_budget > 0
    assert [state.asset_id for state in artifact.meters] == ["202405109001"]
    fitted = artifact.fitted["202405109001"]
    assert fitted.threshold > 0
    assert list(fitted.feature_columns) == list(model_columns(artifact.params.profile_window_hours))


def test_loading_an_artifact_reproduces_the_fit_it_was_written_from() -> None:
    """The format stores the training rows and re-fits, so this round-trip is what makes the
    stored threshold and the stored model impossible to get out of step."""
    detector = meter_fixtures.make_detector()
    state = detector.state_for("meter-01")
    artifact = MeterArtifact(params=meter_fixtures.make_params(), meters=[state])
    rebuilt = MeterArtifact.model_validate(artifact.model_dump())
    assert rebuilt.fitted["meter-01"].threshold == pytest.approx(
        detector.model_for("meter-01").threshold
    )


def test_an_unknown_artifact_id_is_a_config_error() -> None:
    with pytest.raises(ValueError, match="unknown meter artifact id"):
        load_artifact("no-such-artifact")


def test_an_old_schema_version_says_regenerate() -> None:
    with pytest.raises(ValueError, match="regenerate"):
        parse_artifact('{"schema_version": 0}', "stale")


def test_a_ragged_training_matrix_is_refused() -> None:
    with pytest.raises(ValueError, match="match feature_columns"):
        MeterState(
            asset_id="m",
            provenance="test",
            feature_columns=["a", "b"],
            profile=[[0.0] * 7 for _ in range(24)],
            training_rows=[[1.0, 2.0], [3.0]],
        )


def test_summarise_says_so_when_nothing_stood_out() -> None:
    assert "combination" in summarise([])


# ------------------------------------------------------------------- helpers
