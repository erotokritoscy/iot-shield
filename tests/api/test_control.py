"""Pipeline API (write side): discovery, validation, and runs that persist.

The write plane's counterpart to test_api.py: a TestClient over a file-backed SQLite
app, driving runs through HTTP and confirming they land in the database.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from iotshield.api.control import create_control_app
from iotshield.storage import create_schema, make_engine, make_session_factory, repository

STUB_CSV = (
    "2026-07-07T12:00:00+00:00,pump-02,Pressure,45.0\n"
    "2026-07-07T12:01:00+00:00,pump-02,Pressure,96.0\n"
    "2026-07-07T12:01:30+00:00,pump-02,Pressure,99.0\n"
)
SYSLOG_LINE = "<34>1 2026-07-07T12:00:00Z host-a app 1 ID - disk failure\n"


@dataclass
class ControlHarness:
    client: TestClient
    session_factory: sessionmaker[Session]


@pytest.fixture()
def control(tmp_path: Path) -> Iterator[ControlHarness]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'control.db').as_posix()}")
    create_schema(engine)  # init owns the schema; the write API assumes it exists
    app = create_control_app(engine)
    with TestClient(app) as client:
        yield ControlHarness(client=client, session_factory=make_session_factory(engine))
    engine.dispose()


def spec(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_format": "stub-csv",
        "normalizer": "stub",
        "detectors": [
            {"name": "stub_threshold", "params": {"threshold": 80.0, "lane": "telemetry"}}
        ],
        "alert_engine": {"name": "stub"},
    }
    return base | overrides


class TestHealthAndModules:
    def test_health_ok(self, control: ControlHarness) -> None:
        assert control.client.get("/health").json()["status"] == "ok"

    def test_modules_lists_adapters_with_capabilities_and_params(
        self, control: ControlHarness
    ) -> None:
        body = control.client.get("/modules").json()
        detectors = {d["name"]: d for d in body["detectors"]}
        assert {"stub_threshold", "team3_zscore"} <= set(detectors)
        assert "detections" in detectors["stub_threshold"]["produces"]
        assert {p["name"] for p in detectors["stub_threshold"]["params"]} == {"threshold", "lane"}
        normalizers = {n["name"]: n for n in body["normalizers"]}
        assert "format:syslog" in normalizers["syslog"]["consumes"]
        assert normalizers["syslog"]["formats"] == ["syslog"]  # decoded server-side
        assert {e["name"] for e in body["alert_engines"]} == {"stub", "weighted"}


class TestValidate:
    def test_matching_chain_is_ok_without_issues(self, control: ControlHarness) -> None:
        body = control.client.post("/runs/validate", json={"spec": spec()}).json()
        assert body["ok"] is True and body["issues"] == []

    def test_lane_mismatch_is_ok_with_a_warning(self, control: ControlHarness) -> None:
        body = control.client.post(
            "/runs/validate", json={"spec": spec(source_format="syslog", normalizer="syslog")}
        ).json()
        assert body["ok"] is True
        assert [i["level"] for i in body["issues"]] == ["warning"]

    def test_unsupported_format_is_not_ok(self, control: ControlHarness) -> None:
        body = control.client.post("/runs/validate", json={"spec": spec(source_format="x")}).json()
        assert body["ok"] is False
        assert any(i["level"] == "error" for i in body["issues"])


class TestRuns:
    def test_run_persists_and_returns_summary(self, control: ControlHarness) -> None:
        response = control.client.post("/runs", json={"spec": spec(), "input": STUB_CSV})
        assert response.status_code == 200
        body = response.json()
        assert (body["events"], body["malformed"], body["detections"]) == (3, 0, 2)
        assert body["alerts"] >= 1 and body["top_alerts"]
        # results actually landed in the database (the read API would serve these)
        with control.session_factory() as session:
            assert len(repository.get_events(session)) == 3
            assert len(repository.get_alerts(session)) == body["alerts"]

    def test_incompatible_format_is_422_with_issues(self, control: ControlHarness) -> None:
        response = control.client.post(
            "/runs", json={"spec": spec(source_format="x"), "input": STUB_CSV}
        )
        assert response.status_code == 422
        assert any(i["level"] == "error" for i in response.json()["detail"]["issues"])

    def test_unknown_detector_is_422(self, control: ControlHarness) -> None:
        response = control.client.post(
            "/runs", json={"spec": spec(detectors=[{"name": "nope"}]), "input": STUB_CSV}
        )
        assert response.status_code == 422

    def test_idle_detector_run_succeeds_with_warning(self, control: ControlHarness) -> None:
        # syslog input + telemetry detector: runs, warns, produces a log event, no detections.
        response = control.client.post(
            "/runs",
            json={"spec": spec(source_format="syslog", normalizer="syslog"), "input": SYSLOG_LINE},
        )
        assert response.status_code == 200
        body = response.json()
        assert (body["events"], body["detections"]) == (1, 0)
        assert any("will not fire" in warning for warning in body["warnings"])

    def test_operator_cannot_choose_the_database(self, control: ControlHarness) -> None:
        # database_url is deployment-owned: a spec carrying it is rejected (extra=forbid).
        response = control.client.post(
            "/runs", json={"spec": spec(database_url="sqlite:///evil.db"), "input": STUB_CSV}
        )
        assert response.status_code == 422
