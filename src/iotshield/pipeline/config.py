"""Pipeline run configuration: which adapters run, against which database.

The config file is the module-selection mechanism the internship plan requires:
swapping a module = editing a name here, never touching runner code. Unknown
keys are rejected (typos in YAML fail loudly, same policy as the contracts).
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from iotshield.contracts import Asset


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DetectorConfig(_ConfigModel):
    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class AlertEngineConfig(_ConfigModel):
    name: str = "stub"
    params: dict[str, Any] = Field(default_factory=dict)


class PipelineConfig(_ConfigModel):
    database_url: str = Field(min_length=1)
    source_format: str = Field(min_length=1)
    normalizer: str = "stub"
    detectors: list[DetectorConfig] = Field(min_length=1)
    alert_engine: AlertEngineConfig = Field(default_factory=AlertEngineConfig)
    assets: list[Asset] = Field(default_factory=list, description="Seed registrations")


def load_config(path: Path) -> PipelineConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} must contain a YAML mapping")
    return PipelineConfig.model_validate(raw)
