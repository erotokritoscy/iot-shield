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
    assert config.normalizer == "stub"
    assert config.alert_engine.name == "stub"
    assert config.assets == []


def test_unknown_key_rejected() -> None:
    # Typos in YAML must fail loudly, same policy as the contracts.
    with pytest.raises(ValidationError, match="extra"):
        PipelineConfig.model_validate(VALID | {"detectrs": []})


def test_missing_database_url_rejected() -> None:
    incomplete = {k: v for k, v in VALID.items() if k != "database_url"}
    with pytest.raises(ValidationError, match="database_url"):
        PipelineConfig.model_validate(incomplete)


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
    assert load_config(path).source_format == "stub-csv"
