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

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from functools import cache
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

from iotshield.alert import AlertEngine, StubAlertEngine, WeightedAlertEngine
from iotshield.alert.stub import CAPABILITY as _STUB_ALERT_CAPABILITY
from iotshield.alert.weighted import CAPABILITY as _WEIGHTED_ALERT_CAPABILITY
from iotshield.capabilities import Capability, CapabilityProvider, static
from iotshield.contracts import Lane
from iotshield.detect import (
    Detector,
    FlowBurstDetector,
    LogBurstDetector,
    MeterIForestDetector,
    RuleDetector,
    StubThresholdDetector,
    Team3ZScoreDetector,
)
from iotshield.detect._fitted import resolve_params
from iotshield.detect.flow_burst import CAPABILITY as _FLOW_BURST_CAPABILITY
from iotshield.detect.flow_burst import DEFAULT_PRESET as _DEFAULT_FLOW_PRESET
from iotshield.detect.log_burst import CAPABILITY as _LOG_BURST_CAPABILITY
from iotshield.detect.log_burst import DEFAULT_PRESET as _DEFAULT_LOG_PRESET
from iotshield.detect.meter import MeterParams
from iotshield.detect.meter.artifact import available_artifacts as _meter_artifacts
from iotshield.detect.meter.artifact import load_artifact as _load_meter_artifact
from iotshield.detect.meter.detector import CAPABILITY as _METER_CAPABILITY
from iotshield.detect.rules.detector import DEFAULT_LANE as _DEFAULT_RULES_LANE
from iotshield.detect.rules.detector import (
    DEFAULT_MERGE_GAP_MINUTES as _DEFAULT_RULES_MERGE_GAP,
)
from iotshield.detect.rules.detector import capability as _rules_capability
from iotshield.detect.stub import capability as _stub_threshold_capability
from iotshield.detect.team3.artifact import available_artifacts as _team3_artifacts
from iotshield.detect.team3.artifact import load_artifact
from iotshield.detect.team3.detector import CAPABILITY as _TEAM3_CAPABILITY
from iotshield.detect.team3.params import Team3Params
from iotshield.normalize import (
    FlowNormalizer,
    MeterNormalizer,
    Normalizer,
    PassThroughNormalizer,
    StubCsvNormalizer,
    SyslogNormalizer,
)
from iotshield.normalize.flow import CAPABILITY as _FLOW_NORMALIZER_CAPABILITY
from iotshield.normalize.meter import CAPABILITY as _METER_NORMALIZER_CAPABILITY
from iotshield.normalize.passthrough import capability as _passthrough_capability
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

    The two optional fields are how an adapter that *learns* declares the ADR-0022 split,
    so tooling can act on it without knowing the adapter. Both default to "not a learning
    adapter", which is why ``stub_threshold`` and ``log_burst`` say nothing.
    """

    factory: Callable[..., T]
    capability: CapabilityProvider
    params_type: type[BaseModel] | None = None
    """The adapter's hyperparameter model, when it has one.

    Its field defaults are the *real* defaults: a learning adapter's factory params must
    default to ``None`` (the "not given" sentinel ``resolve_params`` needs), which would
    otherwise make ``/modules`` report every knob as having no default and give the UI
    nothing to prefill. Introspection reads the true value from here instead.
    """

    artifact_param: str | None = None
    """Which factory param names a *cached fit*, when the adapter supports one.

    Declared rather than name-sniffed so a UI can offer the real choice — "load trained
    weights" or "fit from scratch with my own hyperparameters" — as two modes instead of
    letting someone combine them and hit the atomicity error (``_fitted.resolve_params``).
    """

    artifacts: tuple[str, ...] = ()
    """Which cached fits this adapter ships (ADR-0035 D1).

    ``artifact_param`` names the *field*; this names the **values**, and without it the two
    shipped ids existed nowhere a client could read them — so the one page that needed them
    carried a hardcoded table listing one of the two.

    Resolved eagerly at import: listing both packages costs 3.5 ms against a 1.7 s
    ``iotshield`` import, and this module already imports the packages that hold them. A
    callable would buy 0.2% of an import that has already happened, at the price of a
    None-vs-empty distinction and a method to reconcile it — "declares no provider" and
    "ships nothing" mean the same thing to every reader, so they are the same value.
    """


def _stub_threshold(
    threshold: float = 80.0, lane: Literal["telemetry", "flow"] = "telemetry"
) -> StubThresholdDetector:
    # float() coerces YAML scalars that arrive quoted (e.g. "90"); a non-numeric
    # value raises ValueError, caught and attributed by _build.
    # The Literal is not enforcement (params arrive untyped from YAML — Lane() is what
    # rejects a bad value); it is *declaration*, so /modules can report the closed
    # vocabulary and the UI can offer it as a choice instead of a free-text box. It offers
    # only the value-carrying lanes: this detector compares event.value, and a LOG event
    # never has one, so `lane: log` would validate clean and then never fire.
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
    artifact: str | None = None,
    window_size: int | None = None,
    lag_steps: list[int] | None = None,
    fft_enabled: bool | None = None,
    fft_include_dc: bool | None = None,
    percentile: float | None = None,
    channels: list[str] | None = None,
    min_segment_length: int | None = None,
) -> Team3ZScoreDetector:
    """Build the team3 detector from a cached fit, from hyperparameters, or from both.

    Every param defaults to ``None`` meaning "not given" — the sentinel ``resolve_params``
    needs to tell an override from an inherited value (ADR-0022). Naming an ``artifact``
    supplies both its hyperparameters and its baseline; naming none yields an *untrained*
    detector, which is legal to build and to fit but refuses to judge. Combining the two
    is refused, because an artifact is atomic.

    A plain pass-through: coercion and validation belong to ``Team3Params``, which
    ``resolve_params`` constructs. Doing it here as well would duplicate what pydantic
    does and would report a bare ``int()`` failure instead of naming the field.
    """
    # load_artifact raises ValueError for an unknown id; _construct attributes it.
    loaded = load_artifact(str(artifact)) if artifact else None
    params = resolve_params(
        Team3Params,
        {
            "window_size": window_size,
            "lag_steps": lag_steps,
            "fft_enabled": fft_enabled,
            "fft_include_dc": fft_include_dc,
            "percentile": percentile,
            "channels": channels,
            "min_segment_length": min_segment_length,
        },
        loaded.params if loaded else None,
    )
    return Team3ZScoreDetector(params, loaded.fitted if loaded else None)


def _log_burst(
    preset: Literal["severity_spike", "auth_failure"] = _DEFAULT_LOG_PRESET,
    window_minutes: float = 5.0,
    min_count: int = 5,
) -> LogBurstDetector:
    # Coercion mirrors _stub_threshold: YAML scalars may arrive quoted; an unknown preset
    # or a non-numeric window/count raises ValueError, caught and attributed by _construct.
    return LogBurstDetector(
        preset=str(preset), window_minutes=float(window_minutes), min_count=int(min_count)
    )


def _flow_normalizer(home_networks: str | list[str] | None = None) -> FlowNormalizer:
    # The first parameterized normalizer (ADR-0023 D8). A bad CIDR raises ValueError,
    # caught and attributed by _construct — so a typo fails at construction, named, rather
    # than silently attributing every flow to the wrong endpoint.
    return FlowNormalizer(home_networks=home_networks)


def _passthrough_normalizer(
    lane: Literal["telemetry", "log", "flow"] = "telemetry",
    payload: Literal["meter", "flow"] | None = None,
) -> PassThroughNormalizer:
    # The first param-driven NORMALIZER capability (ADR-0025): a pass-through record carries
    # its own lane in source_type, which the static guard cannot see, so the lane this source
    # carries is declared here and its capability follows it. The Literal is declaration, not
    # enforcement (params arrive untyped from YAML; Lane() is what rejects a bad value), so
    # /modules can offer the closed vocabulary as a choice instead of a free-text box.
    #
    # `payload` is the same declaration one axis over (ADR-0035 D2), and it changes nothing
    # about parsing — the normalizer envelopes what the file already contains either way. It
    # exists so an operator whose partner emits MeterReadings as JSONL can say so and reach
    # `meter_iforest` without a standing warning. It is read by `passthrough.capability`, not
    # here; `None` (not "") means "no typed vocabulary declared", so it uses the same
    # not-given sentinel as every other optional param and `_literal_choices` sees through
    # the union to offer the two members.
    return PassThroughNormalizer(lane=Lane(lane))


def _optional_float(value: Any) -> float | None:
    """A tunable left blank means "use the preset's default".

    Blank matters as much as absent: the orchestration UI renders an optional numeric as a
    text box, so an untouched field arrives as an empty string rather than as None.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return float(value)


