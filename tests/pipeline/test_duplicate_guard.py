"""Duplicate ingestion and duplicate judgment: the two ways a run counts evidence twice.

The guard existed for detectors and had reproducible false negatives; sources had no guard
at all. Both matter because a repeat is not new evidence, and the queue reads it as if it
were (ADR-0033).
"""

from typing import Any

from iotshield.pipeline import PipelineConfig
from iotshield.pipeline.compat import check_compatibility

DOUBLED = "report every finding twice"


def _config(**overrides: Any) -> PipelineConfig:
    base: dict[str, Any] = {
        "database_url": "sqlite:///x.db",
        "sources": [{"normalizer": "stub", "source_format": "stub-csv"}],
        "detectors": [{"name": "stub_threshold", "params": {"lane": "telemetry"}}],
        "alert_engine": {"name": "weighted"},
    }
    return PipelineConfig.model_validate(base | overrides)


def _warnings(config: PipelineConfig, needle: str) -> list[str]:
    return [i.message for i in check_compatibility(config) if needle in i.message]


class TestDuplicateDetectorsAreRecognisedByWhatTheyBuild:
    def test_an_omitted_param_equal_to_its_default_is_a_duplicate(self) -> None:
        """effective_detector_params filled defaults from inspect.signature, which is the real
        default only when an adapter put it there."""
        entries = [
            {"name": "stub_threshold", "params": {"lane": "telemetry"}},
            {"name": "stub_threshold", "params": {"lane": "telemetry", "threshold": 80}},
        ]

        assert _warnings(_config(detectors=entries), DOUBLED)

    def test_a_preset_derived_default_written_out_is_a_duplicate(self) -> None:
        """flow_burst's window and threshold come from its PRESET, not its signature, so
        {preset: fan_out} and {preset: fan_out, threshold: 20} built identical detectors
        and fingerprinted differently."""
        entries = [
            {"name": "flow_burst", "params": {"preset": "fan_out"}},
            {"name": "flow_burst", "params": {"preset": "fan_out", "threshold": 20}},
        ]
        config = _config(
            sources=[{"normalizer": "flow", "source_format": "flow-csv"}], detectors=entries
        )

        assert _warnings(config, DOUBLED)

    def test_an_int_and_a_float_of_the_same_value_are_a_duplicate(self) -> None:
        """config_fingerprint hashes with json.dumps, so YAML's 80 and a factory's 80.0
        disagreed about a detector that is the same detector."""
        entries = [
            {"name": "stub_threshold", "params": {"threshold": 80}},
            {"name": "stub_threshold", "params": {"threshold": 80.0}},
        ]

        assert _warnings(_config(detectors=entries), DOUBLED)

    def test_two_genuinely_different_configurations_are_not_a_duplicate(self) -> None:
        """The case the list form exists for — a false positive here would be worse than the
        false negatives, because it would fire on demo_rules' signature+statistics pairing."""
        entries = [
            {"name": "stub_threshold", "params": {"threshold": 80.0}},
            {"name": "stub_threshold", "params": {"threshold": 50.0}},
        ]

        assert not _warnings(_config(detectors=entries), DOUBLED)

    def test_two_different_adapters_on_one_lane_are_not_a_duplicate(self) -> None:
        """demo_rules ships rules(lane: flow) alongside flow_burst on purpose: signatures and
        statistics are complements, so same-lane co-configuration must stay legal."""
        entries = [
            {"name": "rules", "params": {"lane": "flow"}},
            {"name": "flow_burst", "params": {"preset": "fan_out"}},
        ]
        config = _config(
            sources=[{"normalizer": "flow", "source_format": "flow-csv"}], detectors=entries
        )

        assert not _warnings(config, DOUBLED)


class TestDuplicateSourcesAreCaught:
    def test_the_same_file_listed_twice_warns(self) -> None:
        """A run pools its sources, so this ingests every record twice — the generator
        already refuses the equivalent scenario for exactly this reason."""
        sources = [
            {"normalizer": "stub", "source_format": "stub-csv", "path": "data/demo/demo.csv"},
            {"normalizer": "stub", "source_format": "stub-csv", "path": "data/demo/demo.csv"},
        ]

        messages = _warnings(_config(sources=sources), "counted twice")

        assert messages
        assert "data/demo/demo.csv" in messages[0]

    def test_two_different_files_through_one_normalizer_are_fine(self) -> None:
        sources = [
            {"normalizer": "stub", "source_format": "stub-csv", "path": "data/demo/demo.csv"},
            {"normalizer": "stub", "source_format": "stub-csv", "path": "data/demo/other.csv"},
        ]

        assert not _warnings(_config(sources=sources), "counted twice")

    def test_two_lanes_from_two_sources_are_fine(self) -> None:
        sources = [
            {"normalizer": "stub", "source_format": "stub-csv"},
            {"normalizer": "syslog", "source_format": "syslog"},
        ]
        config = _config(
            sources=sources,
            detectors=[
                {"name": "stub_threshold", "params": {"lane": "telemetry"}},
                {"name": "log_burst", "params": {"preset": "auth_failure"}},
            ],
        )

        assert not _warnings(config, "counted twice")
