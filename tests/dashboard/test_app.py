"""Page-level tests via Streamlit's AppTest: the app renders each view against
seeded and empty databases without exceptions, and shows friendly empty states
(the state every fresh `docker compose up` starts in)."""

from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

import iotshield.dashboard.app as dashboard_app
from iotshield.dashboard.client import ApiClient
from tests.dashboard.conftest import SeededDashboard

# AppTest runs the entry SCRIPT; it imports main() from the real module, so
# monkeypatching the module's get_client reaches the app under test.
APP_PATH = Path(__file__).parents[2] / "src" / "iotshield" / "dashboard" / "streamlit_app.py"


def run_app(monkeypatch: pytest.MonkeyPatch, client: ApiClient, page: str | None = None) -> AppTest:
    monkeypatch.setattr(dashboard_app, "get_client", lambda: client)
    at = AppTest.from_file(str(APP_PATH), default_timeout=15)
    at.run()
    if page is not None:
        at.sidebar.radio[0].set_value(page).run()
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