def _flow_burst(
    preset: Literal["fan_out", "port_scan", "exfiltration"] = _DEFAULT_FLOW_PRESET,
    window_minutes: float | None = None,
    threshold: float | None = None,
) -> FlowBurstDetector:
    # window/threshold default to the preset's own values rather than to shared constants:
    # 20 distinct peers per minute and 50 MB per half hour are both right, and neither is
    # right for the other preset (ADR-0024 D3).
    return FlowBurstDetector(
        preset=str(preset),
        window_minutes=_optional_float(window_minutes),
        threshold=_optional_float(threshold),
    )


def _rules(
    lane: Literal["telemetry", "log", "flow"] = _DEFAULT_RULES_LANE.value,
    ruleset: str | None = None,
    merge_gap_minutes: float = _DEFAULT_RULES_MERGE_GAP,
) -> RuleDetector:
    """Build a rule detector for one lane over one rule set (ADR-0032 D3).

    ``lane`` selects which of the rule set's rules this instance evaluates — the Detector
    port gives a detector one lane, so a rule set spanning network and logs is two
    configured entries, not two adapters. The Literal is declaration rather than enforcement
    (params arrive untyped from YAML; ``Lane()`` is what rejects a bad value), so ``/modules``
    can offer the closed vocabulary as a choice instead of a free-text box.

    ``ruleset`` names a directory; left unset, the packaged library ships with the product.
    A malformed rule, an unknown field, or a lane the rule set cannot feed raises here at
    construction — caught and attributed by ``_construct`` — so a bad rule fails named,
    before the run, rather than as a detector that quietly finds nothing.
    """
    return RuleDetector(
        lane=Lane(lane),
        ruleset=str(ruleset) if ruleset else None,
        merge_gap_minutes=float(merge_gap_minutes),
    )


