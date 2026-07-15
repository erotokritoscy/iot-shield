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
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from iotshield.contracts import Asset
from iotshield.storage.db import DATABASE_URL_ENV

_POSTGRES_SINK = "postgres"


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DetectorConfig(_ConfigModel):
    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class AlertEngineConfig(_ConfigModel):
    name: str = "stub"
    params: dict[str, Any] = Field(default_factory=dict)


class SinkConfig(_ConfigModel):
    """One result sink: where a run's results are stored or delivered (ADR-0018)."""

    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


def _default_sinks() -> list[SinkConfig]:
    # The default is the Postgres store — so a config that names no sinks (every config
    # written before the output seam existed) behaves exactly as it did before.
    return [SinkConfig(name=_POSTGRES_SINK)]


class PipelineSpec(_ConfigModel):
    """The operator-chosen shape of a run: which adapters, over what input format, to where.

    Deliberately excludes ``database_url`` — *where the database lives* is a deployment
    decision, not an operator one (a tenant must never point a run at an arbitrary
    database). The Pipeline API accepts a ``PipelineSpec`` and the server supplies
    ``database_url`` from its environment; the CLI's ``load_config`` produces a full
    ``PipelineConfig`` directly. ``sinks`` *is* operator-chosen (which stores/deliveries
    to fan the result out to); the Postgres sink among them is what reads ``database_url``.
    """

    source_format: str = Field(min_length=1)
    normalizer: str = "stub"
    detectors: list[DetectorConfig] = Field(min_length=1)
    alert_engine: AlertEngineConfig = Field(default_factory=AlertEngineConfig)
    assets: list[Asset] = Field(default_factory=list, description="Seed registrations")
    sinks: list[SinkConfig] = Field(default_factory=_default_sinks, min_length=1)


class PipelineConfig(PipelineSpec):
    """A runnable config: an operator's spec plus the deployment's result store(s).

    ``database_url`` is optional now that the store is pluggable: a file-only deployment
    needs no database at all. It is *required* precisely when a ``postgres`` sink is in
    play — enforced below so the misconfiguration fails at load, not at write time.
    """

    database_url: str | None = None

    @model_validator(mode="after")
    def _postgres_sink_needs_database(self) -> Self:
        if any(sink.name == _POSTGRES_SINK for sink in self.sinks) and not self.database_url:
            raise ValueError(
                "a 'postgres' sink requires database_url "
                "(set it in the config or via IOTSHIELD_DATABASE_URL)"
            )
        return self


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
