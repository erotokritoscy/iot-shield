"""HTTP API data sources over the Pipeline API (ADR-0049): register, pull, run.

What matters here, beyond the unit tests of the mapper, guard and client: the credentials are
write-only (never in a response, never in the clear in the database, kept on an edit that
leaves them blank), the URL guard runs when a source is saved, a pull and a run read the API
the way they read a database, and one organization never reaches another's API source.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from iotshield.api.control import create_control_app
from iotshield.connect.api import client as api_client
from iotshield.storage import create_schema, make_engine, make_session_factory
from iotshield.storage.orm import DataSourceEndpointRow, EventRow, MalformedRow
from iotshield.storage.secrets import SECRET_KEY_ENV
from tests.api.conftest import Stack

API_KEY = "sk-live-do-not-leak"

READINGS = [
    {"ts": "2026-11-01T10:00:00Z", "dev": "pump-1", "r": {"temp": 21.0}},
    {"ts": "2026-11-01T11:00:00Z", "dev": "pump-1", "r": {"temp": 95.0}},
    {"ts": "2026-11-01T12:00:00Z", "dev": "pump-2", "r": {}},  # no reading: Data Health
]

PLANT_API: dict[str, Any] = {
    "name": "Plant API",
    "type": "OT",
    "api": {
        "url": "https://api.example.com/v1/readings",
        "auth": "api_key",
        "start_param": "from",
        "end_param": "to",
        "window_format": "date",
        "mapping": {
            "lane": "telemetry",
            "records": "data",
            "timestamp": "ts",
            "asset_id": "dev",
            "metrics": {"Temperature": "r.temp"},
        },
    },
    "credentials": {"api_key": API_KEY},
}

LOG_API: dict[str, Any] = {
    "name": "Gateway logs",
    "type": "IT",
    "api": {
        "url": "https://api.example.com/v1/logs",
        "auth": "basic",
        "username": "ops",
        "start_param": "since",
        "end_param": "until",
        "mapping": {
            "lane": "log",
            "timestamp": "at",
            "asset_id": "host",
            "message": "text",
            "severity": "level",
        },
    },
    "credentials": {"password": "pw-1"},
}


def _resolve(host: str, port: int) -> list[str]:
    return {"api.example.com": ["93.184.216.34"], "intranet.example.com": ["10.1.2.3"]}.get(
        host, []
    )


class Upstream:
    """The fake API: answers by path, records what it was asked."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/readings":
            if request.headers.get("Authorization") != f"Bearer {API_KEY}":
                return httpx.Response(401)
            return httpx.Response(200, json={"data": READINGS})
        if request.url.path == "/v1/logs":
            return httpx.Response(
                200,
                json=[
                    {"at": "2026-11-01T10:00:00Z", "host": "gw", "text": "boot", "level": "info"}
                ],
            )
        return httpx.Response(404)


@pytest.fixture()
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[Upstream]:
    monkeypatch.setenv(SECRET_KEY_ENV, Fernet.generate_key().decode())
    fake = Upstream()
    api_client.use_transport(httpx.MockTransport(fake), _resolve)
    yield fake
    api_client.use_transport(None)


@pytest.fixture()
def engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = make_engine(f"sqlite:///{(tmp_path / 'api-sources.db').as_posix()}")
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def client(engine, upstream: Upstream) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    with TestClient(create_control_app(engine, auth=False)) as test_client:
        yield test_client


