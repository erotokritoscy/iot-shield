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


def test_unsupported_source_format_is_an_error() -> None:
    issues = check_compatibility(cfg(source_format="sislog"))
    assert [i.level for i in issues] == [CompatLevel.ERROR]
    assert issues[0].stage == "ingest"
    assert "sislog" in issues[0].message and "stub-csv" in issues[0].message


def test_lane_mismatch_is_a_warning_not_an_error() -> None:
    # syslog produces log; the telemetry detector idles -> warn, don't block
    # (the syslog demo depends on this being allowed).
    issues = check_compatibility(cfg(source_format="syslog", normalizer="syslog"))
    assert [i.level for i in issues] == [CompatLevel.WARNING]
    assert "will not fire" in issues[0].message


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
    assert [i.level for i in issues] == [CompatLevel.WARNING]
