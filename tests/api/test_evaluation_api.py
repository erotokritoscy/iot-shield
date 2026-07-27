"""The evaluation endpoints: opt-in, database-free, and honest about failure.

The first class is the important one. ADR-0021 D1 calls the evaluation plane the first thing
you would drop from a product image, and the way that is made true is *registration*, not a
guard: with the flag unset there is no ``/eval`` route to reach, probe, or exploit.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from iotshield.api._eval_gate import EVAL_API_ENV
from iotshield.api.control import create_control_app
from iotshield.pipeline import load_spec
from iotshield.storage import create_schema

STUB_SPEC = Path("config/bench/stub.yaml")


def make_client(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> TestClient:
    if enabled:
        monkeypatch.setenv(EVAL_API_ENV, "1")
    else:
        monkeypatch.delenv(EVAL_API_ENV, raising=False)
    engine = create_engine("sqlite://")
    create_schema(engine)
    return TestClient(create_control_app(engine))


def bench_spec() -> dict[str, Any]:
    return load_spec(STUB_SPEC).model_dump(mode="json")


class TestGating:
    def test_the_routes_do_not_exist_when_the_flag_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = make_client(monkeypatch, enabled=False)
        assert client.get("/eval/corpora").status_code == 404
        assert client.post("/eval/run", json={}).status_code == 404

    def test_the_product_endpoints_are_unaffected_either_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for enabled in (False, True):
            client = make_client(monkeypatch, enabled=enabled)
            assert client.get("/health").status_code == 200
            assert client.get("/modules").status_code == 200

    def test_the_routes_appear_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = make_client(monkeypatch, enabled=True)
        assert client.get("/eval/corpora").status_code == 200

    def test_the_eval_routes_are_absent_from_the_schema_when_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not merely unreachable — undocumented, because the router was never registered.
        client = make_client(monkeypatch, enabled=False)
        paths = client.get("/openapi.json").json()["paths"]
        assert not [path for path in paths if path.startswith("/eval")]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return make_client(monkeypatch, enabled=True)


class TestCatalogue:
    def test_corpora_lists_the_committed_one_and_advertises_the_rest(
        self, client: TestClient
    ) -> None:
        payload = client.get("/eval/corpora").json()
        assert "synthetic" in {item["name"] for item in payload["corpora"]}
        # Keyed by *corpus* name, so an un-fetched corpus is recognised exactly rather
        # than by a prefix guess — and a researcher learns SKAB is one command away rather
        # than concluding the harness only has toy data.
        assert "skab_valve1" in payload["fetchable"]
        assert "fetch skab" in payload["fetchable"]["skab_valve1"]
        assert payload["root"]

    def test_each_corpus_reports_its_size_and_terms(self, client: TestClient) -> None:
        payload = client.get("/eval/corpora").json()
        synthetic = next(i for i in payload["corpora"] if i["name"] == "synthetic")
        assert synthetic["cases"] > 0
        assert synthetic["licence"] and synthetic["provenance"]

    def test_each_corpus_publishes_the_detector_lanes_it_can_feed(self, client: TestClient) -> None:
        """The join a detector picker needs, decoded server-side.

        Published rather than derived by the client for the same reason `DetectorSummary.lanes`
        is decoded here: the `lane:` prefix is a private detail of `capabilities.py`, so a UI
        computing this for itself would have to parse it. Both sides of the comparison arrive
        in the same vocabulary and the client only intersects them.
        """
        corpora = {item["name"]: item for item in client.get("/eval/corpora").json()["corpora"]}
        assert corpora["synthetic"]["lanes"] == ["telemetry"]
        assert corpora["fleet_scan"]["lanes"] == ["flow"]
        # The trimodal corpus feeds all three, which is what makes one entry judging three
        # lanes expressible at all.
        assert corpora["device_trimodal"]["lanes"] == ["flow", "log", "telemetry"]

    def test_detectors_report_their_lane_and_trainability(self, client: TestClient) -> None:
        detectors = {item["name"]: item for item in client.get("/eval/detectors").json()}
        assert detectors["team3_zscore"]["fittable"] is True
        assert detectors["stub_threshold"]["fittable"] is False
        # Decoded, not wire tokens: a client never needs to know the "lane:" prefix.
        assert detectors["stub_threshold"]["lanes"] == ["telemetry"]
        assert detectors["log_burst"]["lanes"] == ["log"]

    def test_detectors_carry_the_hyperparameter_schema_a_form_needs(
        self, client: TestClient
    ) -> None:
        """One call, not a join against /modules — the Evaluate page builds its form here.

        And the defaults must be the adapter's real ones: a learning adapter's factory
        params all default to None (the ADR-0022 "not given" sentinel), so a signature-only
        introspection would give a UI nothing to prefill and no type to pick a widget by.
        """
        detectors = {item["name"]: item for item in client.get("/eval/detectors").json()}
        team3 = {param["name"]: param for param in detectors["team3_zscore"]["params"]}
        assert team3["window_size"]["default"] == 60
        assert team3["window_size"]["type"] == "int"  # seen through `int | None`
        assert team3["percentile"]["type"] == "float"

    def test_only_a_learning_adapter_declares_a_cached_fit_param(self, client: TestClient) -> None:
        # What lets a UI offer "trained weights" vs "fit from scratch" as two exclusive
        # modes instead of letting someone combine them and hit the atomicity error.
        detectors = {item["name"]: item for item in client.get("/eval/detectors").json()}
        assert detectors["team3_zscore"]["artifact_param"] == "artifact"
        assert detectors["stub_threshold"]["artifact_param"] is None


class TestRun:
    def test_a_benchmark_returns_a_full_report(self, client: TestClient) -> None:
        response = client.post(
            "/eval/run",
            json={"corpus": "synthetic", "specs": [{"label": "stub", "spec": bench_spec()}]},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["corpus"] == "synthetic"
        assert payload["specs"][0]["cases"]

    def test_an_entrys_detectors_run_together_and_can_corroborate(self, client: TestClient) -> None:
        """The measurement the dashboard could not express while it built one detector per spec.

        Detectors in ONE spec pool their detections into a single alert-engine run, so an alert
        can cite more than one lane. Split across specs, every alert cites exactly one lane by
        construction — which is why the corroboration result ADR-0026 publishes was unreachable
        from the UI at any setting. Same corpus, same bytes, same engine: the only difference is
        whether the three detectors share a spec.
        """
        sources = [
            {"normalizer": "stub", "source_format": "stub-csv"},
            {"normalizer": "syslog", "source_format": "syslog"},
            {"normalizer": "flow", "source_format": "flow-csv"},
        ]
        detectors = [
            {"name": "stub_threshold", "params": {"threshold": 80.0, "lane": "telemetry"}},
            {"name": "log_burst", "params": {"preset": "severity_spike"}},
            {"name": "flow_burst", "params": {"preset": "fan_out"}},
        ]
        engine = {"name": "weighted", "params": {"bucket_minutes": 5}}

        def lanes_cited(specs: list[dict[str, Any]]) -> float:
            payload = client.post(
                "/eval/run", json={"corpus": "device_trimodal", "specs": specs}
            ).json()
            return max(
                case["mean_lanes_per_true_alert"] or 0
                for report in payload["specs"]
                for case in report["cases"]
            )

        together = [
            {
                "label": "all-three",
                "spec": {"sources": sources, "detectors": detectors, "alert_engine": engine},
            }
        ]
        apart = [
            {
                "label": detector["name"],
                "spec": {"sources": sources, "detectors": [detector], "alert_engine": engine},
            }
            for detector in detectors
        ]
        assert lanes_cited(apart) == 1.0
        assert lanes_cited(together) > 1.0

    def test_timelines_are_off_by_default_and_on_when_asked(self, client: TestClient) -> None:
        # The report's other job is to be a small, diffable artifact; spans would dominate it.
        body = {"corpus": "synthetic", "specs": [{"label": "stub", "spec": bench_spec()}]}
        without = client.post("/eval/run", json=body).json()
        assert without["specs"][0]["cases"][0]["timeline"] is None

        with_spans = client.post("/eval/run", json={**body, "include_timeline": True}).json()
        timeline = with_spans["specs"][0]["cases"][0]["timeline"]
        assert timeline is not None and timeline["detections"] is not None

    def test_an_absent_corpus_is_404_not_500(self, client: TestClient) -> None:
        response = client.post(
            "/eval/run",
            json={"corpus": "nope", "specs": [{"label": "stub", "spec": bench_spec()}]},
        )
        assert response.status_code == 404

    def test_a_corpus_spec_mismatch_is_422_not_a_misleading_score(self, client: TestClient) -> None:
        # The failure this refuses: a syslog spec over a telemetry corpus quarantines every
        # line, and the report would read exactly like a detector that is simply bad.
        syslog = {
            "sources": [{"normalizer": "syslog", "source_format": "syslog"}],
            "detectors": [{"name": "log_burst"}],
        }
        response = client.post(
            "/eval/run",
            json={"corpus": "synthetic", "specs": [{"label": "syslog", "spec": syslog}]},
        )
        assert response.status_code == 422

    def test_a_sweep_expands_into_one_labelled_spec_per_value(self, client: TestClient) -> None:
        """Server-side expansion (ADR-0022), so CLI and dashboard label runs identically."""
        response = client.post(
            "/eval/run",
            json={
                "corpus": "synthetic",
                "specs": [
                    {
                        "label": "stub",
                        "spec": bench_spec(),
                        "sweep": {"threshold": [40.0, 80.0]},
                    }
                ],
            },
        )
        assert response.status_code == 200
        labels = [spec["spec"] for spec in response.json()["specs"]]
        assert labels == ["stub[threshold=40.0]", "stub[threshold=80.0]"]

    def _shared_threshold_run(self, sweep: dict[str, list[Any]]) -> dict[str, Any]:
        """A trimodal spec whose two detectors BOTH declare `threshold`.

        The configuration that had no expressible sweep before: unqualified `threshold` is
        refused as ambiguous, and there was no other way to name one of them.
        """
        return {
            "corpus": "device_trimodal",
            "specs": [
                {
                    "label": "mix",
                    "spec": {
                        "sources": [
                            {"normalizer": "stub", "source_format": "stub-csv"},
                            {"normalizer": "syslog", "source_format": "syslog"},
                            {"normalizer": "flow", "source_format": "flow-csv"},
                        ],
                        "detectors": [
                            {"name": "stub_threshold", "params": {"threshold": 80.0}},
                            {"name": "flow_burst", "params": {"preset": "fan_out"}},
                        ],
                        "alert_engine": {"name": "weighted"},
                    },
                    "sweep": sweep,
                }
            ],
        }

    def test_a_qualified_sweep_expands_into_one_labelled_spec_per_value(
        self, client: TestClient
    ) -> None:
        # `detector.param` travels over the wire with no schema change: the sweep key was
        # already a string, so the grammar stayed confined to eval/sweep.py.
        body = self._shared_threshold_run({"flow_burst.threshold": [10, 30]})
        payload = client.post("/eval/run", json=body).json()
        assert [report["spec"] for report in payload["specs"]] == [
            "mix[flow_burst.threshold=10]",
            "mix[flow_burst.threshold=30]",
        ]

    def test_an_ambiguous_unqualified_sweep_is_422_and_names_the_qualified_form(
        self, client: TestClient
    ) -> None:
        # Still refused, because one label reporting two incomparable knobs would be a lie.
        # The difference is that the error now carries a remedy the caller can act on.
        response = client.post(
            "/eval/run", json=self._shared_threshold_run({"threshold": [10, 30]})
        )
        assert response.status_code == 422
        assert "flow_burst.threshold" in response.json()["detail"]

    def test_an_incoherent_sweep_is_422_not_a_500(self, client: TestClient) -> None:
        response = client.post(
            "/eval/run",
            json={
                "corpus": "synthetic",
                "specs": [{"label": "stub", "spec": bench_spec(), "sweep": {"nope": [1]}}],
            },
        )
        assert response.status_code == 422
        assert "nope" in str(response.json()["detail"])

    def test_an_untrained_detector_scores_zero_with_a_reason_rather_than_crashing(
        self, client: TestClient
    ) -> None:
        # Naming hyperparameters and no artifact under protocol `none` asks to judge with
        # an untrained model. The benchmark must neither die nor report a silent row of
        # zeros that reads as "this detector is bad" (ADR-0022).
        spec = bench_spec() | {
            "detectors": [{"name": "team3_zscore", "params": {"window_size": 20}}]
        }
        response = client.post(
            "/eval/run", json={"corpus": "synthetic", "specs": [{"label": "raw", "spec": spec}]}
        )
        assert response.status_code == 200
        cases = response.json()["specs"][0]["cases"]
        assert all(case["detections"] == 0 for case in cases)
        assert any("judged nothing" in note for case in cases for note in case["notes"])

    def test_a_fit_protocol_is_accepted_and_reported(self, client: TestClient) -> None:
        response = client.post(
            "/eval/run",
            json={
                "corpus": "synthetic",
                "specs": [{"label": "stub", "spec": bench_spec()}],
                "fit_protocol": "prefix",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["fit_protocol"] == "prefix"
        # stub_threshold cannot train; the report says so rather than the run failing.
        notes = [note for case in payload["specs"][0]["cases"] for note in case["notes"]]
        assert any("not fittable" in note for note in notes)


def test_the_eval_router_needs_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A benchmark computes and returns — that it needs no store is the plane in the wiring.

    Proven by pointing the app at a database that cannot be reached: ``/health`` fails
    because it proves connectivity, while the eval routes are unbothered.
    """
    monkeypatch.setenv(EVAL_API_ENV, "1")
    unreachable = create_engine("sqlite:///C:/nonexistent-dir/nope.db")
    client = TestClient(create_control_app(unreachable), raise_server_exceptions=False)

    assert client.get("/health").status_code == 500
    assert client.get("/eval/corpora").status_code == 200
    response = client.post(
        "/eval/run",
        json={"corpus": "synthetic", "specs": [{"label": "stub", "spec": bench_spec()}]},
    )
    assert response.status_code == 200


