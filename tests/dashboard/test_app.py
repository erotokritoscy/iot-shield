"""Page-level tests via Streamlit's AppTest: the app renders each view against
seeded and empty databases without exceptions, and shows friendly empty states
(the state every fresh `docker compose up` starts in)."""

from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

import iotshield.dashboard.app as dashboard_app
from iotshield.contracts import PrioritizedAlert
from iotshield.dashboard.client import ApiClient
from iotshield.dashboard.control_client import ControlClient
from tests.dashboard.conftest import SeededDashboard

# AppTest runs the entry SCRIPT; it imports main() from the real module, so
# monkeypatching the module's get_client reaches the app under test.
APP_PATH = Path(__file__).parents[2] / "src" / "iotshield" / "dashboard" / "streamlit_app.py"


def run_app(monkeypatch: pytest.MonkeyPatch, client: ApiClient, page: str | None = None) -> AppTest:
    monkeypatch.setattr(dashboard_app, "get_client", lambda: client)
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    at.run()
    if page is not None:
        # radio[0] is Mode (Monitor/Operate); radio[1] is the Monitor View picker.
        at.sidebar.radio[1].set_value(page).run()
    assert not at.exception, f"page raised: {at.exception}"
    return at


class TestTriage:
    def test_seeded_shows_kpis_queue_and_drilldown(
        self, monkeypatch: pytest.MonkeyPatch, seeded: SeededDashboard
    ) -> None:
        at = run_app(monkeypatch, seeded.client)
        # Streamlit stringifies metric values; order: events/detections/alerts/malformed.
        assert [m.value for m in at.metric] == ["3", "1", "1", "1"]
        assert at.selectbox[0].options  # the inspect-alert picker is populated
        rendered = " ".join(str(md.value) for md in at.markdown)
        assert "seeded incident on Feed pump 2" in rendered  # rationale visible

    def test_empty_shows_friendly_message(
        self, monkeypatch: pytest.MonkeyPatch, empty_client: ApiClient
    ) -> None:
        at = run_app(monkeypatch, empty_client)
        assert at.info  # "no alerts yet" guidance, not a stack trace


class TestAssets:
    def test_seeded_renders_timeline_and_detections(
        self, monkeypatch: pytest.MonkeyPatch, seeded: SeededDashboard
    ) -> None:
        at = run_app(monkeypatch, seeded.client, page="Assets & telemetry")
        assert at.selectbox[0].options  # asset picker populated
        captions = " ".join(str(c.value) for c in at.caption)
        assert "criticality: high" in captions
        assert "detection windows" in captions.lower()  # chart caption present

    def test_empty_shows_friendly_message(
        self, monkeypatch: pytest.MonkeyPatch, empty_client: ApiClient
    ) -> None:
        at = run_app(monkeypatch, empty_client, page="Assets & telemetry")
        assert at.info


class TestDataHealth:
    def test_seeded_shows_malformed_audit(
        self, monkeypatch: pytest.MonkeyPatch, seeded: SeededDashboard
    ) -> None:
        at = run_app(monkeypatch, seeded.client, page="Data health")
        assert at.subheader
        assert len(at.dataframe) == 1  # the malformed table

    def test_empty_shows_success_state(
        self, monkeypatch: pytest.MonkeyPatch, empty_client: ApiClient
    ) -> None:
        at = run_app(monkeypatch, empty_client, page="Data health")
        assert at.success  # "no malformed inputs recorded"


def test_unreachable_api_shows_error_not_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    dead = ApiClient.from_url("http://127.0.0.1:9")  # nothing listens here
    monkeypatch.setattr(dashboard_app, "get_client", lambda: dead)
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    at.run()
    assert not at.exception
    assert at.error  # friendly "API unreachable" with the start command


def test_endpoint_failure_mid_page_shows_error_not_traceback(
    monkeypatch: pytest.MonkeyPatch, seeded: SeededDashboard
) -> None:
    # Health passes but a page endpoint fails (single route 5xx / API dying
    # mid-rerun) — the page-dispatch boundary must render st.error, not raise.
    def explode(*args: object, **kwargs: object) -> object:
        raise httpx.ConnectError("connection lost mid-rerun")

    monkeypatch.setattr(type(seeded.client), "alerts", explode)
    monkeypatch.setattr(dashboard_app, "get_client", lambda: seeded.client)
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    at.run()
    assert not at.exception
    assert at.error


def test_contract_drift_mid_page_shows_error_not_traceback(
    monkeypatch: pytest.MonkeyPatch, seeded: SeededDashboard
) -> None:
    # A response that no longer validates against a contract (API/dashboard skew)
    # raises ValidationError — the very failure the typed client exists to
    # surface. The boundary must render st.error, not a traceback.
    def explode(*args: object, **kwargs: object) -> object:
        return PrioritizedAlert.model_validate({})  # missing fields -> ValidationError

    monkeypatch.setattr(type(seeded.client), "alerts", explode)
    monkeypatch.setattr(dashboard_app, "get_client", lambda: seeded.client)
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    at.run()
    assert not at.exception
    assert at.error


class TestCompose:
    """The orchestration page: compose a chain, see it validated, trigger a run."""

    def _open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        control_client: ControlClient,
    ) -> AppTest:
        # The compose page uses only the Pipeline API; a healthy (empty) read client
        # keeps the initial Triage render clean before we switch pages.
        monkeypatch.setattr(dashboard_app, "get_client", lambda: empty_client)
        monkeypatch.setattr(dashboard_app, "get_control_client", lambda: control_client)
        at = AppTest.from_file(str(APP_PATH), default_timeout=30)
        at.run()
        at.sidebar.radio[0].set_value("Operate").run()  # Mode: Monitor -> Operate
        assert not at.exception
        return at

    def test_default_chain_validates_as_compatible(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, control_client)
        assert any("compatible" in str(s.value).lower() for s in at.success)

    def test_default_detector_follows_the_default_source_lane(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        control_client: ControlClient,
    ) -> None:
        # The default source is the telemetry CSV normalizer, so the preselected detector
        # must be a telemetry one — not merely whichever adapter name sorts first, which
        # would open the page on a lane-mismatched chain now that log detectors exist.
        at = self._open(monkeypatch, empty_client, control_client)
        (picked,) = at.multiselect(key="compose_detectors").value
        assert picked == "stub_threshold"

    def test_run_button_triggers_a_pipeline_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, control_client)
        at.text_area[0].set_value("2026-07-07T12:00:00+00:00,pump-02,Pressure,96.0").run()
        at.button[0].click().run()
        assert not at.exception
        assert any("Run complete" in str(s.value) for s in at.success)
        # events, malformed, detections, alerts for the single over-threshold reading
        assert [m.value for m in at.metric] == ["1", "0", "1", "1"]