def _meter_normalizer(
    meter_type: Literal["water", "electricity"] | None = None,
    source_timezone: str | None = None,
) -> MeterNormalizer:
    # Both params are deployment facts a dialect cannot know: which utility an export of
    # SensiPro's one table holds, and which wall clock a client's platform writes in. A bad
    # value raises ValueError at construction, caught and attributed by _construct — so a
    # typo fails named rather than silently placing every reading in the wrong hour.
    return MeterNormalizer(meter_type=meter_type, source_timezone=source_timezone)


def _meter_iforest(
    artifact: str | None = None,
    profile_window_hours: int | None = None,
    n_estimators: int | None = None,
    max_samples: int | None = None,
    fp_budget: float | None = None,
    min_episode_hours: int | None = None,
    min_train_hours: int | None = None,
    random_state: int | None = None,
) -> MeterIForestDetector:
    """Build the meter detector from a cached fit, from hyperparameters, or from both.

    Mirrors ``_team3_zscore``: every param defaults to ``None`` meaning "not given", the
    sentinel ``resolve_params`` needs to tell an override from an inherited value
    (ADR-0022). Naming an ``artifact`` supplies both its hyperparameters and its fitted
    meters; naming none yields an *untrained* detector, legal to build and to fit but
    refusing to judge. Combining the two is refused, because an artifact is atomic.
    """
    loaded = _load_meter_artifact(str(artifact)) if artifact else None
    params = resolve_params(
        MeterParams,
        {
            "profile_window_hours": profile_window_hours,
            "n_estimators": n_estimators,
            "max_samples": max_samples,
            "fp_budget": fp_budget,
            "min_episode_hours": min_episode_hours,
            "min_train_hours": min_train_hours,
            "random_state": random_state,
        },
        loaded.params if loaded else None,
    )
    return MeterIForestDetector(params, dict(loaded.fitted) if loaded else None)


NORMALIZERS: dict[str, AdapterSpec[Normalizer]] = {
    "stub": AdapterSpec(StubCsvNormalizer, static(_STUB_NORMALIZER_CAPABILITY)),
    "syslog": AdapterSpec(SyslogNormalizer, static(_SYSLOG_NORMALIZER_CAPABILITY)),
    "flow": AdapterSpec(_flow_normalizer, static(_FLOW_NORMALIZER_CAPABILITY)),
    "passthrough": AdapterSpec(_passthrough_normalizer, _passthrough_capability),
    "meter": AdapterSpec(_meter_normalizer, static(_METER_NORMALIZER_CAPABILITY)),
}