def test_importing_the_control_app_does_not_import_the_eval_plane() -> None:
    """ADR-0021 D1's droppable-plane claim, for the harder of the two modules.

    `create_control_app` genuinely does construct the eval router, so the deferred import
    at its call site is load-bearing: hoisting it to module scope would make the Pipeline
    API fail to import without `iotshield.eval` present, with a green suite. Checked in a
    subprocess because the rest of this file has already imported the package.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import iotshield.api.control; "
        "print(any(m == 'iotshield.eval' or m.startswith('iotshield.eval.') for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


class TestBenchSpecCatalogue:
    """`/eval/specs` publishes the committed specs, so a UI runs one instead of a lookalike.

    The dashboard used to compose its own `PipelineSpec` from the detectors on screen, which
    dropped the `assets:` block and every source and engine parameter — so the page and the
    CLI published different numbers for what read as the same benchmark. Publishing the spec
    itself removes the second implementation rather than trying to keep two in sync.
    """

    def test_it_publishes_the_committed_specs_with_their_detectors(
        self, client: TestClient
    ) -> None:
        payload = client.get("/eval/specs").json()
        by_name = {item["name"]: item for item in payload}
        assert "stub" in by_name and "flow" in by_name
        assert by_name["stub"]["detectors"] == ["stub_threshold"]
        # The whole spec, not a description of it: a client forwards this untouched.
        assert by_name["stub"]["spec"]["sources"][0]["source_format"] == "stub-csv"

    def test_each_spec_names_the_corpora_it_fits(self, client: TestClient) -> None:
        """The pairing nothing recorded: six specs, six corpora, ~7 legal cells, and the map
        lived only in YAML comments. Derived from both sides' declared shapes, so it cannot
        drift from the thing it describes."""
        payload = client.get("/eval/specs").json()
        by_name = {item["name"]: item for item in payload}
        assert "synthetic" in by_name["stub"]["corpora"]
        assert "fleet_scan" in by_name["flow"]["corpora"]
        # ... and a spec never claims a corpus whose input shape it cannot ingest.
        assert "fleet_scan" not in by_name["stub"]["corpora"]

    def test_a_published_spec_actually_runs(self, client: TestClient) -> None:
        # The point of publishing it: what a client forwards must be a legal request.
        spec = next(item for item in client.get("/eval/specs").json() if item["name"] == "stub")
        response = client.post(
            "/eval/run",
            json={
                "corpus": "synthetic",
                "specs": [{"label": "stub", "spec": spec["spec"]}],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["specs"][0]["spec"] == "stub"
