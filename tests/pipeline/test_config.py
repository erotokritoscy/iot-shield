from pathlib import Path

import pytest
from pydantic import ValidationError

from iotshield.contracts import Criticality
from iotshield.pipeline import PipelineConfig, load_config

VALID = {
    "database_url": "sqlite:///x.db",
    "source_format": "stub-csv",
    "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
}


def test_minimal_config_parses_with_defaults() -> None:
    config = PipelineConfig.model_validate(VALID)
    assert config.alert_engine.name == "stub"
    assert config.assets == []


def test_legacy_single_source_desugars_into_one_source() -> None:
    # The top-level source_format/normalizer form (every config written before ADR-0019)
    # folds into a one-element sources list, so nothing downstream sees the legacy shape.
    config = PipelineConfig.model_validate(VALID)
    assert len(config.sources) == 1
    assert config.sources[0].normalizer == "stub"
    assert config.sources[0].source_format == "stub-csv"


def test_multi_source_form_parses() -> None:
    config = PipelineConfig.model_validate(
        {
            "database_url": "sqlite:///x.db",
            "sources": [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {"normalizer": "syslog", "source_format": "syslog"},
            ],
            "detectors": [{"name": "stub_threshold"}],
        }
    )
    assert [s.normalizer for s in config.sources] == ["stub", "syslog"]


def test_source_params_default_to_empty_and_parse_when_given() -> None:
    # ADR-0023 D8: additive with a default, so every config written before it stays valid.
    config = PipelineConfig.model_validate(
        {
            "database_url": "sqlite:///x.db",
            "sources": [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {
                    "normalizer": "flow",
                    "source_format": "flow-csv",
                    "params": {"home_networks": ["10.0.0.0/8"]},
                },
            ],
            "detectors": [{"name": "stub_threshold"}],
        }
    )
    assert config.sources[0].params == {}
    assert config.sources[1].params == {"home_networks": ["10.0.0.0/8"]}


def test_naming_both_legacy_and_sources_is_refused() -> None:
    # A contradiction (which one wins?) — refuse it loudly rather than resolve silently.
    with pytest.raises(ValidationError, match="not both"):
        PipelineConfig.model_validate(VALID | {"sources": [{"source_format": "stub-csv"}]})


def test_empty_sources_list_rejected() -> None:
    with pytest.raises(ValidationError, match="sources"):
        PipelineConfig.model_validate(
            {
                "database_url": "sqlite:///x.db",
                "sources": [],
                "detectors": [{"name": "stub_threshold"}],
            }
        )


def test_unknown_key_rejected() -> None:
    # Typos in YAML must fail loudly, same policy as the contracts.
    with pytest.raises(ValidationError, match="extra"):
        PipelineConfig.model_validate(VALID | {"detectrs": []})


def test_missing_database_url_rejected() -> None:
    # The default sink is postgres, which requires a database — so omitting it fails.
    incomplete = {k: v for k, v in VALID.items() if k != "database_url"}
    with pytest.raises(ValidationError, match="database_url"):
        PipelineConfig.model_validate(incomplete)


def test_default_sink_is_postgres() -> None:
    # A config that names no sinks behaves as before the output seam existed (ADR-0018).
    config = PipelineConfig.model_validate(VALID)
    assert [sink.name for sink in config.sinks] == ["postgres"]


def test_file_only_config_needs_no_database_url() -> None:
    # A pluggable store means a file-only run is valid with no database at all.
    config = PipelineConfig.model_validate(
        {
            "source_format": "stub-csv",
            "detectors": [{"name": "stub_threshold"}],
            "sinks": [{"name": "file", "params": {"path": "out.jsonl"}}],
        }
    )
    assert config.database_url is None
    assert [sink.name for sink in config.sinks] == ["file"]


def test_empty_detector_list_rejected() -> None:
    with pytest.raises(ValidationError, match="detectors"):
        PipelineConfig.model_validate(VALID | {"detectors": []})


def test_assets_parse_into_contract_objects() -> None:
    config = PipelineConfig.model_validate(
        VALID
        | {
            "assets": [
                {
                    "asset_id": "pump-02",
                    "name": "Feed pump 2",
                    "asset_type": "pump",
                    "criticality": "high",
                }
            ]
        }
    )
    assert config.assets[0].criticality == Criticality.HIGH


def test_load_config_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(path)


def test_load_config_reads_yaml_file(tmp_path: Path) -> None:
    path = tmp_path / "ok.yaml"
    path.write_text(
        "database_url: sqlite:///x.db\n"
        "source_format: stub-csv\n"
        "detectors:\n"
        "  - name: stub_threshold\n",
        encoding="utf-8",
    )
    assert load_config(path).sources[0].source_format == "stub-csv"


_MINIMAL_YAML = "source_format: stub-csv\ndetectors:\n  - name: stub_threshold\n"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_env_database_url_overrides_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Compose injects the Postgres URL; it must win over the file's SQLite default.
    path = _write(tmp_path, "database_url: sqlite:///file.db\n" + _MINIMAL_YAML)
    monkeypatch.setenv("IOTSHIELD_DATABASE_URL", "postgresql+psycopg://u:p@postgres:5432/db")
    assert load_config(path).database_url == "postgresql+psycopg://u:p@postgres:5432/db"


def test_env_database_url_supplies_value_absent_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A config that omits database_url is valid when the environment provides it.
    path = _write(tmp_path, _MINIMAL_YAML)
    monkeypatch.setenv("IOTSHIELD_DATABASE_URL", "sqlite:///env.db")
    assert load_config(path).database_url == "sqlite:///env.db"


def test_config_file_url_used_when_env_absent(tmp_path: Path) -> None:
    # The hermetic autouse fixture guarantees IOTSHIELD_DATABASE_URL is unset.
    path = _write(tmp_path, "database_url: sqlite:///file.db\n" + _MINIMAL_YAML)
    assert load_config(path).database_url == "sqlite:///file.db"
