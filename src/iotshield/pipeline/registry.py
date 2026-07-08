"""Adapter registries: config names -> constructed port implementations.

This is the plug board that makes module swapping operational: integrating a
new adapter = one factory entry here + one name in the YAML config. The runner
never changes. Factory parameters come straight from the config's ``params``
mapping; unknown names and bad params fail loudly with the known alternatives.
"""

from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any, TypeVar

from iotshield.alert import AlertEngine, StubAlertEngine
from iotshield.contracts import Lane
from iotshield.detect import Detector, StubThresholdDetector
from iotshield.normalize import Normalizer, StubCsvNormalizer

T = TypeVar("T")


def _stub_threshold(threshold: float = 80.0, lane: str = "telemetry") -> StubThresholdDetector:
    return StubThresholdDetector(threshold=threshold, lane=Lane(lane))


def _stub_alert_engine(bucket_minutes: float = 5.0) -> StubAlertEngine:
    return StubAlertEngine(bucket=timedelta(minutes=bucket_minutes))


NORMALIZERS: dict[str, Callable[..., Normalizer]] = {
    "stub": StubCsvNormalizer,
}

DETECTORS: dict[str, Callable[..., Detector]] = {
    "stub_threshold": _stub_threshold,
}

ALERT_ENGINES: dict[str, Callable[..., AlertEngine]] = {
    "stub": _stub_alert_engine,
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
    except TypeError as exc:
        raise ValueError(f"invalid params for {kind} {name!r}: {exc}") from exc


def build_normalizer(name: str) -> Normalizer:
    return _build("normalizer", NORMALIZERS, name, {})


def build_detector(name: str, params: Mapping[str, Any]) -> Detector:
    return _build("detector", DETECTORS, name, params)


def build_alert_engine(name: str, params: Mapping[str, Any]) -> AlertEngine:
    return _build("alert engine", ALERT_ENGINES, name, params)
