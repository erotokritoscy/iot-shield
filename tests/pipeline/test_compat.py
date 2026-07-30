"""Compatibility guard: coherent chains pass, broken ones ERROR, idle ones WARN."""

from pathlib import Path
from typing import Any

import pytest

from iotshield.pipeline import PipelineConfig, load_spec
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
    assert "detectors[0]" in issues[0].message and "report every finding twice" in issues[0].message


def test_duplicate_detection_ignores_the_order_params_were_written_in() -> None:
    entries = [
        {"name": "stub_threshold", "params": {"lane": "telemetry", "threshold": 80.0}},
        {"name": "stub_threshold", "params": {"threshold": 80.0, "lane": "telemetry"}},
    ]
    assert levels(cfg(detectors=entries)) == [CompatLevel.WARNING]


def test_duplicate_detection_resolves_defaults_so_omitted_equals_explicit() -> None:
    # `{}` and `{preset: fan_out}` are the same detector, since fan_out is the default. The
    # guard compares *effective* params, so it catches this — not only textual identity.
    entries = [
        {"name": "flow_burst", "params": {}},
        {"name": "flow_burst", "params": {"preset": "fan_out"}},
    ]
    issues = check_compatibility(
        cfg(source_format="flow-csv", normalizer="flow", detectors=entries)
    )
    assert [i.level for i in issues if "report every finding twice" in i.message] == [
        CompatLevel.WARNING
    ]


def test_the_same_detector_with_different_params_is_not_a_duplicate() -> None:
    # The legitimate case the list form exists to enable: one adapter, two configurations.
    entries = [
        {"name": "log_burst", "params": {"preset": "severity_spike"}},
        {"name": "log_burst", "params": {"preset": "auth_failure"}},
    ]
    issues = check_compatibility(
        cfg(source_format="syslog", normalizer="syslog", detectors=entries)
    )
    assert not [i for i in issues if "report every finding twice" in i.message]


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


class TestPayloadJoint:
    """The lane can be satisfied while the vocabulary inside it is not (ADR-0035 D2)."""

    def _with(self, normalizer: str, source_format: str, **params: Any) -> PipelineConfig:
        # Built without cfg()'s top-level source_format/normalizer: those desugar INTO a
        # one-element `sources` list (ADR-0019), so a config carrying both is refused.
        return PipelineConfig.model_validate(
            {
                "database_url": "sqlite:///x.db",
                "sources": [
                    {"normalizer": normalizer, "source_format": source_format, "params": params}
                ],
                "detectors": [{"name": "meter_iforest", "params": {"artifact": "demo_meter"}}],
                "alert_engine": {"name": "stub"},
            }
        )

    def test_a_bare_telemetry_source_warns_that_the_meter_detector_reads_nothing(self) -> None:
        """The defect this axis exists for. `stub-csv` feeds lane telemetry, `meter_iforest`
        consumes lane telemetry, every joint agreed — and the detector then discarded all
        1,148 events of a SKAB recording and the harness published a row of zeros."""
        issues = check_compatibility(self._with("stub", "stub-csv"))
        assert [i.level for i in issues] == [CompatLevel.WARNING]
        assert "meter payload(s)" in issues[0].message
        assert "discard all of them" in issues[0].message
        # NOT the idle-lane wording: the lane genuinely matches, and saying "will not fire"
        # would send a reader looking for a lane problem that does not exist.
        assert "will not fire" not in issues[0].message

    def test_a_meter_source_satisfies_it(self) -> None:
        assert check_compatibility(self._with("meter", "eoa-meter-csv")) == []

    def test_a_foreign_lane_reports_only_the_lane_problem(self) -> None:
        """One cause, one message. The payload is also unmet when the lane is, and reporting
        both would make the operator fix two things to satisfy one joint."""
        issues = check_compatibility(self._with("syslog", "syslog"))
        messages = [i.message for i in issues]
        assert any("will not fire" in message for message in messages)
        assert not any("discard all of them" in message for message in messages)

    def test_a_passthrough_source_may_declare_the_payload_it_carries(self) -> None:
        """The escape hatch that keeps this a WARNING rather than a refusal: a partner system
        emitting MeterReadings as normalized JSONL is a legitimate source, and only the
        operator can know it (ADR-0025's reasoning, one axis over)."""
        undeclared = self._with("passthrough", "normalized-jsonl", lane="telemetry")
        assert [i.level for i in check_compatibility(undeclared)] == [CompatLevel.WARNING]
        declared = self._with("passthrough", "normalized-jsonl", lane="telemetry", payload="meter")
        assert check_compatibility(declared) == []


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


BENCH_SPECS = sorted((CONFIG_DIR / "bench").glob("*.yaml"))


def test_there_are_shipped_specs_to_check() -> None:
    """An empty parametrize does not fail — it collects nothing and passes silently.

    This guard used to run over `config/demo*.yaml`. Those were deleted when the demo moved
    into the dashboard, and the glob below would have gone quietly vacuous rather than
    telling anyone, so the non-emptiness is asserted separately from the check itself.
    """
    assert BENCH_SPECS, f"no benchmark specs found under {CONFIG_DIR / 'bench'}"


@pytest.mark.parametrize("path", BENCH_SPECS, ids=lambda p: p.stem)
def test_shipped_bench_specs_are_error_free(path: Path) -> None:
    # Every committed spec must at least run: none may contain an ERROR-level issue. These
    # are what the dashboard's Evaluate page offers, so a broken one is a broken demo.
    #
    # `load_spec`, not `load_config`: a benchmark cannot write to a store, so a bench spec
    # carries no `database_url` and would be refused by PipelineConfig for defaulting to a
    # postgres sink it cannot satisfy. That refusal is the type system doing its job
    # (ADR-0021) — the guard itself takes the PipelineSpec both planes share.
    errors = [i for i in check_compatibility(load_spec(path)) if i.level is CompatLevel.ERROR]
    assert errors == [], f"{path.name} has compatibility errors: {errors}"


def test_a_log_source_with_only_a_telemetry_detector_warns_about_the_idle_detector() -> None:
    """The guard's middle verdict: structurally legal, certainly futile.

    Nothing here is *broken* — a syslog normalizer and a telemetry detector are both fine
    adapters — but the detector consumes a lane the run never produces, so it cannot fire.
    Refuse what is structurally broken, mark what is merely futile.
    """
    issues = check_compatibility(cfg(source_format="syslog", normalizer="syslog"))
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
        # Two sources, one telemetry + one log; the log-lane detector no longer idles because
        # a source now produces the log lane (the union across sources feeds the joint).
        #
        # log_burst, not a second stub_threshold. This test used to pair stub_threshold with
        # `lane: log` and assert "both detectors fire: a fully clean chain" — which was false:
        # a LOG event carries no `value`, so that detector could never fire and the guard was
        # certifying a chain that produces nothing. It is now refused at construction, and the
        # assertion here is about lane COVERAGE, which needs a detector that can actually
        # judge the lane to mean anything.
        config = self.cfg_ms(
            [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {"normalizer": "syslog", "source_format": "syslog"},
            ],
            detectors=[
                {"name": "stub_threshold", "params": {"lane": "telemetry"}},
                {"name": "log_burst", "params": {"preset": "auth_failure"}},
            ],
        )
        # Both produced lanes are consumed and both detectors can fire: a fully clean chain.
        assert check_compatibility(config) == []
