"""Page-level tests via Streamlit's AppTest: the app renders each view against
seeded and empty databases without exceptions, and shows friendly empty states
(the state every fresh `docker compose up` starts in)."""

import subprocess
import sys
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


class TestEvaluateMode:
    """The third plane is opt-in, and reads a report rather than running one (ADR-0021)."""

    def test_mode_is_absent_by_default(
        self, monkeypatch: pytest.MonkeyPatch, empty_client: ApiClient
    ) -> None:
        # A product deployment must never surface the research instrument.
        monkeypatch.delenv(dashboard_app.EVAL_UI_ENV, raising=False)
        at = run_app(monkeypatch, empty_client)
        assert "Evaluate" not in at.sidebar.radio[0].options

    def test_mode_appears_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch, empty_client: ApiClient
    ) -> None:
        monkeypatch.setenv(dashboard_app.EVAL_UI_ENV, "1")
        at = run_app(monkeypatch, empty_client)
        assert "Evaluate" in at.sidebar.radio[0].options

    def _offline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No Pipeline API reachable, so the page takes the saved-report path."""
        unreachable = ControlClient(httpx.Client(base_url="http://127.0.0.1:1"))
        monkeypatch.setattr(dashboard_app, "get_control_client", lambda: unreachable)

    def test_missing_report_explains_how_to_make_one(
        self, monkeypatch: pytest.MonkeyPatch, empty_client: ApiClient, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(dashboard_app.EVAL_UI_ENV, "1")
        monkeypatch.setenv(dashboard_app.EVAL_REPORT_ENV, str(tmp_path / "absent.json"))
        self._offline(monkeypatch)
        at = run_app(monkeypatch, empty_client)
        at.sidebar.radio[0].set_value("Evaluate").run()
        assert not at.exception
        assert any("no saved report" in str(i.value).lower() for i in at.info)

    def test_a_saved_report_renders(
        self, monkeypatch: pytest.MonkeyPatch, empty_client: ApiClient, tmp_path: Path
    ) -> None:
        from iotshield.eval import evaluate, load_corpus
        from iotshield.pipeline import load_spec

        report = evaluate(
            load_corpus("synthetic"), [("team3", load_spec(Path("config/bench/team3.yaml")))]
        )
        report_path = tmp_path / "evaluation.json"
        report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

        monkeypatch.setenv(dashboard_app.EVAL_UI_ENV, "1")
        monkeypatch.setenv(dashboard_app.EVAL_REPORT_ENV, str(report_path))
        self._offline(monkeypatch)
        at = run_app(monkeypatch, empty_client)
        at.sidebar.radio[0].set_value("Evaluate").run()
        assert not at.exception
        assert any("synthetic" in str(c.value) for c in at.caption)

    def test_a_corrupt_report_is_an_empty_state_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, empty_client: ApiClient, tmp_path: Path
    ) -> None:
        report_path = tmp_path / "evaluation.json"
        report_path.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv(dashboard_app.EVAL_UI_ENV, "1")
        monkeypatch.setenv(dashboard_app.EVAL_REPORT_ENV, str(report_path))
        self._offline(monkeypatch)
        at = run_app(monkeypatch, empty_client)
        at.sidebar.radio[0].set_value("Evaluate").run()
        assert not at.exception
        assert any("no saved report" in str(i.value).lower() for i in at.info)


def test_dashboard_does_not_import_the_eval_plane_at_module_scope() -> None:
    """The Evaluate mode is env-gated at runtime; its dependency must be gated too.

    ADR-0021 D1 says the evaluation plane is the first thing you would drop from a product
    image. A top-level `from iotshield.eval...` would make the dashboard fail at startup
    without it, turning the runtime gate into a hard dependency. Checked in a subprocess
    because the rest of this suite has already imported `iotshield.eval`.
    """
    probe = (
        "import sys; import iotshield.dashboard.app; "
        "print(any(m == 'iotshield.eval' or m.startswith('iotshield.eval.') for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


class TestEvaluateRun:
    """The Evaluate mode composes and runs a benchmark through the API (ADR-0021 D6)."""

    def _open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        control_client: ControlClient,
    ) -> AppTest:
        monkeypatch.setenv(dashboard_app.EVAL_UI_ENV, "1")
        monkeypatch.setattr(dashboard_app, "get_control_client", lambda: control_client)
        at = run_app(monkeypatch, empty_client)
        at.sidebar.radio[0].set_value("Evaluate").run()
        assert not at.exception, f"page raised: {at.exception}"
        return at

    def test_the_page_offers_the_committed_corpus_and_detectors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, eval_control_client)
        assert "synthetic" in at.selectbox(key="eval_corpus").options
        # Only telemetry detectors: offering a log detector would build a spec whose format
        # the API rightly refuses over a telemetry corpus.
        options = at.multiselect(key="eval_detectors").options
        assert "stub_threshold" in options and "log_burst" not in options

    def test_running_a_benchmark_renders_charts_and_a_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, eval_control_client)
        # Explicit: which corpus sorts first depends on what has been fetched locally, and
        # the multi-case sections only make sense on a multi-case corpus.
        at.selectbox(key="eval_corpus").set_value("synthetic").run()
        at.multiselect(key="eval_detectors").set_value(["stub_threshold"]).run()
        at.button[0].click().run()
        assert not at.exception
        headings = " ".join(str(h.value) for h in at.subheader)
        assert "Head to head" in headings
        assert "Consistency across cases" in headings  # the per-case variance strip
        assert "What actually happened" in headings  # the timeline
        assert at.dataframe

    def test_the_fit_protocol_is_offered(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, eval_control_client)
        assert set(at.radio(key="eval_protocol").options) == {"none", "prefix", "holdout"}

    def test_an_unreachable_api_falls_back_to_a_saved_report(
        self, monkeypatch: pytest.MonkeyPatch, empty_client: ApiClient, tmp_path: Path
    ) -> None:
        # The CLI -> file -> dashboard path must keep working: the page degrades, never breaks.
        from iotshield.eval import evaluate, load_corpus
        from iotshield.pipeline import load_spec

        report = evaluate(
            load_corpus("synthetic"),
            [("stub", load_spec(Path("config/bench/stub.yaml")))],
            include_timeline=True,
        )
        report_path = tmp_path / "evaluation.json"
        report_path.write_text(report.model_dump_json(), encoding="utf-8")

        unreachable = ControlClient(httpx.Client(base_url="http://127.0.0.1:1"))
        monkeypatch.setenv(dashboard_app.EVAL_UI_ENV, "1")
        monkeypatch.setenv(dashboard_app.EVAL_REPORT_ENV, str(report_path))
        monkeypatch.setattr(dashboard_app, "get_control_client", lambda: unreachable)
        at = run_app(monkeypatch, empty_client)
        at.sidebar.radio[0].set_value("Evaluate").run()
        assert not at.exception
        assert any("unreachable" in str(w.value) for w in at.warning)
