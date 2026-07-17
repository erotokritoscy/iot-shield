"""Every adapter must declare a capability, and the declaration must match reality.

This is the capability analogue of the port suites: a structural guarantee that no
adapter ships undeclared, plus behavioural cross-checks that a declared shape agrees
with what the built adapter actually does — so the guard never reads a stale lie.
"""

import pytest

from iotshield.capabilities import Capability, formats_in, lanes_in
from iotshield.contracts import Lane
from iotshield.normalize import STUB_SOURCE_FORMAT, SYSLOG_SOURCE_FORMAT
from iotshield.pipeline.registry import (
    ALERT_ENGINES,
    DETECTORS,
    NORMALIZERS,
    AdapterSpec,
    build_detector,
    build_normalizer,
)

_ALL_SPECS = [
    *((f"normalizer:{name}", spec) for name, spec in NORMALIZERS.items()),
    *((f"detector:{name}", spec) for name, spec in DETECTORS.items()),
    *((f"alert_engine:{name}", spec) for name, spec in ALERT_ENGINES.items()),
]


@pytest.mark.parametrize("label,spec", _ALL_SPECS, ids=[label for label, _ in _ALL_SPECS])
def test_every_adapter_declares_a_nonempty_capability(label: str, spec: AdapterSpec) -> None:
    capability = spec.capability({})  # default params
    assert isinstance(capability, Capability)
    assert capability.consumes, f"{label} declares no consumes tokens"
    assert capability.produces, f"{label} declares no produces tokens"


def test_normalizer_declared_format_matches_source_format_constant() -> None:
    assert formats_in(NORMALIZERS["stub"].capability({}).consumes) == {STUB_SOURCE_FORMAT}
    assert formats_in(NORMALIZERS["syslog"].capability({}).consumes) == {SYSLOG_SOURCE_FORMAT}


def test_stub_normalizer_actually_emits_its_declared_lane() -> None:
    declared = lanes_in(NORMALIZERS["stub"].capability({}).produces)
    result = build_normalizer("stub").normalize(
        "2026-07-07T12:00:00+00:00,pump-02,Pressure,45.0",
        source_format=STUB_SOURCE_FORMAT,
        origin="test",
    )
    assert result.events  # the sample parsed
    assert {event.source_type for event in result.events} <= declared


def test_syslog_normalizer_actually_emits_its_declared_lane() -> None:
    declared = lanes_in(NORMALIZERS["syslog"].capability({}).produces)
    line = "<34>1 2026-07-07T12:00:00Z host-a app 1 ID - disk failure"
    result = build_normalizer("syslog").normalize(
        line, source_format=SYSLOG_SOURCE_FORMAT, origin="test"
    )
    assert result.events  # the sample parsed
    assert {event.source_type for event in result.events} <= declared


@pytest.mark.parametrize(
    "params,expected",
    [({}, Lane.TELEMETRY), ({"lane": "telemetry"}, Lane.TELEMETRY), ({"lane": "log"}, Lane.LOG)],
)
def test_stub_detector_declared_lane_matches_built_instance(
    params: dict[str, str], expected: Lane
) -> None:
    # The param-driven case: the declaration must follow params exactly as the
    # factory does, or the guard would warn (or stay silent) about the wrong lane.
    declared = lanes_in(DETECTORS["stub_threshold"].capability(params).consumes)
    assert declared == {expected}
    assert build_detector("stub_threshold", params).lane == expected


def test_team3_detector_declared_lane_matches_built_instance() -> None:
    declared = lanes_in(DETECTORS["team3_zscore"].capability({}).consumes)
    assert declared == {Lane.TELEMETRY}
    assert build_detector("team3_zscore", {}).lane == Lane.TELEMETRY


def test_log_burst_detector_declared_lane_matches_built_instance() -> None:
    # Fixed-lane detector: the declaration must hold for every preset, not just the
    # default, since the preset changes *what* it counts but never *which* lane it reads.
    assert lanes_in(DETECTORS["log_burst"].capability({}).consumes) == {Lane.LOG}
    for preset in ("severity_spike", "auth_failure"):
        assert build_detector("log_burst", {"preset": preset}).lane == Lane.LOG
