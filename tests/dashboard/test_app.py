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

    def test_evidence_is_named_by_what_it_found_not_by_which_detector_found_it(
        self, monkeypatch: pytest.MonkeyPatch, rule_seeded: ApiClient
    ) -> None:
        """The regression ADR-0032 D3 opened, and the two fields that were invisible.

        Six rules share one ``method``, so a UI that names evidence by method renders
        several identical headings for genuinely different findings — three of them on
        the walkthrough's gateway alert. The fix is the sentence D7 requires of
        every detection, which is also what makes this assertable at all.
        """
        at = run_app(monkeypatch, rule_seeded)
        rendered = " ".join(str(md.value) for md in at.markdown)

        # Both rows report `rules:flow`, so the method column cannot tell them apart...
        evidence = at.dataframe[1].value  # [0] is the ranked queue
        assert list(evidence["method"]) == ["rules:flow", "rules:flow"]
        # ...and the finding column can. The column, not the frame's repr: these sentences
        # are long enough that pandas elides them, which would pass on truncated text.
        assert list(evidence["finding"]) == [
            "The device originated a connection to port 23 (telnet).",
            "Outbound bytes on one flow exceed 10000.",
        ]
        # The same sentence names each disclosure, which is what the analyst clicks.
        assert [e.label for e in at.expander] == [
            "The device originated a connection to port 23 (telnet). — rules:flow",
            "Outbound bytes on one flow exceed 10000. — rules:flow",
        ]

        # The unioned next steps are a checklist, not a paragraph (D5).
        assert "- Isolate the device." in rendered
        assert "- Review the destination." in rendered

        # Every technique the evidence cites, tactic included; `threat_class` carries one.
        assert "Lateral Movement — Remote Services (T1021)" in rendered
        assert "Exfiltration — Exfiltration Over C2 Channel (T1041)" in rendered


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


