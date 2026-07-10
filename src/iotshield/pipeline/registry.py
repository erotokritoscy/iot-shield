"""Adapter registries: config names -> constructed port implementations.

This is the plug board that makes module swapping operational: integrating a
new adapter = one factory entry here + one name in the YAML config. The runner
never changes. Factory parameters come straight from the config's ``params``
mapping; unknown names and bad params fail loudly with the known alternatives.
"""

from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any, TypeVar

from iotshield.alert import AlertEngine, StubAlertEngine, WeightedAlertEngine
from iotshield.contracts import Lane
from iotshield.detect import Detector, StubThresholdDetector
from iotshield.normalize import Normalizer, StubCsvNormalizer, SyslogNormalizer

T = TypeVar("T")


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


NORMALIZERS: dict[str, Callable[..., Normalizer]] = {
    "stub": StubCsvNormalizer,
    "syslog": SyslogNormalizer,
}

DETECTORS: dict[str, Callable[..., Detector]] = {
    "stub_threshold": _stub_threshold,
}

ALERT_ENGINES: dict[str, Callable[..., AlertEngine]] = {
    "stub": _stub_alert_engine,
    "weighted": _weighted_alert_engine,
}


def _build(
    kind: str, registry: Mapping[str, Callable[..., T]], name: str, params: Mapping[str, Any]
) -> T:
    try:
        factory = registry[name]
    except KeyError:
        known = ", ".join(sorted(registry))
        raise ValueError(f"unknown {kind} {name!r}; known: {known}") from None
    try:
        return factory(**params)
    except (TypeError, ValueError) as exc:
        # TypeError: unknown/misspelled param name. ValueError: a bad param
        # *value* (non-numeric threshold, non-positive bucket, unknown Lane) —
        # both fail here at construction, before any database is touched, and
        # name the adapter so the operator knows which config entry is wrong.
        raise ValueError(f"invalid params for {kind} {name!r}: {exc}") from exc


def build_normalizer(name: str) -> Normalizer:
    return _build("normalizer", NORMALIZERS, name, {})


def build_detector(name: str, params: Mapping[str, Any]) -> Detector:
    return _build("detector", DETECTORS, name, params)


def build_alert_engine(name: str, params: Mapping[str, Any]) -> AlertEngine:
    return _build("alert engine", ALERT_ENGINES, name, params)
