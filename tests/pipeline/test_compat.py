"""Compatibility guard: coherent chains pass, broken ones ERROR, idle ones WARN."""

from pathlib import Path
from typing import Any

import pytest

from iotshield.pipeline import PipelineConfig, load_config
from iotshield.pipeline.compat import CompatLevel, check_compatibility

CONFIG_DIR = Path(__file__).parents[2] / "config"


def cfg(**overrides: Any) -> PipelineConfig:
    base: dict[str, Any] = {
        "database_url": "sqlite:///x.db",
        "source_format": "stub-csv",
        "normalizer": "stub",
        "detectors": [{"name": "stub_threshold", "params": {"lane": "telemetry"}}],
        "alert_engine": {"name": "stub"},
    }
    return PipelineConfig.model_validate(base | overrides)


def levels(config: PipelineConfig) -> list[CompatLevel]:
    return [issue.level for issue in check_compatibility(config)]


def test_matching_telemetry_chain_has_no_issues() -> None:
    assert check_compatibility(cfg()) == []


def test_identically_configured_detector_twice_warns_about_double_counting() -> None:
    """Two identical entries judge the same events twice, and the alert engine's `volume`
    factor reads the repeats as independent corroboration (ADR-0020 D4)."""
    entry = {"name": "stub_threshold", "params": {"lane": "telemetry"}}
    issues = check_compatibility(cfg(detectors=[entry, dict(entry)]))
    assert [i.level for i in issues] == [CompatLevel.WARNING]
    assert issues[0].stage == "detectors[1]"
    assert "detectors[0]" in issues[0].message and "double-counted" in issues[0].message


def test_duplicate_detection_ignores_the_order_params_were_written_in() -> None:
    entries = [
        {"name": "stub_threshold", "params": {"lane": "telemetry", "threshold": 80.0}},
        {"name": "stub_threshold", "params": {"threshold": 80.0, "lane": "telemetry"}},
    ]
    assert levels(cfg(detectors=entries)) == [CompatLevel.WARNING]


def test_the_same_detector_with_different_params_is_not_a_duplicate() -> None:
    # The legitimate case the list form exists to enable: one adapter, two configurations.
    entries = [
        {"name": "log_burst", "params": {"preset": "severity_spike"}},
        {"name": "log_burst", "params": {"preset": "auth_failure"}},
    ]
    issues = check_compatibility(
        cfg(source_format="syslog", normalizer="syslog", detectors=entries)
    )
    assert not [i for i in issues if "double-counted" in i.message]


def test_unsupported_source_format_is_an_error() -> None:
    issues = check_compatibility(cfg(source_format="sislog"))
    assert [i.level for i in issues] == [CompatLevel.ERROR]
    assert issues[0].stage == "sources[0]"
    assert "sislog" in issues[0].message and "stub-csv" in issues[0].message


def test_lane_mismatch_warns_about_both_idle_detector_and_uncovered_lane() -> None:
    # syslog produces log; the telemetry detector idles AND the log lane has no detector.
    # Both are true and both are surfaced (WARNING), never blocking (the syslog demo relies
    # on this being allowed).
    issues = check_compatibility(cfg(source_format="syslog", normalizer="syslog"))
    assert [i.level for i in issues] == [CompatLevel.WARNING, CompatLevel.WARNING]
    messages = " ".join(i.message for i in issues)
    assert "will not fire" in messages  # the idle telemetry detector
    assert "no detector that consumes it" in messages  # the uncovered log lane


def test_unknown_normalizer_is_an_error() -> None:
    issues = check_compatibility(cfg(normalizer="nope"))
    assert [i.level for i in issues] == [CompatLevel.ERROR]
    assert "nope" in issues[0].message


def test_unknown_detector_is_an_error() -> None:
    issues = check_compatibility(cfg(detectors=[{"name": "nope"}]))
    assert any(i.level is CompatLevel.ERROR and "nope" in i.message for i in issues)


def test_bad_detector_param_value_is_an_error() -> None:
    issues = check_compatibility(
        cfg(detectors=[{"name": "stub_threshold", "params": {"lane": "bogus"}}])
    )
    assert any(i.level is CompatLevel.ERROR for i in issues)


def test_unknown_alert_engine_is_an_error() -> None:
    issues = check_compatibility(cfg(alert_engine={"name": "nope"}))
    assert any(i.level is CompatLevel.ERROR and "nope" in i.message for i in issues)


@pytest.mark.parametrize("path", sorted(CONFIG_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_shipped_demo_configs_are_error_free(path: Path) -> None:
    # Every committed demo must at least run: none may contain an ERROR-level issue
    # (the syslog demo is allowed its idle-detector WARNING).
    errors = [i for i in check_compatibility(load_config(path)) if i.level is CompatLevel.ERROR]
    assert errors == [], f"{path.name} has compatibility errors: {errors}"


def test_syslog_demo_warns_about_its_idle_detector() -> None:
    issues = check_compatibility(load_config(CONFIG_DIR / "demo_syslog.yaml"))
    assert issues and all(i.level is CompatLevel.WARNING for i in issues)
    assert any("will not fire" in i.message for i in issues)


class TestMultiSource:
    def cfg_ms(self, sources: list[dict[str, str]], **overrides: Any) -> PipelineConfig:
        base: dict[str, Any] = {
            "database_url": "sqlite:///x.db",
            "sources": sources,
            "detectors": [{"name": "stub_threshold", "params": {"lane": "telemetry"}}],
            "alert_engine": {"name": "stub"},
        }
        return PipelineConfig.model_validate(base | overrides)

    def test_mixed_modality_chain_only_warns_about_the_uncovered_log_lane(self) -> None:
        # A telemetry source + a syslog source, with only a telemetry detector: the ingest
        # joints are both fine, the telemetry detector is covered, and the log lane (which no
        # detector consumes) is the single warning.
        config = self.cfg_ms(
            [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {"normalizer": "syslog", "source_format": "syslog"},
            ]
        )
        issues = check_compatibility(config)
        assert [i.level for i in issues] == [CompatLevel.WARNING]
        assert "log" in issues[0].message and "no detector that consumes it" in issues[0].message

    def test_a_second_sources_bad_format_is_reported_against_that_source(self) -> None:
        config = self.cfg_ms(
            [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {"normalizer": "syslog", "source_format": "sisslog"},
            ]
        )
        errors = [i for i in check_compatibility(config) if i.level is CompatLevel.ERROR]
        assert [e.stage for e in errors] == ["sources[1]"]
        assert "sisslog" in errors[0].message

    def test_union_of_source_lanes_keeps_a_detector_from_idling(self) -> None:
        # Two sources, one telemetry + one log; a log-lane detector no longer idles because
        # a source now produces the log lane (the union across sources feeds the joint).
        config = self.cfg_ms(
            [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {"normalizer": "syslog", "source_format": "syslog"},
            ],
            detectors=[
                {"name": "stub_threshold", "params": {"lane": "telemetry"}},
                {"name": "stub_threshold", "params": {"lane": "log"}},
            ],
        )
        # Both produced lanes are consumed and both detectors fire: a fully clean chain.
        assert check_compatibility(config) == []
