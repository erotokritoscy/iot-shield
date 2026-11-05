"""Fits made at deployment: named, listed, loaded, and never reached by path (ADR-0048 D3)."""

import json
from pathlib import Path

import pytest

from iotshield.detect.meter import train
from iotshield.detect.meter.artifact import (
    MODELS_PATH_ENV,
    available_artifacts,
    load_artifact,
    shelved,
)
from iotshield.detect.meter.detector import MeterIForestDetector
from iotshield.detect.meter.lof import available_lof_artifacts, load_lof_artifact
from iotshield.detect.meter.params import LOF_METHOD, METHOD
from iotshield.pipeline.registry import DETECTORS

DEMO_HISTORY = ["--source", "data/demo/demo_meter_history.csv"]


@pytest.fixture
def shelf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(MODELS_PATH_ENV, str(tmp_path))
    return tmp_path


def fit(name: str = "fleet", *extra: str) -> None:
    train.main(["--name", name, *DEMO_HISTORY, *extra])


def test_an_empty_shelf_leaves_only_what_ships(shelf: Path) -> None:
    assert available_artifacts() == ("demo_meter",)
    assert shelved(METHOD) == {}


def test_a_named_fit_is_offered_and_loads(shelf: Path) -> None:
    fit()
    assert (shelf / METHOD / "fleet.json").is_file()
    assert "fleet" in available_artifacts()
    assert load_artifact("fleet").fitted


def test_the_registry_builds_a_ready_detector_from_it(shelf: Path) -> None:
    fit()
    detector = DETECTORS["meter_iforest"].factory(artifact="fleet")
    assert isinstance(detector, MeterIForestDetector)
    assert detector.is_fitted
    assert detector.fitted_assets == ("202405109001",)


def test_the_catalogue_offers_it_without_a_restart(shelf: Path) -> None:
    spec = DETECTORS["meter_iforest"]

    def offered() -> list[str]:
        return list(spec.discovered["artifact"]())

    assert "fleet" not in offered()
    fit()
    assert "fleet" in offered()


def test_a_refit_replaces_the_cached_one(shelf: Path) -> None:
    fit()
    before = load_artifact("fleet").params.fp_budget
    path = shelf / METHOD / "fleet.json"
    raw = json.loads(path.read_text())
    raw["params"]["fp_budget"] = 0.002
    path.write_text(json.dumps(raw))
    assert load_artifact("fleet").params.fp_budget == 0.002 != before


@pytest.mark.parametrize(
    "hostile", ["../secret", "/etc/passwd", "fleet.json", "..", "", "fleet/../fleet", "nope"]
)
def test_an_id_is_membership_never_a_path(shelf: Path, hostile: str) -> None:
    fit()
    (shelf / "secret.json").write_text("{}")
    with pytest.raises(ValueError, match="unknown meter artifact id"):
        load_artifact(hostile)


@pytest.mark.parametrize("bad", ["../up", "a/b", ".hidden", ""])
def test_a_name_that_could_not_be_read_back_is_refused(shelf: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="plain identifier"):
        train._shelf_path(METHOD, bad)


def test_a_fit_cannot_shadow_a_shipped_artifact(shelf: Path) -> None:
    with pytest.raises(ValueError, match="shadow"):
        fit("demo_meter")


def test_a_corrupt_file_is_refused_when_named(shelf: Path) -> None:
    fit()
    (shelf / METHOD / "fleet.json").write_text('{"schema_version": 99}')
    with pytest.raises(ValueError, match="schema_version"):
        load_artifact("fleet")


def test_until_keeps_a_fit_out_of_what_it_will_judge(shelf: Path) -> None:
    fit("all")
    fit("early", "--until", "2026-05-01")
    whole = len(load_artifact("all").meters[0].training_rows)
    part = len(load_artifact("early").meters[0].training_rows)
    assert part < whole


def test_a_window_left_with_nothing_says_so(shelf: Path) -> None:
    with pytest.raises(ValueError, match="no consumption events"):
        fit("none", "--until", "2000-01-01")


def test_a_preset_supplies_upstreams_calibrated_settings(shelf: Path) -> None:
    fit("calibrated", "--preset", "wbn_fs")
    params = load_artifact("calibrated").params
    assert (params.n_estimators, params.max_samples) == (400, 512)


def test_an_unknown_preset_lists_the_known_ones(shelf: Path) -> None:
    with pytest.raises(ValueError, match="wbn_fs"):
        fit("x", "--preset", "sensipro_water")


def test_lof_has_its_own_shelf_and_presets(shelf: Path) -> None:
    train.main(["--detector", LOF_METHOD, "--name", "fleet", "--preset", "wbn_fs", *DEMO_HISTORY])
    assert (shelf / LOF_METHOD / "fleet.json").is_file()
    assert "fleet" in available_lof_artifacts()
    assert "fleet" not in available_artifacts()
    assert load_lof_artifact("fleet").params.metric == "manhattan"


def test_n_estimators_is_refused_for_lof(shelf: Path) -> None:
    with pytest.raises(ValueError, match="only to meter_iforest"):
        train.main(["--detector", LOF_METHOD, "--n-estimators", "50", *DEMO_HISTORY])


def test_name_and_out_together_are_refused(shelf: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        train.main(["--name", "x", "--out", str(tmp_path / "x.json"), *DEMO_HISTORY])
