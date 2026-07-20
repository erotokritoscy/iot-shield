import pytest

from iotshield.alert import AlertEngine
from iotshield.contracts import Lane
from iotshield.detect import Detector
from iotshield.detect.team3.detector import Team3ZScoreDetector
from iotshield.detect.team3.params import Team3Params
from iotshield.normalize import Normalizer
from iotshield.pipeline.registry import (
    DETECTORS,
    build_alert_engine,
    build_detector,
    build_normalizer,
    build_sink,
    detector_param_names,
    sink_requires_database,
)
from iotshield.sink import ResultSink, SinkResources


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


def test_team3_detector_builds_from_committed_artifact() -> None:
    detector = build_detector("team3_zscore", {})
    assert detector.lane == Lane.TELEMETRY
    assert detector.method == "team3_zscore"


def test_team3_unknown_artifact_fails_fast_and_names_adapter() -> None:
    # A bad artifact id is a config error; it must fail at construction, named.
    with pytest.raises(
        ValueError, match=r"invalid params for detector 'team3_zscore'.*unknown team3 artifact"
    ):
        build_detector("team3_zscore", {"artifact": "nope"})


def test_log_burst_detector_builds_with_its_preset() -> None:
    detector = build_detector("log_burst", {"preset": "auth_failure"})
    assert detector.lane == Lane.LOG
    # The preset rides in the method, so two configured bursts stay distinguishable.
    assert detector.method == "log_burst:auth_failure"


def test_log_burst_unknown_preset_fails_fast_and_names_adapter() -> None:
    with pytest.raises(
        ValueError, match=r"invalid params for detector 'log_burst'.*unknown log_burst preset"
    ):
        build_detector("log_burst", {"preset": "nope"})


def test_log_burst_quoted_numeric_params_are_coerced() -> None:
    # YAML scalars can arrive quoted; the factory coerces them as _stub_threshold does.
    detector = build_detector("log_burst", {"window_minutes": "2", "min_count": "5"})
    assert detector.method == "log_burst:severity_spike"  # the format-agnostic default


def test_build_file_sink_satisfies_the_port() -> None:
    sink: ResultSink = build_sink("file", {"path": "out.jsonl"}, SinkResources())
    assert sink is not None


def test_unknown_sink_lists_known_alternatives() -> None:
    with pytest.raises(ValueError, match=r"unknown sink 'nope'.*postgres"):
        build_sink("nope", {}, SinkResources())


def test_postgres_sink_without_a_database_fails_and_names_it() -> None:
    # The postgres sink needs an injected session factory; without one it fails, named.
    with pytest.raises(
        ValueError, match=r"invalid params for sink 'postgres'.*requires a database"
    ):
        build_sink("postgres", {}, SinkResources())


def test_sink_requires_database_flags_only_db_sinks() -> None:
    assert sink_requires_database("postgres") is True
    assert sink_requires_database("file") is False


def build_team3(params: dict) -> Team3ZScoreDetector:
    """Build through the registry, narrowed — these assertions are about the adapter."""
    detector = build_detector("team3_zscore", params)
    assert isinstance(detector, Team3ZScoreDetector)
    return detector


class TestHyperparameterSurface:
    """ADR-0022: the factory signature IS the adapter's tunable set, and it is reachable."""

    def test_every_team3_hyperparameter_is_settable_from_config(self) -> None:
        # The defect the ADR fixed: window/lags/fft/percentile/channels lived inside the
        # artifact file, so no config could reach them and no benchmark could sweep them.
        declared = set(Team3Params.model_fields)
        assert declared <= detector_param_names("team3_zscore")

    def test_an_artifact_supplies_both_weights_and_its_hyperparameters(self) -> None:
        detector = build_team3({"artifact": "skab_zscore"})
        assert detector.is_fitted is True
        assert detector._params.window_size == 60

    def test_no_artifact_yields_an_untrained_detector(self) -> None:
        detector = build_team3({"window_size": 20, "lag_steps": [1, 3]})
        assert detector.is_fitted is False
        assert detector._params.window_size == 20

    def test_overriding_an_artifacts_hyperparameter_is_refused_with_a_copyable_block(
        self,
    ) -> None:
        # Refused because an artifact is atomic: its baseline is aligned to the features
        # its own window produced. The message prints the settings so switching to a
        # from-scratch fit is an edit, not a hunt through JSON.
        with pytest.raises(ValueError) as caught:
            build_detector("team3_zscore", {"artifact": "skab_zscore", "window_size": 20})
        message = str(caught.value)
        assert "cannot override ['window_size']" in message
        assert "window_size: 60" in message and "lag_steps: [1, 5, 10]" in message

    def test_a_matching_value_is_still_an_override(self) -> None:
        # Value equality is deliberately NOT the test: "not given" is the only way to
        # inherit, so the rule stays one sentence and never depends on what the file holds.
        with pytest.raises(ValueError, match="cannot override"):
            build_detector("team3_zscore", {"artifact": "skab_zscore", "window_size": 60})

    def test_lag_steps_accept_yaml_lists_and_ui_text(self) -> None:
        for value in ([1, 3], "1,3", "[1, 3]", "(1, 3)"):
            assert build_team3({"window_size": 20, "lag_steps": value})._params.lag_steps == (1, 3)

    def test_a_bad_hyperparameter_fails_at_construction_named(self) -> None:
        with pytest.raises(ValueError, match=r"invalid params for detector 'team3_zscore'"):
            build_detector("team3_zscore", {"window_size": 5, "lag_steps": [99]})

    def test_rule_detectors_declare_no_learning_metadata(self) -> None:
        # Both optional fields default to "not a learning adapter", so a rule-based
        # detector says nothing and stays a first-class Detector.
        for name in ("stub_threshold", "log_burst"):
            assert DETECTORS[name].params_type is None
            assert DETECTORS[name].artifact_param is None
        assert DETECTORS["team3_zscore"].artifact_param == "artifact"
