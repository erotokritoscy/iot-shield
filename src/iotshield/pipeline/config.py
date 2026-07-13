"""Pipeline run configuration: which adapters run, against which database.

The config file is the module-selection mechanism the internship plan requires:
swapping a module = editing a name here, never touching runner code. Unknown
keys are rejected (typos in YAML fail loudly, same policy as the contracts).

The one value that also reads from the environment is ``database_url``:
``IOTSHIELD_DATABASE_URL`` overrides it when set, so a single config file runs
both on the host (its SQLite default) and in compose (Postgres, injected per
12-factor) — matching how the API and dashboard already resolve their wiring.
"""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from iotshield.contracts import Asset
from iotshield.storage.db import DATABASE_URL_ENV


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DetectorConfig(_ConfigModel):
    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class AlertEngineConfig(_ConfigModel):
    name: str = "stub"
    params: dict[str, Any] = Field(default_factory=dict)


class PipelineSpec(_ConfigModel):
    """The operator-chosen shape of a run: which adapters, over what input format.

    Deliberately excludes ``database_url`` — where results are stored is a
    *deployment* decision, not an operator one (a tenant must never point a run at an
    arbitrary database). The Pipeline API accepts a ``PipelineSpec`` and the server
    supplies ``database_url`` from its environment; the CLI's ``load_config`` produces
    a full ``PipelineConfig`` directly. This split is also where a future output-sink
    selection will live, alongside the database (ADR-0015 / week-2 §9-A).
    """

    source_format: str = Field(min_length=1)
    normalizer: str = "stub"
    detectors: list[DetectorConfig] = Field(min_length=1)
    alert_engine: AlertEngineConfig = Field(default_factory=AlertEngineConfig)
    assets: list[Asset] = Field(default_factory=list, description="Seed registrations")


class PipelineConfig(PipelineSpec):
    """A runnable config: an operator's spec plus the deployment's database."""

    database_url: str = Field(min_length=1)


def config_from_spec(spec: PipelineSpec, database_url: str) -> PipelineConfig:
    """Assemble a runnable config from an operator spec + a deployment-supplied database.

    The seam that keeps database choice server-side: the API validates a spec off the
    wire, then this stitches in the environment's ``database_url`` — the caller never
    names it.
    """
    return PipelineConfig.model_validate({**spec.model_dump(), "database_url": database_url})


def load_config(path: Path) -> PipelineConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # yaml.YAMLError is not a ValueError, so surface config-syntax errors as
        # one — the CLI catches ValueError and exits 2 with a message.
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} must contain a YAML mapping")
    # Env wins over the file for the DB location (compose injects Postgres; the
    # file keeps its SQLite default for host runs). It may also supply the value
    # for a config that omits it entirely.
    env_url = os.environ.get(DATABASE_URL_ENV)
    if env_url:
        raw = {**raw, "database_url": env_url}
    return PipelineConfig.model_validate(raw)
