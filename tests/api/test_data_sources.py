"""An organization's IT/OT data-source register over HTTP (ADR-0041).

Auth off first — the single-tenant product, where every request is the default org's admin —
then with login on, for the two properties that matter there: only admins write, and one
organization never sees another's sources.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from iotshield.api.control import create_control_app
from iotshield.storage import accounts, create_schema, make_engine, make_session_factory
from iotshield.storage.orm import DataSourceRow
from tests.api.conftest import Stack
from tests.connect_world import CONNECTIONS_YAML, World

CAMERA_FLOWS: dict[str, Any] = {
    "name": "Camera network",
    "type": "IT",
    "description": "Zeek conn.log from the camera VLAN",
    "normalizer": "flow",
    "source_format": "zeek-conn",
}
METERS: dict[str, Any] = {
    "name": "SensiPro meters",
    "type": "OT",
    "normalizer": "meter",
    "source_format": "sensipro-hourly-csv",
}


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'sources.db').as_posix()}")
    create_schema(engine)
    with TestClient(create_control_app(engine, auth=False)) as test_client:
        yield test_client
    engine.dispose()


class TestTheRegister:
    def test_starts_empty_and_lists_what_was_added_it_before_ot(self, client: TestClient) -> None:
        assert client.get("/data-sources").json() == []
        assert client.post("/data-sources", json=METERS).status_code == 201
        assert client.post("/data-sources", json=CAMERA_FLOWS).status_code == 201
        listed = client.get("/data-sources").json()
        assert [(s["type"], s["name"]) for s in listed] == [
            ("IT", "Camera network"),
            ("OT", "SensiPro meters"),
        ]
        assert listed[0]["description"] == "Zeek conn.log from the camera VLAN"

    def test_a_source_needs_no_reader_yet(self, client: TestClient) -> None:
        body = client.post("/data-sources", json={"name": "Biology", "type": "IT"}).json()
        assert (body["normalizer"], body["source_format"], body["description"]) == (
            None,
            None,
            None,
        )

    def test_blank_optional_text_is_stored_as_not_set(self, client: TestClient) -> None:
        body = client.post(
            "/data-sources",
            json={"name": "  Biology  ", "type": "IT", "description": " ", "normalizer": ""},
        ).json()
        assert body["name"] == "Biology"
        assert body["description"] is None and body["normalizer"] is None

    def test_a_normalizer_with_one_format_gets_it_filled_in(self, client: TestClient) -> None:
        body = client.post(
            "/data-sources", json={"name": "Gateways", "type": "IT", "normalizer": "syslog"}
        ).json()
        assert body["source_format"] == "syslog"

    def test_edits_replace_every_field(self, client: TestClient) -> None:
        created = client.post("/data-sources", json=CAMERA_FLOWS).json()
        changed = client.put(
            f"/data-sources/{created['source_id']}",
            json={"name": "Camera VLAN", "type": "OT"},
        )
        assert changed.status_code == 200
        body = changed.json()
        assert (body["name"], body["type"], body["normalizer"], body["description"]) == (
            "Camera VLAN",
            "OT",
            None,
            None,
        )
        assert body["created_at"] == created["created_at"]

    def test_removes_a_source(self, client: TestClient) -> None:
        created = client.post("/data-sources", json=CAMERA_FLOWS).json()
        assert client.delete(f"/data-sources/{created['source_id']}").status_code == 204
        assert client.get("/data-sources").json() == []

    def test_an_unknown_id_is_404(self, client: TestClient) -> None:
        assert client.put(f"/data-sources/{uuid4()}", json=CAMERA_FLOWS).status_code == 404
        assert client.delete(f"/data-sources/{uuid4()}").status_code == 404

    def test_a_name_is_unique_within_the_organization(self, client: TestClient) -> None:
        client.post("/data-sources", json=CAMERA_FLOWS)
        again = client.post("/data-sources", json=CAMERA_FLOWS | {"type": "OT"})
        assert again.status_code == 409
        other = client.post("/data-sources", json=METERS).json()
        renamed = client.put(f"/data-sources/{other['source_id']}", json=CAMERA_FLOWS)
        assert renamed.status_code == 409
        # Keeping its own name is not a clash with itself.
        same = client.put(f"/data-sources/{other['source_id']}", json=METERS)
        assert same.status_code == 200

    @pytest.mark.parametrize(
        "body",
        [
            {"name": "x", "type": "IoT"},  # only IT or OT
            {"name": " ", "type": "IT"},
            {"name": "x" * 101, "type": "IT"},
            {"name": "x", "type": "IT", "normalizer": "nope"},
            {"name": "x", "type": "IT", "normalizer": "flow"},  # reads two formats: name one
            {"name": "x", "type": "IT", "normalizer": "stub", "source_format": "syslog"},
            {"name": "x", "type": "IT", "source_format": "syslog"},  # a format with no reader
            {"name": "x", "type": "IT", "colour": "red"},
        ],
    )
    def test_refuses_what_a_run_would_refuse(
        self, client: TestClient, body: dict[str, Any]
    ) -> None:
        assert client.post("/data-sources", json=body).status_code == 422


class TestWithLogin:
    def test_a_member_reads_but_only_an_admin_writes(self, stack: Stack) -> None:
        admin = stack.browser()
        admin.register("Acme", "alice")
        created = admin.pipeline.post("/data-sources", json=CAMERA_FLOWS).json()
        admin.pipeline.post("/auth/users", json={"username": "bob", "password": "temp-pass-1"})

        member = stack.browser()
        member.login("bob", "temp-pass-1")
        assert [s["name"] for s in member.pipeline.get("/data-sources").json()] == [
            "Camera network"
        ]
        assert member.pipeline.post("/data-sources", json=METERS).status_code == 403
        path = f"/data-sources/{created['source_id']}"
        assert member.pipeline.put(path, json=METERS).status_code == 403
        assert member.pipeline.delete(path).status_code == 403

    def test_one_organization_never_sees_or_touches_anothers(self, stack: Stack) -> None:
        acme = stack.browser()
        acme.register("Acme", "alice")
        created = acme.pipeline.post("/data-sources", json=CAMERA_FLOWS).json()

        globex = stack.browser()
        globex.register("Globex", "gina")
        assert globex.pipeline.get("/data-sources").json() == []
        path = f"/data-sources/{created['source_id']}"
        assert globex.pipeline.put(path, json=METERS).status_code == 404
        assert globex.pipeline.delete(path).status_code == 404
        # The same name is free in another organization.
        assert globex.pipeline.post("/data-sources", json=CAMERA_FLOWS).status_code == 201
        assert len(acme.pipeline.get("/data-sources").json()) == 1

    def test_a_superadmin_without_a_home_sees_no_sources(self, stack: Stack) -> None:
        root = stack.superadmin()
        assert root.pipeline.get("/data-sources").status_code == 403

    def test_deleting_an_organization_deletes_its_sources(self, stack: Stack) -> None:
        acme = stack.browser()
        acme.register("Acme", "alice")
        acme.pipeline.post("/data-sources", json=CAMERA_FLOWS)
        factory = make_session_factory(stack.engine)
        with factory() as session:
            assert accounts.delete_organization(session, "acme")
            session.commit()
        with factory() as session:
            assert _rows(session) == 0


def _rows(session: Session) -> int:
    return len(session.query(DataSourceRow).all())


@pytest.fixture()
def connected(
    client: TestClient,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """The register with the two test databases declared as connections (ADR-0042)."""
    path = tmp_path / "connections.yaml"
    path.write_text(CONNECTIONS_YAML, encoding="utf-8")
    monkeypatch.setenv("IOTSHIELD_CONNECTIONS_PATH", str(path))
    return client


def _source(client: TestClient, connection: str, **extra: Any) -> dict[str, Any]:
    response = client.post(
        "/data-sources",
        json={"name": connection.title(), "type": "OT", "connection": connection} | extra,
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestConnections:
    def test_lists_the_connections_with_what_a_pull_yields_and_its_params(
        self, connected: TestClient
    ) -> None:
        body = connected.get("/data-sources/connections").json()
        by_name = {item["name"]: item for item in body}
        assert set(by_name) == {"biology", "sensipro"}
        sensipro = by_name["sensipro"]
        assert (sensipro["normalizer"], sensipro["source_format"]) == (
            "meter",
            "sensipro-hourly-csv",
        )
        params = {param["name"]: param for param in sensipro["params"]}
        assert params["utility"]["choices"] == ["electricity", "water"]
        assert params["utility"]["default"] == "electricity"
        assert sensipro["has_devices"] is True
        assert "host" not in str(body) and "db.invalid" not in str(body)

    def test_no_connections_file_means_none(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IOTSHIELD_CONNECTIONS_PATH", str(tmp_path / "absent.yaml"))
        assert client.get("/data-sources/connections").json() == []

    def test_a_connected_source_is_read_the_way_its_connector_writes(
        self, connected: TestClient
    ) -> None:
        source = _source(connected, "sensipro")
        assert (source["normalizer"], source["source_format"], source["connection"]) == (
            "meter",
            "sensipro-hourly-csv",
            "sensipro",
        )
        clash = connected.post(
            "/data-sources",
            json={"name": "x", "type": "OT", "connection": "sensipro", "normalizer": "syslog"},
        )
        assert clash.status_code == 422 and "yields sensipro-hourly-csv" in clash.text

    def test_an_undeclared_connection_is_refused(self, connected: TestClient) -> None:
        response = connected.post(
            "/data-sources", json={"name": "x", "type": "IT", "connection": "nope"}
        )
        assert response.status_code == 422 and "no connection named 'nope'" in response.text

    def test_pulls_a_window_ready_to_be_a_runs_input(self, connected: TestClient) -> None:
        source = _source(connected, "sensipro")
        response = connected.post(
            f"/data-sources/{source['source_id']}/pull",
            json={"start": "2023-07-01", "end": "2023-07-01", "params": {"utility": "water"}},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["normalizer"], body["source_format"], body["rows"]) == (
            "meter",
            "sensipro-hourly-csv",
            6,
        )
        assert body["origin"] == "Sensipro 2023-07-01..2023-07-01"
        # The content runs as-is: the same request the Compose page then sends.
        run = connected.post(
            "/runs",
            json={
                "spec": {
                    "sources": [{"normalizer": "meter", "source_format": "sensipro-hourly-csv"}],
                    "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
                    "alert_engine": {"name": "stub"},
                },
                "inputs": [{"content": body["content"], "origin": body["origin"]}],
            },
        )
        assert run.status_code == 200, run.text
        assert (run.json()["events"], run.json()["malformed"]) == (5, 0)

    def test_lists_devices_for_the_chosen_utility(self, connected: TestClient) -> None:
        source = _source(connected, "sensipro")
        devices = connected.get(f"/data-sources/{source['source_id']}/devices?utility=water").json()
        assert devices == [{"id": "w1", "label": "w1 — Kitchen"}]

    @pytest.mark.parametrize(
        ("body", "status", "says"),
        [
            ({"start": "2023-07-02", "end": "2023-07-01"}, 422, "before the start"),
            ({"start": "2023-01-01", "end": "2023-12-31"}, 422, "at most 92"),
            (
                {"start": "2023-07-01", "end": "2023-07-01", "params": {"utility": "gas"}},
                422,
                "utility",
            ),
            ({"start": "July", "end": "2023-07-01"}, 422, "date"),
        ],
    )
    def test_a_pull_that_cannot_be_served_says_why(
        self, connected: TestClient, body: dict[str, Any], status: int, says: str
    ) -> None:
        source = _source(connected, "sensipro")
        response = connected.post(f"/data-sources/{source['source_id']}/pull", json=body)
        assert response.status_code == status and says in response.text

    def test_a_source_without_a_connection_has_nothing_to_pull(self, connected: TestClient) -> None:
        plain = connected.post("/data-sources", json=METERS).json()
        response = connected.post(
            f"/data-sources/{plain['source_id']}/pull",
            json={"start": "2023-07-01", "end": "2023-07-01"},
        )
        assert response.status_code == 422 and "no connection" in response.text


class TestConnectionsWithLogin:
    def test_a_member_may_pull(
        self, stack: Stack, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "connections.yaml"
        path.write_text(CONNECTIONS_YAML, encoding="utf-8")
        monkeypatch.setenv("IOTSHIELD_CONNECTIONS_PATH", str(path))
        admin = stack.browser()
        admin.register("Acme", "alice")
        source = admin.pipeline.post(
            "/data-sources", json={"name": "Meters", "type": "OT", "connection": "sensipro"}
        ).json()
        admin.pipeline.post("/auth/users", json={"username": "bob", "password": "temp-pass-1"})
        member = stack.browser()
        member.login("bob", "temp-pass-1")
        pulled = member.pipeline.post(
            f"/data-sources/{source['source_id']}/pull",
            json={"start": "2023-07-01", "end": "2023-07-01"},
        )
        assert pulled.status_code == 200 and pulled.json()["rows"] == 12

    def test_a_connection_reserved_for_another_organization_does_not_exist_for_you(
        self,
        stack: Stack,
        world: World,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "connections.yaml"
        path.write_text(CONNECTIONS_YAML, encoding="utf-8")
        monkeypatch.setenv("IOTSHIELD_CONNECTIONS_PATH", str(path))
        acme = stack.browser()
        acme.register("Acme", "alice")
        # `biology` is declared for the `default` organization only.
        assert [c["name"] for c in acme.pipeline.get("/data-sources/connections").json()] == [
            "sensipro"
        ]
        refused = acme.pipeline.post(
            "/data-sources", json={"name": "Flows", "type": "IT", "connection": "biology"}
        )
        assert refused.status_code == 422 and "no connection named 'biology'" in refused.text
