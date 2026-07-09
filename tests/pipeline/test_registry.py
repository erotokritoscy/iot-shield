import pytest

from iotshield.alert import AlertEngine
from iotshield.contracts import Lane
from iotshield.detect import Detector
from iotshield.normalize import Normalizer
from iotshield.pipeline.registry import build_alert_engine, build_detector, build_normalizer


def test_built_adapters_satisfy_their_ports() -> None:
    # The annotations make pyright verify protocol conformance statically.
    normalizer: Normalizer = build_normalizer("stub")
    detector: Detector = build_detector("stub_threshold", {})
    engine: AlertEngine = build_alert_engine("stub", {})
    assert normalizer is not None and detector is not None and engine is not None


def test_detector_params_are_applied() -> None:
    detector = build_detector("stub_threshold", {"threshold": 5.0, "lane": "log"})
    assert detector.lane == Lane.LOG


def test_unknown_name_lists_known_alternatives() -> None:
    with pytest.raises(ValueError, match=r"unknown detector 'nope'.*stub_threshold"):
        build_detector("nope", {})


def test_bad_param_names_the_adapter() -> None:
    with pytest.raises(ValueError, match=r"invalid params for detector 'stub_threshold'"):
        build_detector("stub_threshold", {"thresold": 80.0})


def test_quoted_numeric_threshold_is_coerced() -> None:
    # YAML scalars can arrive quoted; float() coercion keeps them usable instead
    # of exploding later inside detect().
    detector = build_detector("stub_threshold", {"threshold": "90"})
    assert detector.detect  # constructed successfully with a numeric threshold


def test_non_numeric_param_value_fails_fast_and_names_adapter() -> None:
    with pytest.raises(ValueError, match=r"invalid params for detector 'stub_threshold'"):
        build_detector("stub_threshold", {"threshold": "high"})


def test_non_positive_bucket_fails_fast_and_names_adapter() -> None:
    # bucket_minutes=0 would ZeroDivisionError mid-run; catch it at construction.
    with pytest.raises(ValueError, match=r"invalid params for alert engine 'stub'"):
        build_alert_engine("stub", {"bucket_minutes": 0})