DETECTORS: dict[str, AdapterSpec[Detector]] = {
    "stub_threshold": AdapterSpec(_stub_threshold, _stub_threshold_capability),
    "team3_zscore": AdapterSpec(
        _team3_zscore,
        static(_TEAM3_CAPABILITY),
        params_type=Team3Params,
        artifact_param="artifact",
        artifacts=_team3_artifacts(),
    ),
    "log_burst": AdapterSpec(_log_burst, static(_LOG_BURST_CAPABILITY)),
    "flow_burst": AdapterSpec(_flow_burst, static(_FLOW_BURST_CAPABILITY)),
    "meter_iforest": AdapterSpec(
        _meter_iforest,
        static(_METER_CAPABILITY),
        params_type=MeterParams,
        artifact_param="artifact",
        artifacts=_meter_artifacts(),
    ),
    "rules": AdapterSpec(_rules, _rules_capability),
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
    harness's ``Dataset``): it only indexes and lists keys, so every by-name registry in
    the project shares one lookup and one error convention.
    """
    try:
        return registry[name]
    except KeyError:
        known = ", ".join(sorted(registry))
        raise ValueError(f"unknown {kind} {name!r}; known: {known}") from None


def effective_detector_params(name: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """A detector's params with its factory defaults filled in.

    Two entries are the *same detector* when they build the same thing, which the written
    params alone do not show: ``{}`` and ``{preset: fan_out}`` are identical because
    ``fan_out`` is the default. The guard's duplicate check compares effective params so it
    catches that, rather than only textually-identical entries. Unknown params are left as
    written — the builder rejects them, and this is only about detecting sameness.
    """
    spec = lookup("detector", DETECTORS, name)
    resolved = {
        parameter.name: parameter.default
        for parameter in config_parameters(spec.factory)
        if parameter.default is not inspect.Parameter.empty
    }
    resolved.update(params)
    return resolved


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


def build_normalizer(name: str, params: Mapping[str, Any] | None = None) -> Normalizer:
    """Construct a normalizer by name, with its config params (ADR-0023 D8).

    ``params`` defaults to none for the many call sites that build a parameterless
    normalizer; passing params a normalizer does not accept raises a named config error
    through ``_construct``, exactly as it does for a detector.
    """
    return _build("normalizer", NORMALIZERS, name, params or {})


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


def normalizer_capability(name: str, params: Mapping[str, Any] | None = None) -> Capability:
    return lookup("normalizer", NORMALIZERS, name).capability(params or {})


def detector_capability(name: str, params: Mapping[str, Any]) -> Capability:
    return lookup("detector", DETECTORS, name).capability(params)


def alert_engine_capability(name: str, params: Mapping[str, Any]) -> Capability:
    return lookup("alert engine", ALERT_ENGINES, name).capability(params)


def config_parameters(factory: Callable[..., Any]) -> list[inspect.Parameter]:
    """The factory parameters an operator may set — the adapter's configuration surface.

    One definition of "counts as a config param", shared by the two things that ask:
    ``/modules`` and ``/eval/detectors`` describe them for a UI, and the harness's sweep
    expansion checks membership before rewriting a spec (ADR-0022). Kept in one place
    because the exclusions are policy — ``resources`` is the deployment bag injected into
    sink factories, not something a config names — and two copies would eventually let a
    knob be advertised that cannot be swept, or the reverse.
    """
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return []
    return [
        parameter
        for name, parameter in signature.parameters.items()
        if name not in ("self", "resources")
        and parameter.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
    ]


@cache
def detector_param_names(name: str) -> frozenset[str]:
    """Which hyperparameters a registered detector accepts.

    Cached because ``DETECTORS`` is a module-level constant with static factories, so the
    answer cannot change in a process — the same invariant ``/modules`` relies on to build
    its catalogue once. Sweep expansion asks this per detector per spec, so without the
    cache a benchmark re-runs ``inspect.signature`` for an answer that is already known.
    """
    return frozenset(
        parameter.name
        for parameter in config_parameters(lookup("detector", DETECTORS, name).factory)
    )