def _add(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/data-sources", json=body)
    assert response.status_code == 201, response.text
    return response.json()


class TestRegister:
    def test_an_api_source_is_read_by_passthrough_in_its_mapped_lane(
        self, client: TestClient
    ) -> None:
        source = _add(client, PLANT_API)
        assert (source["normalizer"], source["source_format"]) == (
            "passthrough",
            "normalized-jsonl",
        )
        assert source["reader_params"] == {"lane": "telemetry"}
        assert source["api"]["url"] == PLANT_API["api"]["url"]
        assert source["has_credentials"] is True
        assert _add(client, LOG_API)["reader_params"] == {"lane": "log"}

    def test_credentials_are_write_only_and_sealed_at_rest(
        self,
        client: TestClient,
        engine,  # type: ignore[no-untyped-def]
    ) -> None:
        source = _add(client, PLANT_API)
        assert API_KEY not in json.dumps(source)
        assert API_KEY not in client.get("/data-sources").text
        with make_session_factory(engine)() as session:
            stored = session.scalars(select(DataSourceEndpointRow)).one()
            assert stored.credentials and API_KEY not in stored.credentials
            assert API_KEY not in stored.config

    def test_an_edit_that_leaves_the_key_blank_keeps_it(self, client: TestClient) -> None:
        source = _add(client, PLANT_API)
        path = f"/data-sources/{source['source_id']}"
        renamed = PLANT_API | {"name": "Renamed", "credentials": {"api_key": "  "}}
        assert client.put(path, json=renamed).status_code == 200
        pulled = client.post(f"{path}/pull", json={"start": "2026-11-01", "end": "2026-11-01"})
        assert pulled.status_code == 200, pulled.text  # still authenticates

    def test_switching_the_authentication_needs_the_new_secret(self, client: TestClient) -> None:
        source = _add(client, PLANT_API)
        path = f"/data-sources/{source['source_id']}"
        basic = PLANT_API | {
            "api": PLANT_API["api"] | {"auth": "basic", "username": "ops"},
            "credentials": None,
        }
        response = client.put(path, json=basic)
        assert response.status_code == 422 and "password" in response.text

    def test_a_new_source_needs_its_secret(self, client: TestClient) -> None:
        response = client.post("/data-sources", json=PLANT_API | {"credentials": None})
        assert response.status_code == 422 and "API key" in response.text
        assert client.get("/data-sources").json() == []  # nothing half-saved

    def test_an_internal_url_is_refused_when_saved(self, client: TestClient) -> None:
        inward = PLANT_API | {"api": PLANT_API["api"] | {"url": "https://intranet.example.com/x"}}
        response = client.post("/data-sources", json=inward)
        assert response.status_code == 422 and "private or internal" in response.text
        assert client.get("/data-sources").json() == []

    @pytest.mark.parametrize(
        "changes",
        [
            {"connection": "sensipro"},
            {"normalizer": "stub"},
        ],
    )
    def test_refuses_a_second_reader(self, client: TestClient, changes: dict[str, Any]) -> None:
        assert client.post("/data-sources", json=PLANT_API | changes).status_code == 422

    def test_no_server_key_refuses_rather_than_storing_in_the_clear(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SECRET_KEY_ENV)
        response = client.post("/data-sources", json=PLANT_API)
        assert response.status_code == 503 and SECRET_KEY_ENV in response.text
        assert client.get("/data-sources").json() == []

    def test_an_api_without_authentication_stores_no_credentials(self, client: TestClient) -> None:
        open_api = PLANT_API | {"api": PLANT_API["api"] | {"auth": "none"}, "credentials": None}
        assert _add(client, open_api)["has_credentials"] is False

    def test_dropping_the_api_drops_its_endpoint_and_removing_the_source_too(
        self,
        client: TestClient,
        engine,  # type: ignore[no-untyped-def]
    ) -> None:
        source = _add(client, PLANT_API)
        path = f"/data-sources/{source['source_id']}"
        plain = {"name": "Plant API", "type": "OT"}
        assert client.put(path, json=plain).json()["api"] is None
        factory = make_session_factory(engine)
        with factory() as session:
            assert session.scalars(select(DataSourceEndpointRow)).all() == []
        again = _add(client, LOG_API)
        assert client.delete(f"/data-sources/{again['source_id']}").status_code == 204
        with factory() as session:
            assert session.scalars(select(DataSourceEndpointRow)).all() == []


class TestPullAndRun:
    def test_a_pull_summarizes_the_window_from_the_api(
        self, client: TestClient, upstream: Upstream
    ) -> None:
        source = _add(client, PLANT_API)
        response = client.post(
            f"/data-sources/{source['source_id']}/pull",
            json={"start": "2026-11-01", "end": "2026-11-02"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["rows"], body["normalizer"]) == (3, "passthrough")
        assert len(body["sample"].splitlines()) == 3
        params = upstream.requests[-1].url.params
        assert (params["from"], params["to"]) == ("2026-11-01", "2026-11-02")

    def test_an_upstream_refusal_is_a_502_naming_the_source(self, client: TestClient) -> None:
        source = _add(client, PLANT_API | {"credentials": {"api_key": "wrong"}})
        response = client.post(
            f"/data-sources/{source['source_id']}/pull",
            json={"start": "2026-11-01", "end": "2026-11-01"},
        )
        assert response.status_code == 502
        assert "Plant API" in response.text and "credentials" in response.text

    def _run(
        self, client: TestClient, source_id: str, lane: str | None = "telemetry"
    ) -> httpx.Response:
        params = {"lane": lane} if lane else {}
        return client.post(
            "/runs",
            json={
                "spec": {
                    "sources": [
                        {
                            "normalizer": "passthrough",
                            "source_format": "normalized-jsonl",
                            "params": params,
                        }
                    ],
                    "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
                    "alert_engine": {"name": "stub"},
                },
                "inputs": [
                    {"pull": {"source_id": source_id, "start": "2026-11-01", "end": "2026-11-01"}}
                ],
            },
        )

    def test_a_run_reads_the_api_itself_and_judges_it(
        self,
        client: TestClient,
        engine,  # type: ignore[no-untyped-def]
    ) -> None:
        source = _add(client, PLANT_API)
        run = self._run(client, source["source_id"])
        assert run.status_code == 200, run.text
        body = run.json()
        assert (body["events"], body["malformed"], body["detections"]) == (2, 1, 1)
        with make_session_factory(engine)() as session:
            assert len(session.scalars(select(EventRow)).all()) == 2
            reject = session.scalars(select(MalformedRow)).one()
            assert "r.temp" in reject.raw
        saved = client.get(f"/runs/{body['run_id']}").json()
        assert saved["sources"][0]["pull"]["source_id"] == source["source_id"]

    def test_a_default_lane_reads_a_telemetry_api(self, client: TestClient) -> None:
        source = _add(client, PLANT_API)
        assert self._run(client, source["source_id"], lane=None).status_code == 200

    def test_a_run_must_read_the_api_in_its_mapped_lane(
        self, client: TestClient, upstream: Upstream
    ) -> None:
        source = _add(client, LOG_API)
        response = self._run(client, source["source_id"], lane="telemetry")
        assert response.status_code == 422 and "lane: log" in response.text
        assert upstream.requests == []  # refused before anything was read


class TestTenancy:
    def test_another_organization_cannot_pull_or_see_an_api_source(
        self, stack: Stack, upstream: Upstream
    ) -> None:
        acme = stack.browser()
        acme.register("Acme", "alice")
        created = acme.pipeline.post("/data-sources", json=PLANT_API)
        assert created.status_code == 201, created.text
        source_id = created.json()["source_id"]

        globex = stack.browser()
        globex.register("Globex", "gina")
        assert globex.pipeline.get("/data-sources").json() == []
        pulled = globex.pipeline.post(
            f"/data-sources/{source_id}/pull", json={"start": "2026-11-01", "end": "2026-11-01"}
        )
        assert pulled.status_code == 404
        assert upstream.requests == []
