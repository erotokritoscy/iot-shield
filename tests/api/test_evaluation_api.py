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

    def test_detectors_report_their_lane_and_trainability(self, client: TestClient) -> None:
        detectors = {item["name"]: item for item in client.get("/eval/detectors").json()}
        assert detectors["team3_zscore"]["fittable"] is True
        assert detectors["stub_threshold"]["fittable"] is False
        # Decoded, not wire tokens: a client never needs to know the "lane:" prefix.
        assert detectors["stub_threshold"]["lanes"] == ["telemetry"]
        assert detectors["log_burst"]["lanes"] == ["log"]


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
