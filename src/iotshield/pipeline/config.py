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


class SourceSpec(_ConfigModel):
    """One input source: which normalizer parses it, asserting its raw format (ADR-0019).

    A run ingests a *list* of these, so one run can carry mixed modalities (telemetry CSV
    *and* syslog) that pool into the detector lanes. Routing is explicit — the operator (or
    source system) tags each source with its ``normalizer``/``source_format``; the pipeline
    never sniffs a blob's format (the normalizer double-checks and rejects a mismatch, as a
    config error). ``path`` is a loading hint used only by the file-reading CLI to locate a
    source's content; the Pipeline API ignores it (content arrives in the request) and the
    compatibility guard ignores it (it is not part of the chain's shape).

    ``params`` configures the normalizer itself, exactly as ``DetectorConfig.params``
    configures a detector (ADR-0023 D8). Normalizers were the only one of the four ports
    that could not be parameterized — an asymmetry rather than a decision: the builder
    hardcoded an empty mapping, while the ``/modules`` catalogue already introspected
    normalizer factory signatures for parameters that could never arrive. The flow
    normalizer's ``home_networks`` is the first real need. Additive with a default, so
    every config file and API request written before this stays valid.
    """

    normalizer: str = "stub"
    source_format: str = Field(min_length=1)
    path: str | None = Field(default=None, description="CLI/config: where to read this source")
    params: dict[str, Any] = Field(default_factory=dict, description="Normalizer construction")


class PipelineSpec(_ConfigModel):
    """The operator-chosen shape of a run: which adapters, over what input source(s), to where.

    Deliberately excludes ``database_url`` — *where the database lives* is a deployment
    decision, not an operator one (a tenant must never point a run at an arbitrary
    database). The Pipeline API accepts a ``PipelineSpec`` and the server supplies
    ``database_url`` from its environment; the CLI's ``load_config`` produces a full
    ``PipelineConfig`` directly. ``sinks`` *is* operator-chosen (which stores/deliveries
    to fan the result out to); the Postgres sink among them is what reads ``database_url``.

    ``sources`` is the multi-source ingest list (ADR-0019). A pre-validator desugars the
    legacy single-source form (top-level ``source_format``/``normalizer``) into a one-element
    ``sources`` list, so every config and spec written before multi-source keeps working.
    """

    sources: list[SourceSpec] = Field(min_length=1)
    detectors: list[DetectorConfig] = Field(min_length=1)
    alert_engine: AlertEngineConfig = Field(default_factory=AlertEngineConfig)
    assets: list[Asset] = Field(default_factory=list, description="Seed registrations")
    sinks: list[SinkConfig] = Field(default_factory=_default_sinks, min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _desugar_single_source(cls, data: Any) -> Any:
        """Fold a legacy top-level ``source_format``/``normalizer`` into ``sources=[...]``.

        Runs before field validation (so ``extra="forbid"`` never sees the legacy keys) and
        before ``PipelineConfig``'s after-validator. Naming both a legacy field and ``sources``
        is a contradiction, so it is refused rather than silently resolved one way.
        """
        if not isinstance(data, dict):
            return data
        legacy = {key: data[key] for key in ("source_format", "normalizer") if key in data}
        if not legacy:
            return data
        if "sources" in data:
            raise ValueError(
                "specify either the top-level source_format/normalizer or a 'sources' list, "
                "not both"
            )
        rest = {key: value for key, value in data.items() if key not in legacy}
        return {**rest, "sources": [legacy]}


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


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # yaml.YAMLError is not a ValueError, so surface config-syntax errors as
        # one — the CLI catches ValueError and exits 2 with a message.
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} must contain a YAML mapping")
    return raw


def load_config(path: Path) -> PipelineConfig:
    raw = _load_yaml_mapping(path)
    # Env wins over the file for the DB location (compose injects Postgres; the
    # file keeps its SQLite default for host runs). It may also supply the value
    # for a config that omits it entirely.
    env_url = os.environ.get(DATABASE_URL_ENV)
    if env_url:
        raw = {**raw, "database_url": env_url}
    return PipelineConfig.model_validate(raw)


def load_spec(path: Path) -> PipelineSpec:
    """Load a spec with no deployment wiring — for callers that only ever compute.

    The evaluation harness's loader (ADR-0021). Deliberately does *not* read
    ``IOTSHIELD_DATABASE_URL`` or require ``database_url``: a benchmark computes results and
    scores them in memory, so demanding a database it will never open — which
    ``load_config`` would, because ``sinks`` defaults to ``[postgres]`` — would be asking for
    a credential to satisfy a sink that never runs.
    """
    return PipelineSpec.model_validate(_load_yaml_mapping(path))
