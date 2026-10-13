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
from iotshield.detect.rules.loader import RULES_PATH_ENV
from iotshield.storage import (
    DEFAULT_ORG_ID,
    create_schema,
    make_engine,
    make_session_factory,
    repository,
)

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
    app = create_control_app(engine, auth=False)
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
        sinks = {s["name"]: s for s in body["sinks"]}
        assert set(sinks) == {"postgres", "file"}
        assert sinks["postgres"]["params"] == []  # the injected 'resources' is not a param
        assert {p["name"] for p in sinks["file"]["params"]} == {"path"}

    def test_modules_report_closed_vocabularies_as_choices(self, control: ControlHarness) -> None:
        # A param the factory annotates Literal[...] carries its allowed values, so the UI
        # can offer them instead of accepting free text (ADR-0023 D9).
        body = control.client.get("/modules").json()
        params = {
            p["name"]: p for d in body["detectors"] if d["name"] == "log_burst" for p in d["params"]
        }
        assert params["preset"]["choices"] == ["severity_spike", "auth_failure"]
        assert params["window_minutes"]["choices"] is None  # open-ended: no vocabulary

    def test_an_optional_closed_vocabulary_still_reports_its_choices(
        self, control: ControlHarness
    ) -> None:
        # `Literal[...] | None` is the shape every param takes when "not given" has to be
        # expressible (ADR-0022's sentinel). The type name unwrapped the union and the
        # choices did not, so the meter normalizer's utility published `type: "Literal"`
        # with `choices: null` and a UI had nothing to render but a free-text box — for a
        # two-value vocabulary whose whole point is that it cannot be typed wrong.
        body = control.client.get("/modules").json()
        params = {
            p["name"]: p for n in body["normalizers"] if n["name"] == "meter" for p in n["params"]
        }
        assert params["meter_type"]["choices"] == ["water", "electricity"]
        assert params["meter_type"]["type"] == "str"
        # The sibling param on the same factory is genuinely open-ended: a timezone name.
        assert params["source_timezone"]["choices"] is None
        assert params["source_timezone"]["type"] == "str"

    def test_a_rule_set_added_while_running_is_offered_without_a_restart(
        self, control: ControlHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The rule shelf as a closed vocabulary (ADR-0037), asserted **over HTTP** because that
        is where it was false.

        ``AdapterSpec.discovered`` holds callables so this vocabulary is read per call, but
        `/modules` served a response built at *import* time: every layer underneath resolved the
        shelf freshly and the one a client actually reads was frozen at process start. A unit
        test on the helper passes either way — only a second request, after a change to the
        shelf, can tell the difference. The full list is asserted, not just membership, so this
        pins the offer an operator sees rather than that something was offered.
        """
        monkeypatch.setenv(RULES_PATH_ENV, str(tmp_path))

        def offered() -> dict[str, Any]:
            body = control.client.get("/modules").json()
            rules = next(d for d in body["detectors"] if d["name"] == "rules")
            return next(p for p in rules["params"] if p["name"] == "ruleset")

        assert offered()["default"] == "packaged"
        assert offered()["choices"] == ["packaged"]
        (tmp_path / "site-rules").mkdir()
        (tmp_path / "site-rules" / "rule.yml").write_text("{}", encoding="utf-8")
        assert offered()["choices"] == ["packaged", "site-rules"]


class TestValidate:
    def test_matching_chain_is_ok_without_issues(self, control: ControlHarness) -> None:
        body = control.client.post("/runs/validate", json={"spec": spec()}).json()
        assert body["ok"] is True and body["issues"] == []

    def test_lane_mismatch_is_ok_with_a_warning(self, control: ControlHarness) -> None:
        body = control.client.post(
            "/runs/validate", json={"spec": spec(source_format="syslog", normalizer="syslog")}
        ).json()
        assert body["ok"] is True
        # Idle telemetry detector + uncovered log lane: two warnings, still ok.
        assert [i["level"] for i in body["issues"]] == ["warning", "warning"]

    def test_unsupported_format_is_not_ok(self, control: ControlHarness) -> None:
        body = control.client.post("/runs/validate", json={"spec": spec(source_format="x")}).json()
        assert body["ok"] is False
        assert any(i["level"] == "error" for i in body["issues"])


class TestRuns:
    def test_run_persists_and_returns_summary(self, control: ControlHarness) -> None:
        response = control.client.post(
            "/runs", json={"spec": spec(), "inputs": [{"content": STUB_CSV}]}
        )
        assert response.status_code == 200
        body = response.json()
        assert (body["events"], body["malformed"], body["detections"]) == (3, 0, 2)
        assert body["alerts"] >= 1 and body["top_alerts"]
        # results actually landed in the database (the read API would serve these)
        with control.session_factory() as session:
            assert len(repository.get_events(session, org_id=DEFAULT_ORG_ID)) == 3
            assert len(repository.get_alerts(session, org_id=DEFAULT_ORG_ID)) == body["alerts"]

    def test_mixed_modality_run_pools_two_sources(self, control: ControlHarness) -> None:
        # One request, two sources of different formats: a telemetry CSV + a syslog line,
        # routed to their normalizers and pooled into the two lanes (ADR-0019).
        multi_spec = {
            "sources": [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {"normalizer": "syslog", "source_format": "syslog"},
            ],
            "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
            "alert_engine": {"name": "stub"},
        }
        response = control.client.post(
            "/runs",
            json={
                "spec": multi_spec,
                "inputs": [{"content": STUB_CSV}, {"content": SYSLOG_LINE}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        # 3 telemetry + 1 log = 4 events; the telemetry spikes still detect; log lane uncovered.
        assert body["events"] == 4
        assert body["detections"] == 2
        assert any("no detector that consumes it" in w for w in body["warnings"])

    def test_inputs_must_align_with_sources(self, control: ControlHarness) -> None:
        # Two sources declared but one input supplied: refused before any run side effect.
        two_source_spec = {
            "sources": [
                {"normalizer": "stub", "source_format": "stub-csv"},
                {"normalizer": "syslog", "source_format": "syslog"},
            ],
            "detectors": [{"name": "stub_threshold"}],
            "alert_engine": {"name": "stub"},
        }
        response = control.client.post(
            "/runs", json={"spec": two_source_spec, "inputs": [{"content": STUB_CSV}]}
        )
        assert response.status_code == 422
        assert "match spec.sources" in response.json()["detail"]["message"]

    def test_a_bad_param_value_is_422_carrying_the_adapters_own_message(
        self, control: ControlHarness
    ) -> None:
        """The guard reads declared capabilities without building adapters, so a bad param
        *value* legitimately passes validation and is only refused at construction. That is
        a caller error, not a server fault — it used to escape as a bare 500, and the
        adapter's carefully worded reason reached nobody.
        """
        bad = spec(detectors=[{"name": "log_burst", "params": {"min_count": 1}}])
        assert control.client.post("/runs/validate", json={"spec": bad}).json()["ok"] is True

        response = control.client.post(
            "/runs", json={"spec": bad, "inputs": [{"content": STUB_CSV}]}
        )
        assert response.status_code == 422
        assert "min_count must be >= 2" in response.json()["detail"]["message"]

    def test_a_run_cannot_name_a_rule_directory_on_the_server(
        self, control: ControlHarness, tmp_path: Path
    ) -> None:
        """ADR-0037. A ``PipelineSpec`` is the same object whether it was read from a YAML file
        or posted to this endpoint, so ``ruleset`` honoured as a path let any caller aim the
        rule loader at a directory and read the parse errors — which quote the offending file.

        Rule sets are named against the server's own shelf instead, and a name that is not on
        it is refused without the shelf ever being left.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "private.yaml").write_text("token: s3cr3t\n", encoding="utf-8")

        bad = spec(detectors=[{"name": "rules", "params": {"ruleset": str(outside)}}])
        response = control.client.post(
            "/runs", json={"spec": bad, "inputs": [{"content": STUB_CSV}]}
        )
        assert response.status_code == 422
        message = response.json()["detail"]["message"]
        assert "unknown rule set" in message
        assert "s3cr3t" not in message
        assert "packaged" in message  # what it could have named instead

    def test_a_bad_param_value_is_refused_before_anything_is_written(
        self, control: ControlHarness
    ) -> None:
        bad = spec(detectors=[{"name": "log_burst", "params": {"min_count": 1}}])
        control.client.post("/runs", json={"spec": bad, "inputs": [{"content": STUB_CSV}]})
        with control.session_factory() as session:
            assert list(repository.get_events(session, org_id=DEFAULT_ORG_ID)) == []

    def test_incompatible_format_is_422_with_issues(self, control: ControlHarness) -> None:
        response = control.client.post(
            "/runs", json={"spec": spec(source_format="x"), "inputs": [{"content": STUB_CSV}]}
        )
        assert response.status_code == 422
        assert any(i["level"] == "error" for i in response.json()["detail"]["issues"])

    def test_unknown_detector_is_422(self, control: ControlHarness) -> None:
        response = control.client.post(
            "/runs",
            json={"spec": spec(detectors=[{"name": "nope"}]), "inputs": [{"content": STUB_CSV}]},
        )
        assert response.status_code == 422

    def test_idle_detector_run_succeeds_with_warning(self, control: ControlHarness) -> None:
        # syslog input + telemetry detector: runs, warns, produces a log event, no detections.
        response = control.client.post(
            "/runs",
            json={
                "spec": spec(source_format="syslog", normalizer="syslog"),
                "inputs": [{"content": SYSLOG_LINE}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert (body["events"], body["detections"]) == (1, 0)
        assert any("will not fire" in warning for warning in body["warnings"])

    def test_operator_cannot_choose_the_database(self, control: ControlHarness) -> None:
        # database_url is deployment-owned: a spec carrying it is rejected (extra=forbid).
        response = control.client.post(
            "/runs",
            json={
                "spec": spec(database_url="sqlite:///evil.db"),
                "inputs": [{"content": STUB_CSV}],
            },
        )
        assert response.status_code == 422
