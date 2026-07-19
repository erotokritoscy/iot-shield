"""Adapter registries: config names -> constructed port implementations + capabilities.

This is the plug board that makes module swapping operational: integrating a new
adapter = one ``AdapterSpec`` entry here + one name in the YAML config. The runner
never changes. Each entry pairs the adapter's *factory* (how to build it) with its
*capability* (how it connects), so an adapter cannot be registered without also
declaring what it consumes/produces — the compatibility guard and the ``/modules``
discovery endpoint read the capability WITHOUT constructing the (possibly expensive)
adapter. Factory parameters come straight from the config's ``params`` mapping;
unknown names and bad params fail loudly with the known alternatives.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Generic, TypeVar

from iotshield.alert import AlertEngine, StubAlertEngine, WeightedAlertEngine
from iotshield.alert.stub import CAPABILITY as _STUB_ALERT_CAPABILITY
from iotshield.alert.weighted import CAPABILITY as _WEIGHTED_ALERT_CAPABILITY
from iotshield.capabilities import Capability, CapabilityProvider, static
from iotshield.contracts import Lane
from iotshield.detect import (
    Detector,
    LogBurstDetector,
    StubThresholdDetector,
    Team3ZScoreDetector,
)
from iotshield.detect.log_burst import CAPABILITY as _LOG_BURST_CAPABILITY
from iotshield.detect.log_burst import DEFAULT_PRESET as _DEFAULT_LOG_PRESET
from iotshield.detect.stub import capability as _stub_threshold_capability
from iotshield.detect.team3.detector import CAPABILITY as _TEAM3_CAPABILITY
from iotshield.normalize import Normalizer, StubCsvNormalizer, SyslogNormalizer
from iotshield.normalize.stub import CAPABILITY as _STUB_NORMALIZER_CAPABILITY
from iotshield.normalize.syslog import CAPABILITY as _SYSLOG_NORMALIZER_CAPABILITY
from iotshield.sink import ResultSink, SinkResources
from iotshield.sink.file import build as _build_file_sink
from iotshield.sink.postgres import build as _build_postgres_sink

T = TypeVar("T")
V = TypeVar("V")


@dataclass(frozen=True)
class AdapterSpec(Generic[T]):
    """A registered adapter: how to build it, and how it connects.

    Pairing factory and capability in one record means the two can never drift out
    of each other's existence — you cannot add an adapter without declaring its shape.
    """

    factory: Callable[..., T]
    capability: CapabilityProvider


def _stub_threshold(threshold: float = 80.0, lane: str = "telemetry") -> StubThresholdDetector:
    # float() coerces YAML scalars that arrive quoted (e.g. "90"); a non-numeric
    # value raises ValueError, caught and attributed by _build.
    return StubThresholdDetector(threshold=float(threshold), lane=Lane(lane))


def _bucket_timedelta(bucket_minutes: float) -> timedelta:
    minutes = float(bucket_minutes)
    if minutes <= 0:
        raise ValueError(f"bucket_minutes must be positive, got {minutes}")
    return timedelta(minutes=minutes)


def _stub_alert_engine(bucket_minutes: float = 5.0) -> StubAlertEngine:
    return StubAlertEngine(bucket=_bucket_timedelta(bucket_minutes))


def _weighted_alert_engine(bucket_minutes: float = 5.0) -> WeightedAlertEngine:
    return WeightedAlertEngine(bucket=_bucket_timedelta(bucket_minutes))


def _team3_zscore(
    artifact: str = "skab_zscore", min_segment_length: int = 1
) -> Team3ZScoreDetector:
    # from_resource raises ValueError for an unknown artifact id; _build attributes it.
    return Team3ZScoreDetector.from_resource(
        str(artifact), min_segment_length=int(min_segment_length)
    )


def _log_burst(
    preset: str = _DEFAULT_LOG_PRESET, window_minutes: float = 5.0, min_count: int = 5
) -> LogBurstDetector:
    # Coercion mirrors _stub_threshold: YAML scalars may arrive quoted; an unknown preset
    # or a non-numeric window/count raises ValueError, caught and attributed by _construct.
    return LogBurstDetector(
        preset=str(preset), window_minutes=float(window_minutes), min_count=int(min_count)
    )


NORMALIZERS: dict[str, AdapterSpec[Normalizer]] = {
    "stub": AdapterSpec(StubCsvNormalizer, static(_STUB_NORMALIZER_CAPABILITY)),
    "syslog": AdapterSpec(SyslogNormalizer, static(_SYSLOG_NORMALIZER_CAPABILITY)),
}

DETECTORS: dict[str, AdapterSpec[Detector]] = {
    "stub_threshold": AdapterSpec(_stub_threshold, _stub_threshold_capability),
    "team3_zscore": AdapterSpec(_team3_zscore, static(_TEAM3_CAPABILITY)),
    "log_burst": AdapterSpec(_log_burst, static(_LOG_BURST_CAPABILITY)),
}

ALERT_ENGINES: dict[str, AdapterSpec[AlertEngine]] = {
    "stub": AdapterSpec(_stub_alert_engine, static(_STUB_ALERT_CAPABILITY)),
    "weighted": AdapterSpec(_weighted_alert_engine, static(_WEIGHTED_ALERT_CAPABILITY)),
}


@dataclass(frozen=True)
class SinkSpec:
    """A registered result sink: how to build it, and whether it needs a database.

    A lighter record than ``AdapterSpec``: sinks carry no ``Capability`` because they are
    terminal consumers of the whole ``RunResult``, not typed joints the guard reasons
    about (ADR-0018). ``requires_database`` lets a host run build a DB engine only when a
    configured sink actually needs one — so a file-only run needs no database at all. The
    factory takes the deployment's ``SinkResources`` first, then its config ``params``.
    """

    factory: Callable[..., ResultSink]
    requires_database: bool = False


SINKS: dict[str, SinkSpec] = {
    "postgres": SinkSpec(_build_postgres_sink, requires_database=True),
    "file": SinkSpec(_build_file_sink),
}


def lookup(kind: str, registry: Mapping[str, V], name: str) -> V:
    """Resolve a registered spec by name, or raise naming the known alternatives.

    Generic over the spec type (``AdapterSpec``, ``SinkSpec``, and the evaluation
    harness's ``CorpusSpec``): it only indexes and lists keys, so every by-name registry
    in the project shares one lookup and one error convention.
    """
    try:
        return registry[name]
    except KeyError:
        known = ", ".join(sorted(registry))
        raise ValueError(f"unknown {kind} {name!r}; known: {known}") from None


def _construct(kind: str, name: str, build: Callable[[], T]) -> T:
    """Build an adapter, turning a construction failure into a named config error.

    TypeError: an unknown/misspelled param name. ValueError: a bad param *value*
    (non-numeric threshold, non-positive bucket, unknown Lane) or a missing required
    resource (the postgres sink with no database). Both fail here at construction —
    before any run side effect — named, so the operator knows which entry is wrong.
    """
    try:
        return build()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid params for {kind} {name!r}: {exc}") from exc


def _build(
    kind: str, registry: Mapping[str, AdapterSpec[T]], name: str, params: Mapping[str, Any]
) -> T:
    spec = lookup(kind, registry, name)
    return _construct(kind, name, lambda: spec.factory(**params))


def build_normalizer(name: str) -> Normalizer:
    return _build("normalizer", NORMALIZERS, name, {})


def build_detector(name: str, params: Mapping[str, Any]) -> Detector:
    return _build("detector", DETECTORS, name, params)


def build_alert_engine(name: str, params: Mapping[str, Any]) -> AlertEngine:
    return _build("alert engine", ALERT_ENGINES, name, params)


def build_sink(name: str, params: Mapping[str, Any], resources: SinkResources) -> ResultSink:
    """Construct a sink by name, injecting the deployment's resources plus config params."""
    spec = lookup("sink", SINKS, name)
    return _construct("sink", name, lambda: spec.factory(resources, **params))


def sink_requires_database(name: str) -> bool:
    """Whether a configured sink needs a DB engine — so a host run builds one only if so."""
    return lookup("sink", SINKS, name).requires_database


# --- Capability resolution (no construction): for the guard and /modules discovery.
# Resolution can raise ValueError for an unknown name or a bad param value (e.g. an
# invalid lane); the guard catches that and reports it as a structured issue.


def normalizer_capability(name: str) -> Capability:
    return lookup("normalizer", NORMALIZERS, name).capability({})


def detector_capability(name: str, params: Mapping[str, Any]) -> Capability:
    return lookup("detector", DETECTORS, name).capability(params)


def alert_engine_capability(name: str, params: Mapping[str, Any]) -> Capability:
    return lookup("alert engine", ALERT_ENGINES, name).capability(params)
