"""LOF's own behaviour; the port suite holds its contract and the ADR-0022 laws."""

import numpy as np
import pytest

from iotshield.detect._fitted import NotFitted
from iotshield.detect.meter.detector import MeterIForestDetector
from iotshield.detect.meter.lof import (
    MeterLofDetector,
    available_lof_artifacts,
    load_lof_artifact,
)
from iotshield.detect.meter.params import LofParams
from iotshield.meter import SIGNATURE_KEY, Signature
from iotshield.pipeline.registry import DETECTORS
from tests import meter_fixtures as fx


def fitted(**overrides: object) -> MeterLofDetector:
    params = LofParams.model_validate({**fx.LOF_FIXTURE_PARAMS, **overrides})
    return MeterLofDetector(params).fit(fx.clean_events())


def test_untrained_it_refuses_to_judge() -> None:
    detector = MeterLofDetector(LofParams.model_validate(fx.LOF_FIXTURE_PARAMS))
    with pytest.raises(NotFitted, match="meter_lof"):
        detector.detect(fx.clean_events())


def test_it_finds_the_overnight_leak() -> None:
    assert fitted().detect(fx.leaking_events())


def test_its_threshold_is_not_taken_from_self_neighbouring_scores() -> None:
    """The bias this adapter exists to avoid (module docstring).

    Passed back through ``score_samples``, a training row finds itself at distance zero and looks
    denser than it is. A threshold from those scores would sit too low.
    """
    model = fitted(fp_budget=0.02).model_for(fx.METER_ID)
    self_neighbouring = np.quantile(model.score(model.training_rows), 0.98)
    assert model.threshold > self_neighbouring


def test_unseen_clean_hours_stay_near_the_budget() -> None:
    detector = fitted(fp_budget=0.02)
    model = detector.model_for(fx.METER_ID)
    unseen = fx.meter_events(24 * 21, seed=99)
    from iotshield.detect.meter._features import build_features, feature_matrix
    from iotshield.detect.meter.detector import consumption_series

    series, interval = consumption_series(unseen)
    rows, _ = feature_matrix(
        build_features(series, model.profile, window=24, interval_hours=interval),
        model.feature_columns,
    )
    rate = float((model.score(rows) > model.threshold).mean())
    assert rate < 0.10  # a generous multiple of 0.02: the biased threshold is far above this


def test_a_meter_it_never_saw_is_skipped_and_explained() -> None:
    detector = fitted()
    other = [e.model_copy(update={"asset_id": "other"}) for e in fx.leaking_events()]
    assert detector.detect(other) == []
    assert "other" in (detector.why_silent(other) or "")


def test_detections_name_their_signature() -> None:
    for detection in fitted().detect(fx.leaking_events()):
        assert detection.method == "meter_lof"
        assert Signature(detection.evidence[SIGNATURE_KEY])


def test_the_forest_names_its_signature_too() -> None:
    for detection in fx.make_detector().detect(fx.leaking_events()):
        assert Signature(detection.evidence[SIGNATURE_KEY])
    assert isinstance(fx.make_detector(), MeterIForestDetector)


def test_the_committed_artifact_loads_and_is_published() -> None:
    assert "demo_meter" in available_lof_artifacts()
    assert load_lof_artifact("demo_meter").fitted


def test_an_artifact_is_atomic() -> None:
    with pytest.raises(ValueError):
        DETECTORS["meter_lof"].factory(artifact="demo_meter", n_neighbors=5)


def test_an_unknown_metric_is_refused() -> None:
    with pytest.raises(ValueError):
        LofParams(metric="cosine")  # type: ignore[arg-type]