def _catalogue_params(control_client: ControlClient, name: str = "team3_zscore") -> list:
    """One detector's params exactly as /modules reports them to the page."""
    return next(m.params for m in control_client.modules().detectors if m.name == name)


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
        assert at.selectbox(key="det_0_name").value == "stub_threshold"

    def test_the_same_adapter_can_be_configured_twice_with_different_params(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        control_client: ControlClient,
    ) -> None:
        """The point of rendering detectors as a list (ADR-0023).

        The old multiselect was over *names*, so this was structurally impossible from the
        UI: one adapter meant one instance, hence one preset per run. Two rows of the same
        adapter with different params is exactly how two presets run together.
        """
        at = self._open(monkeypatch, empty_client, control_client)
        at.number_input(key="compose_det_n").set_value(2).run()

        at.selectbox(key="det_0_name").set_value("log_burst").run()
        at.selectbox(key="det_1_name").set_value("log_burst").run()
        at.selectbox(key="det_0_preset").set_value("severity_spike").run()
        at.selectbox(key="det_1_preset").set_value("auth_failure").run()
        assert not at.exception

        # Both rows exist, name the same adapter, and carry different presets — the widget
        # keys no longer collide, which is the whole fix.
        assert at.selectbox(key="det_0_name").value == at.selectbox(key="det_1_name").value
        assert at.selectbox(key="det_0_preset").value != at.selectbox(key="det_1_preset").value

    def test_an_untouched_detector_row_sends_no_parameters(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        control_client: ControlClient,
    ) -> None:
        """A form must be able to say "not given", and a widget cannot.

        `describe_params` back-fills real defaults, so every widget renders populated.
        Submitting all of them makes each run an explicit request for every parameter, which is
        wrong wherever "not given" carries meaning — and for `log_burst` it does twice over,
        since `preset` owns the real window and count (ADR-0024 D3).
        """
        at = self._open(monkeypatch, empty_client, control_client)
        at.selectbox(key="det_0_name").set_value("log_burst").run()
        assert not at.exception

        params = dashboard_app._param_widgets(
            "det_0", _catalogue_params(control_client, "log_burst")
        )
        assert params == {}, f"untouched row submitted: {params}"

    def test_a_learning_detector_offers_its_artifact_and_no_hyperparameters(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        control_client: ControlClient,
    ) -> None:
        """A run never fits, so an artifact is the only thing that can configure one (ADR-0036).

        It supplies the hyperparameters its weights were fitted with, and naming one beside it
        is refused at construction as a half-configured request. Offering the knobs anyway made
        every one of them a trap — moving a single knob turned a working chain into a failed run
        — and the page only worked because `_untouched` drops what nobody moved.
        """
        at = self._open(monkeypatch, empty_client, control_client)
        at.selectbox(key="det_0_name").set_value("team3_zscore").run()
        assert not at.exception

        # The ids the adapter ships, offered rather than typed from memory (ADR-0035 D1), and
        # preselected because here an artifact is mandatory rather than optional.
        artifact = at.selectbox(key="det_0_art")
        assert list(artifact.options) == ["skab_zscore"]
        assert artifact.value == "skab_zscore"

        # ...and not one hyperparameter widget beside it.
        rendered = {widget.key for widget in [*at.number_input, *at.selectbox, *at.text_input]}
        assert rendered.isdisjoint({"det_0_window_size", "det_0_percentile", "det_0_channels"})

    def test_a_changed_parameter_is_still_sent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        control_client: ControlClient,
    ) -> None:
        # The other half: omitting untouched values must not make the form unable to speak.
        at = self._open(monkeypatch, empty_client, control_client)
        at.selectbox(key="det_0_name").set_value("log_burst").run()
        at.selectbox(key="det_0_preset").set_value("auth_failure").run()
        assert at.selectbox(key="det_0_preset").value == "auth_failure"

    def test_preset_is_offered_as_a_choice_not_a_free_text_box(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        control_client: ControlClient,
    ) -> None:
        # A closed vocabulary declared on the factory reaches the operator as options.
        at = self._open(monkeypatch, empty_client, control_client)
        at.selectbox(key="det_0_name").set_value("log_burst").run()
        assert set(at.selectbox(key="det_0_preset").options) == {"severity_spike", "auth_failure"}

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
        mode: str = dashboard_app._COMPOSE_SPECS,
    ) -> AppTest:
        """Open the Evaluate page, in the compose mode most of these tests exercise.

        The page *opens* on "run a committed bench spec", because that is the mode whose
        numbers a reader can reproduce from the CLI — the page composing its own lookalike
        spec is what let the two surfaces publish different numbers for the same benchmark.
        The entry widgets below belong to the other mode, so the switch is explicit here.
        """
        monkeypatch.setenv(dashboard_app.EVAL_UI_ENV, "1")
        monkeypatch.setattr(dashboard_app, "get_control_client", lambda: control_client)
        at = run_app(monkeypatch, empty_client)
        at.sidebar.radio[0].set_value("Evaluate").run()
        assert not at.exception, f"page raised: {at.exception}"
        if mode != dashboard_app._SHIPPED_SPECS:
            at.radio(key="eval_spec_mode").set_value(mode).run()
            assert not at.exception, f"page raised: {at.exception}"
        return at

    def test_it_opens_on_a_committed_bench_spec_that_matches_the_corpus(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        """The reproducibility mode, and the page's default.

        The page used to compose a `PipelineSpec` from the detectors on screen and nothing
        else, which silently dropped the `assets:` block and every source and engine
        parameter — so `config/bench/stub.yaml` and the page's reconstruction of it published
        different priority scores (39.46 vs 35.72) with nothing on either surface disclosing
        which spec had run.
        """
        at = self._open(
            monkeypatch, empty_client, eval_control_client, mode=dashboard_app._SHIPPED_SPECS
        )
        at.selectbox(key="eval_corpus").set_value("synthetic").run()
        offered = at.multiselect(key="eval_bench_specs").options
        # Only specs whose source shape matches the corpus: offering the rest would put a
        # guaranteed alignment failure in a dropdown.
        assert "stub" in offered and "flow" not in offered

        # ... and the pairing follows the corpus rather than being a fixed list. `fleet_scan`
        # now fits two committed specs, which is what makes it a head-to-head (ADR-0032 D8):
        # authored signatures against a statistical burst preset, same corpus, same bytes.
        at.selectbox(key="eval_corpus").set_value("fleet_scan").run()
        assert at.multiselect(key="eval_bench_specs").options == ["flow", "rules_flow"]

    def test_a_shipped_spec_runs_verbatim_and_the_page_shows_what_it_sent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        at = self._open(
            monkeypatch, empty_client, eval_control_client, mode=dashboard_app._SHIPPED_SPECS
        )
        at.selectbox(key="eval_corpus").set_value("synthetic").run()
        at.multiselect(key="eval_bench_specs").set_value(["stub"]).run()
        at.button[0].click().run()
        assert not at.exception
        assert "stub" in str(at.dataframe[0].value)
        # The exact request is on the page that made it, which is what makes a screenshot and
        # a CLI number reconcilable without reading the dashboard's source.
        assert at.json

    def test_the_page_offers_the_committed_corpus_and_detectors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, eval_control_client)
        assert "synthetic" in at.selectbox(key="eval_corpus").options
        assert "stub_threshold" in at.multiselect(key="eval_e0_dets").options

    def test_the_detector_roster_follows_the_corpus_lanes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        """Eligibility is the corpus's declared shape — both axes of it — not a hardcoded lane.

        This filter was pinned to telemetry, which was right only until ADR-0026 shipped a
        flow corpus and a trimodal one. The cost was not a missing option: the page offered
        `stub_threshold` over `fleet_scan`, built a spec whose flow events it cannot consume,
        and reported a clean row of zeros that reads exactly like a bad detector. The old
        version of this test asserted that behaviour was correct.

        So did the previous version of *this* test, one axis further in (ADR-0035 D2). It
        expected `meter_iforest` on `synthetic` and called that the ADR-0026 amendment working
        as intended — "a silent detector is a real measurement, not an option withheld". That
        principle is right and this is not an instance of it. The line is whether the
        detector's answer can vary with the data:

        - `severity_spike` on a burst of INFO lines *evaluates* every event and correctly
          finds nothing. Different logs, different answer. That is a measurement, and
          withholding it would prevent making it.
        - `meter_iforest` on `stub-csv` telemetry cannot read a single event, whatever they
          say, because none carries a `MeterReading`. Its answer is forced by the shape of
          the corpus rather than by its contents. That is not a measurement.

        Which is the same rule the lane filter already applied, one level finer — nobody
        called withholding `stub_threshold` from `fleet_scan` a suppressed measurement.
        `team3_zscore` stays offered on `synthetic` precisely because it is the first kind:
        right lane, no payload needed, and an artifact whose channels simply do not cover
        this rig — a real finding about domain shift, reported by `why_silent`.
        """
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.selectbox(key="eval_corpus").set_value("fleet_scan").run()
        # `rules` defaults to the FLOW lane (ADR-0032 D3), so it is eligible here and absent
        # from the telemetry roster below — the roster follows each adapter's declared
        # capability under its default params, which is exactly the point of the filter.
        assert at.multiselect(key="eval_e0_dets").options == ["flow_burst", "rules"]

        # Switching back must prune the stale flow pick rather than raising: Streamlit keeps a
        # widget's value across the change, and `flow_burst` is not an option here.
        at.selectbox(key="eval_corpus").set_value("synthetic").run()
        assert not at.exception
        # Telemetry detectors that need no lane vocabulary. `meter_iforest` is absent because
        # this corpus carries no meter payload; `team3_zscore` is present because nothing
        # about the corpus stops it judging — see the docstring for why those are different.
        assert at.multiselect(key="eval_e0_dets").options == ["stub_threshold", "team3_zscore"]

    def test_one_entry_can_hold_several_detectors_that_run_together(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        """The entry, not the detector, is the unit of comparison.

        One detector per entry is head to head; several in one entry is a single pipeline
        whose detections pool into one alert-engine run. Only the second shape can produce
        cross-lane corroboration, and with a detector per spec it was unreachable from the UI
        at every setting.
        """
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.selectbox(key="eval_corpus").set_value("device_trimodal").run()
        at.number_input(key="eval_entry_n").set_value(1).run()
        at.multiselect(key="eval_e0_dets").set_value(
            ["stub_threshold", "log_burst", "flow_burst"]
        ).run()
        at.button[0].click().run()
        assert not at.exception
        # One row, labelled by its members, judging all three lanes in one pipeline.
        rendered = str(at.dataframe[0].value)
        assert "stub_threshold + log_burst + flow_burst" in rendered

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
        at.multiselect(key="eval_e0_dets").set_value(["stub_threshold"]).run()
        at.button[0].click().run()
        assert not at.exception
        headings = " ".join(str(h.value) for h in at.subheader)
        assert "Head to head" in headings
        assert "Consistency across cases" in headings  # the per-case variance strip
        assert "What actually happened" in headings  # the timeline
        assert at.dataframe

    def test_a_learning_detector_offers_weights_mode_and_its_hyperparameters(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        """The ADR-0022 split, as the page's central choice.

        Two exclusive modes rather than one form: an artifact is atomic, so a page that
        let someone edit a hyperparameter beside an artifact id would be building the one
        request construction refuses.
        """
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.multiselect(key="eval_e0_dets").set_value(["team3_zscore"]).run()
        assert set(at.radio(key="eval_e0_team3_zscore_mode").options) == {
            dashboard_app._TRAINED,
            dashboard_app._SCRATCH,
        }
        # Trained mode shows the artifact id and NOT the hyperparameters. The id is chosen
        # from the ids the adapter actually ships (ADR-0035 D1), not typed from memory.
        artifact = at.selectbox(key="eval_e0_team3_zscore_art")
        assert artifact.options == ["skab_zscore"]
        assert artifact.value == "skab_zscore"
        assert "eval_e0_team3_zscore_window_size" not in {w.key for w in at.number_input}

    def test_fit_from_scratch_exposes_hyperparameters_prefilled_with_real_defaults(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.multiselect(key="eval_e0_dets").set_value(["team3_zscore"]).run()
        at.radio(key="eval_e0_team3_zscore_mode").set_value(dashboard_app._SCRATCH).run()
        # 60, not 0: the factory param defaults to None (the "not given" sentinel), so the
        # real default has to come from the adapter's hyperparameter model via /eval/detectors.
        assert at.number_input(key="eval_e0_team3_zscore_window_size").value == 60

    def test_the_artifact_control_cannot_express_an_untrained_detector(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        """A blank artifact reaches the factory as "no artifact", which ADR-0022 means as
        *untrained* — and a run refuses an untrained detector outright.

        This used to be enforced after the fact: the box was free text, clearing it produced a
        blocker, and the operator's way out was to type an id nothing had told them. Offering
        the shipped ids (ADR-0035 D1) removes the state instead of catching it — every value
        the control can hold is one `load_artifact` will resolve. The blocker still exists for
        an adapter that declares an artifact param and ships nothing, which is the only case
        that can still reach the text fallback.
        """
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.multiselect(key="eval_e0_dets").set_value(["team3_zscore"]).run()
        assert "eval_e0_team3_zscore_art" not in {w.key for w in at.text_input}
        assert "" not in at.selectbox(key="eval_e0_team3_zscore_art").options
        assert not any("no artifact is named" in str(e.value) for e in at.error)
        assert not at.button[0].disabled

    def test_fitting_from_scratch_under_protocol_none_blocks_the_run_and_says_why(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        # An untrained model under `none` judges nothing; a row of zeros would read as
        # "this detector is bad" when the truth is that it never ran.
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.multiselect(key="eval_e0_dets").set_value(["team3_zscore"]).run()
        at.radio(key="eval_e0_team3_zscore_mode").set_value(dashboard_app._SCRATCH).run()
        assert at.radio(key="eval_protocol").value == "none"
        assert any("prefix" in str(e.value) for e in at.error)
        assert at.button[0].disabled

    def test_a_sweep_is_offered_and_reports_how_many_runs_it_costs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.multiselect(key="eval_e0_dets").set_value(["stub_threshold"]).run()
        sweep = at.selectbox(key="eval_e0_sw")
        assert "threshold" in sweep.options
        sweep.set_value("threshold").run()
        at.text_input(key="eval_e0_swv").set_value("40, 80, 120").run()
        assert any("3 run(s)" in str(c.value) for c in at.caption)
        assert not at.exception

    def test_a_swept_benchmark_runs_and_renders_a_row_per_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.selectbox(key="eval_corpus").set_value("synthetic").run()
        at.multiselect(key="eval_e0_dets").set_value(["stub_threshold"]).run()
        at.selectbox(key="eval_e0_sw").set_value("threshold").run()
        at.text_input(key="eval_e0_swv").set_value("40, 80").run()
        at.button[0].click().run()
        assert not at.exception
        rendered = str(at.dataframe[0].value)
        assert "stub_threshold[threshold=40" in rendered
        assert "stub_threshold[threshold=80" in rendered

    def test_a_multi_detector_entry_offers_every_knob_qualified_by_detector(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        """Both detectors declare `threshold`, and both are now reachable.

        The menu used to *hide* any name two of the entry's detectors shared, mirroring a
        server refusal, so it silently shrank from four options to one. Qualifying by detector
        removes the ambiguity instead of routing around it.
        """
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.selectbox(key="eval_corpus").set_value("device_trimodal").run()
        at.number_input(key="eval_entry_n").set_value(1).run()
        at.multiselect(key="eval_e0_dets").set_value(["stub_threshold", "flow_burst"]).run()
        assert at.selectbox(key="eval_e0_sw").options == [
            dashboard_app._NO_SWEEP,
            "stub_threshold.threshold",
            "flow_burst.window_minutes",
            "flow_burst.threshold",
        ]

    def test_a_single_detector_entry_keeps_bare_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        # The compatibility pin, at the UI end: labels like `stub_threshold[threshold=40]` are
        # quoted in the README and the ADRs, so the common case must not gain a qualifier.
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.selectbox(key="eval_corpus").set_value("synthetic").run()
        at.number_input(key="eval_entry_n").set_value(1).run()
        at.multiselect(key="eval_e0_dets").set_value(["stub_threshold"]).run()
        assert at.selectbox(key="eval_e0_sw").options == [dashboard_app._NO_SWEEP, "threshold"]

    def test_invalidating_a_sweep_warns_instead_of_silently_clearing_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        """Editing an entry can strip the swept key from the menu.

        Streamlit resets a selectbox whose stored value is gone — silently, unlike a
        multiselect, which raises. So a configured sweep vanished with no message and the run
        then measured an *unswept* spec: the user gets a number they did not ask for.
        """
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.selectbox(key="eval_corpus").set_value("device_trimodal").run()
        at.number_input(key="eval_entry_n").set_value(1).run()
        at.multiselect(key="eval_e0_dets").set_value(["flow_burst"]).run()
        at.selectbox(key="eval_e0_sw").set_value("threshold").run()
        at.text_input(key="eval_e0_swv").set_value("10, 20").run()

        # Adding a detector re-keys the menu to qualified names, so bare `threshold` is gone.
        at.multiselect(key="eval_e0_dets").set_value(["flow_burst", "stub_threshold"]).run()
        assert not at.exception
        assert any("no longer applies" in str(w.value) for w in at.warning)
        assert at.selectbox(key="eval_e0_sw").value == dashboard_app._NO_SWEEP

    def test_a_qualified_sweep_runs_and_labels_each_row_by_detector(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.selectbox(key="eval_corpus").set_value("device_trimodal").run()
        at.number_input(key="eval_entry_n").set_value(1).run()
        at.multiselect(key="eval_e0_dets").set_value(["stub_threshold", "flow_burst"]).run()
        at.selectbox(key="eval_e0_sw").set_value("flow_burst.threshold").run()
        at.text_input(key="eval_e0_swv").set_value("10, 30").run()
        at.button[0].click().run()
        assert not at.exception
        # The column, not the frame's repr: these labels are long enough that pandas elides
        # them, which would make the assertion pass on truncated text.
        assert list(at.dataframe[0].value["spec"]) == [
            "stub_threshold + flow_burst[flow_burst.threshold=10]",
            "stub_threshold + flow_burst[flow_burst.threshold=30]",
        ]

    def test_trained_mode_says_why_it_offers_no_knobs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        """An artifact is atomic, so trained mode contributes nothing to the sweep menu.

        Correct, and previously invisible: the radio that controls it sits inside a collapsed
        expander, so the menu appeared to gain three options for no reason.
        """
        at = self._open(monkeypatch, empty_client, eval_control_client)
        at.selectbox(key="eval_corpus").set_value("synthetic").run()
        at.multiselect(key="eval_e0_dets").set_value(["team3_zscore"]).run()
        assert any("atomic" in str(c.value) for c in at.caption)
        # And switching to scratch mode makes its hyperparameters sweepable.
        at.radio(key="eval_e0_team3_zscore_mode").set_value(dashboard_app._SCRATCH).run()
        assert "window_size" in at.selectbox(key="eval_e0_sw").options

    def test_the_fit_protocol_is_offered(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_client: ApiClient,
        eval_control_client: ControlClient,
    ) -> None:
        at = self._open(monkeypatch, empty_client, eval_control_client)
        assert set(at.radio(key="eval_protocol").options) == {"none", "prefix"}

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


class TestBenchSpecMatchesEveryCorpusShape:
    """The Evaluate page composes a spec on the user's behalf, so it must compose a valid one.

    Its sources used to be a hardcoded single stub-csv entry, which was right only while every
    corpus was single-lane telemetry. A benchmark checks a case's inputs against the spec's
    `sources` positionally and by format (`runner._check_alignment`), so a flow or multi-lane
    corpus would be offered in the picker and then refused on run — worse than not shipping it
    (ADR-0026).
    """

    def _normalizers(self) -> dict[str, object]:
        """The catalogue exactly as the API builds it, so the page is tested against the wire
        shape it really receives rather than a stand-in that could drift from it."""
        from iotshield.api.control import _module_info
        from iotshield.pipeline.registry import NORMALIZERS

        return {name: _module_info("normalizer", name, spec) for name, spec in NORMALIZERS.items()}

    def _detector(self, name: str) -> dict[str, object]:
        return {"name": name, "params": {}}

    def test_it_builds_one_source_per_declared_format(self) -> None:
        spec = dashboard_app._composed_spec(
            [self._detector("flow_burst")],
            "weighted",
            ["stub-csv", "syslog", "flow-csv"],
            self._normalizers(),
        )
        assert [source["source_format"] for source in spec["sources"]] == [
            "stub-csv",
            "syslog",
            "flow-csv",
        ]

    def test_it_resolves_each_normalizer_from_the_registry(self) -> None:
        # By declared capability, not by name — so a newly registered normalizer works here
        # with no change, and a rename cannot silently break the page.
        spec = dashboard_app._composed_spec(
            [self._detector("flow_burst")], "weighted", ["flow-csv"], self._normalizers()
        )
        assert spec["sources"] == [{"normalizer": "flow", "source_format": "flow-csv"}]

    def test_an_entry_carries_every_detector_into_one_spec(self) -> None:
        # Several detectors in ONE spec run together, which is what lets their detections pool
        # into a single alert-engine run and corroborate across lanes.
        spec = dashboard_app._composed_spec(
            [self._detector("stub_threshold"), self._detector("flow_burst")],
            "weighted",
            ["stub-csv", "flow-csv"],
            self._normalizers(),
        )
        assert [entry["name"] for entry in spec["detectors"]] == ["stub_threshold", "flow_burst"]

    def test_every_offerable_corpus_and_detector_pair_composes_a_coherent_spec(self) -> None:
        """The end-to-end guard: whatever the picker can offer, the harness must accept.

        Both axes are discovered rather than named, so a corpus or detector added later is
        covered automatically. The detector axis is the half that matters and the half the
        previous version of this test was missing: it hardcoded `stub_threshold` and checked
        only `_check_alignment`, which compares *sources*. That passed for `fleet_scan` while
        the page was composing a flow corpus into a telemetry detector, because the sources
        genuinely did align — so this asserts the deeper joint too, via the guard that
        `evaluate` now consults.

        Two things here were still too narrow to catch `meter_iforest` on a `stub-csv` corpus
        (ADR-0035 D2). Eligibility was computed on lanes alone, exactly as the page computed
        it, so the test agreed with the bug instead of judging it; and the assertion matched
        the literal words "will not fire", so a differently-worded warning about the same
        defect would have passed. Eligibility now mirrors BOTH axes, and any warning naming a
        detector fails the pair — the invariant is "an offered detector draws no complaint",
        not "an offered detector draws no complaint phrased this way".
        """
        from iotshield.api.evaluation import _shape_fed_by
        from iotshield.capabilities import lanes_in, payloads_in
        from iotshield.eval import list_corpora, load_corpus
        from iotshield.eval.runner import _check_alignment, _compatibility_notes
        from iotshield.pipeline import PipelineSpec
        from iotshield.pipeline.registry import DETECTORS

        infos = list_corpora()
        assert infos, "no corpora discovered: this test would be vacuous"
        checked = 0
        for info in infos:
            formats = list(info.source_formats)
            lanes, payloads = _shape_fed_by(tuple(formats))
            eligible = [
                name
                for name, spec in DETECTORS.items()
                if {lane.value for lane in lanes_in(spec.capability({}).consumes)} & set(lanes)
                and payloads_in(spec.capability({}).consumes) <= set(payloads)
            ]
            assert eligible, f"no detector can judge {info.name!r}: the picker would be empty"
            for name in eligible:
                payload = dashboard_app._composed_spec(
                    [self._detector(name)], "weighted", formats, self._normalizers()
                )
                spec = PipelineSpec.model_validate(payload)
                for case in load_corpus(info.name).cases:
                    _check_alignment(spec, case)
                # An eligible detector must be *coherent*, not merely aligned. Any warning
                # about the detector stage means the picker offered something the guard
                # objects to; warnings about `ingest` are excluded because composing ONE
                # detector for a multi-lane corpus legitimately leaves lanes unjudged.
                assert not [
                    note for note in _compatibility_notes(spec) if note.startswith("detectors[")
                ], f"{name} is offered for {info.name} but the guard objects to it"
                checked += 1
        assert checked >= len(infos), "every corpus should contribute at least one pair"
