"""Typed control-client tests: the real Pipeline API in-process, models out."""

from typing import Any

from iotshield.dashboard.control_client import ControlClient

STUB_CSV = "2026-07-07T12:00:00+00:00,pump-02,Pressure,96.0\n"


def _spec(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_format": "stub-csv",
        "normalizer": "stub",
        "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
        "alert_engine": {"name": "stub"},
    }
    return base | overrides


def test_modules_parses(control_client: ControlClient) -> None:
    catalogue = control_client.modules()
    assert {m.name for m in catalogue.detectors} >= {"stub_threshold", "team3_zscore"}
    stub = next(m for m in catalogue.detectors if m.name == "stub_threshold")
    assert "detections" in stub.produces
    assert {p.name for p in stub.params} == {"threshold", "lane"}


def test_validate_ok_for_matching_chain(control_client: ControlClient) -> None:
    result = control_client.validate(_spec())
    assert result.ok and result.issues == []


def test_validate_flags_unsupported_format(control_client: ControlClient) -> None:
    result = control_client.validate(_spec(source_format="nope"))
    assert not result.ok
    assert any(i.level == "error" for i in result.issues)


def test_run_returns_summary(control_client: ControlClient) -> None:
    result = control_client.run(_spec(), inputs=[{"content": STUB_CSV}])
    assert (result.events, result.malformed, result.detections, result.alerts) == (1, 0, 1, 1)
    assert result.top_alerts and result.top_alerts[0].asset_id == "pump-02"
