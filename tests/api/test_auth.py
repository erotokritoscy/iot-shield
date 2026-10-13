"""Login, organizations and per-organization data across both APIs (ADR-0039).

Both apps run with ``auth=True`` over one SQLite file, as compose runs them over one Postgres.
A "browser" is a pair of TestClients sharing one session cookie — the read API on one, the
Pipeline API on the other — which is what nginx's single origin gives the real dashboard.
"""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iotshield.api import create_app
from iotshield.api._auth import PUBLIC_ROUTES, SESSION_COOKIE
from iotshield.api.control import create_control_app
from iotshield.storage import create_schema, make_engine
from tests.api.conftest import PASSWORD, Browser, Stack


def _routes(app: FastAPI) -> list[tuple[str, str]]:
    # From the OpenAPI document, not `app.routes`: FastAPI keeps an included router (the
    # `/auth` and `/eval` routers) as one opaque entry there, so walking `app.routes` silently
    # skipped exactly the routes most worth checking. The schema lists every operation.
    return [
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    ]


def _concrete(path: str) -> str:
    return path.replace("{alert_id}", str(uuid4())).replace("{user_id}", str(uuid4()))


class TestTheGateIsDefaultDeny:
    """Every route not named public refuses a caller with no session — on BOTH apps."""

    @pytest.mark.parametrize("app_name", ["read_app", "pipeline_app"])
    def test_every_non_public_route_is_401_without_a_session(
        self, stack: Stack, app_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With the eval plane on too, so its router is held to the same rule.
        monkeypatch.setenv("IOTSHIELD_EVAL_API", "1")
        builders = {"read_app": create_app, "pipeline_app": create_control_app}
        app: FastAPI = builders[app_name](stack.engine, auth=True)
        client = TestClient(app)
        stack.clients.append(client)
        checked = 0
        for method, path in _routes(app):
            if path in PUBLIC_ROUTES:
                continue
            response = client.request(method, _concrete(path))
            assert response.status_code == 401, (method, path, response.text)
            checked += 1
        # The loop really ran over the app's surface, included routers and all.
        assert checked >= (8 if app_name == "read_app" else 12), checked

    def test_a_forged_cookie_is_401(self, stack: Stack) -> None:
        browser = stack.browser()
        browser.read.cookies.set(SESSION_COOKIE, "forged")
        assert browser.read.get("/alerts").status_code == 401

    def test_health_stays_public_for_the_compose_healthcheck(self, stack: Stack) -> None:
        browser = stack.browser()
        assert browser.read.get("/health").status_code == 200
        assert browser.pipeline.get("/health").status_code == 200

    def test_with_auth_off_the_auth_routes_do_not_exist(self, tmp_path: Path) -> None:
        # Absent, not guarded: the dashboard reads this 404 as "no login here".
        engine = make_engine(f"sqlite:///{(tmp_path / 'off.db').as_posix()}")
        create_schema(engine)
        with TestClient(create_control_app(engine, auth=False)) as client:
            assert client.get("/auth/me").status_code == 404
        engine.dispose()


class TestRegisterAndLogin:
    def test_register_logs_the_new_admin_in(self, stack: Stack) -> None:
        browser = stack.browser()
        response = browser.register("Acme Water", "Alice")
        assert response.status_code == 201
        body = response.json()
        assert body["username"] == "alice" and body["role"] == "admin"
        assert body["organization"] == {"org_id": "acme-water", "name": "Acme Water"}
        assert browser.pipeline.get("/auth/me").json()["username"] == "alice"
        assert browser.read.get("/summary").status_code == 200

    def test_the_session_cookie_is_httponly_and_strict(self, stack: Stack) -> None:
        response = stack.browser().register("Acme", "alice")
        cookie = response.headers["set-cookie"].lower()
        assert f"{SESSION_COOKIE}=" in cookie
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert "secure" not in cookie  # plain http here; set when the browser is on https

    def test_the_cookie_is_secure_behind_an_https_proxy(self, stack: Stack) -> None:
        browser = stack.browser()
        response = browser.pipeline.post(
            "/auth/register",
            json={"organization": "Acme", "username": "alice", "password": PASSWORD},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert "secure" in response.headers["set-cookie"].lower()

    def test_a_taken_org_or_username_is_409(self, stack: Stack) -> None:
        stack.browser().register("Acme", "alice")
        assert stack.browser().register("ACME", "bob").status_code == 409
        assert stack.browser().register("Globex", "alice").status_code == 409

    @pytest.mark.parametrize(
        "body",
        [
            {"organization": "Acme", "username": "alice", "password": "short"},
            {"organization": "Acme", "username": "a", "password": PASSWORD},
            {"organization": "Acme", "username": "has space", "password": PASSWORD},
            {"organization": "!!", "username": "alice", "password": PASSWORD},
            {"organization": "Acme", "username": "alice", "password": PASSWORD, "role": "x"},
        ],
    )
    def test_invalid_registrations_are_422(self, stack: Stack, body: dict[str, Any]) -> None:
        assert stack.browser().pipeline.post("/auth/register", json=body).status_code == 422

    def test_login_with_the_right_password_starts_a_session(self, stack: Stack) -> None:
        stack.browser().register("Acme", "alice")
        browser = stack.browser()
        assert browser.login("ALICE").status_code == 200
        assert browser.read.get("/alerts").status_code == 200

    def test_a_wrong_password_and_an_unknown_user_say_the_same_thing(self, stack: Stack) -> None:
        stack.browser().register("Acme", "alice")
        wrong = stack.browser().login("alice", "not-the-password")
        unknown = stack.browser().login("nobody")
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json() == {"detail": "invalid username or password"}

    def test_logout_ends_the_session_server_side(self, stack: Stack) -> None:
        browser = stack.browser()
        browser.register("Acme", "alice")
        token = browser.pipeline.cookies.get(SESSION_COOKIE)
        assert browser.pipeline.post("/auth/logout").status_code == 204
        # Replaying the old token must fail: logout is not merely the cookie being cleared.
        replay = stack.browser()
        replay.read.cookies.set(SESSION_COOKIE, token or "")
        assert replay.read.get("/alerts").status_code == 401

    def test_logout_without_a_session_still_succeeds(self, stack: Stack) -> None:
        assert stack.browser().pipeline.post("/auth/logout").status_code == 204


class TestPasswordChange:
    def test_changing_the_password_requires_the_current_one(self, stack: Stack) -> None:
        browser = stack.browser()
        browser.register("Acme", "alice")
        refused = browser.pipeline.post(
            "/auth/password",
            json={"current_password": "wrong-one", "new_password": "another-pass"},
        )
        assert refused.status_code == 403

    def test_a_changed_password_logs_out_other_sessions_and_works(self, stack: Stack) -> None:
        here = stack.browser()
        here.register("Acme", "alice")
        elsewhere = stack.browser()
        elsewhere.login("alice")
        changed = here.pipeline.post(
            "/auth/password",
            json={"current_password": PASSWORD, "new_password": "another-pass"},
        )
        assert changed.status_code == 204
        assert here.read.get("/alerts").status_code == 200  # this session survives
        assert elsewhere.read.get("/alerts").status_code == 401  # the other one does not
        assert stack.browser().login("alice", PASSWORD).status_code == 401
        assert stack.browser().login("alice", "another-pass").status_code == 200


class TestUserManagement:
    def _admin_with_member(self, stack: Stack) -> tuple[Browser, dict[str, Any]]:
        admin = stack.browser()
        admin.register("Acme", "alice")
        created = admin.pipeline.post(
            "/auth/users", json={"username": "bob", "password": "temp-pass-1"}
        )
        assert created.status_code == 201
        return admin, created.json()

    def test_an_admin_adds_a_member_who_can_then_log_in(self, stack: Stack) -> None:
        admin, bob = self._admin_with_member(stack)
        assert bob["role"] == "member"
        users = admin.pipeline.get("/auth/users").json()
        assert [u["username"] for u in users] == ["alice", "bob"]
        member = stack.browser()
        assert member.login("bob", "temp-pass-1").json()["organization"]["org_id"] == "acme"

    def test_an_admin_can_add_another_admin(self, stack: Stack) -> None:
        admin = stack.browser()
        admin.register("Acme", "alice")
        created = admin.pipeline.post(
            "/auth/users", json={"username": "carol", "password": "temp-pass-1", "role": "admin"}
        )
        assert created.json()["role"] == "admin"

    def test_a_member_cannot_manage_users(self, stack: Stack) -> None:
        self._admin_with_member(stack)
        member = stack.browser()
        member.login("bob", "temp-pass-1")
        assert member.pipeline.get("/auth/users").status_code == 403
        new = {"username": "eve", "password": "temp-pass-1"}
        assert member.pipeline.post("/auth/users", json=new).status_code == 403

    def test_a_member_can_still_run_and_read(self, stack: Stack) -> None:
        self._admin_with_member(stack)
        member = stack.browser()
        member.login("bob", "temp-pass-1")
        assert member.run_demo().status_code == 200
        assert len(member.read.get("/alerts").json()) == 1

    def test_removing_a_user_revokes_their_access(self, stack: Stack) -> None:
        admin, bob = self._admin_with_member(stack)
        member = stack.browser()
        member.login("bob", "temp-pass-1")
        assert admin.pipeline.delete(f"/auth/users/{bob['user_id']}").status_code == 204
        assert member.read.get("/alerts").status_code == 401
        assert stack.browser().login("bob", "temp-pass-1").status_code == 401

    def test_an_admin_cannot_remove_themselves(self, stack: Stack) -> None:
        admin = stack.browser()
        me = admin.register("Acme", "alice").json()
        assert admin.pipeline.delete(f"/auth/users/{me['user_id']}").status_code == 400

    def test_an_admin_cannot_touch_another_orgs_users(self, stack: Stack) -> None:
        stack.browser().register("Globex", "gina")
        gina_id = UUID(stack.browser().login("gina").json()["user_id"])
        admin = stack.browser()
        admin.register("Acme", "alice")
        assert admin.pipeline.delete(f"/auth/users/{gina_id}").status_code == 404
        assert [u["username"] for u in admin.pipeline.get("/auth/users").json()] == ["alice"]
        assert stack.browser().login("gina").status_code == 200

    def test_a_duplicate_username_is_409(self, stack: Stack) -> None:
        admin, _ = self._admin_with_member(stack)
        again = admin.pipeline.post(
            "/auth/users", json={"username": "BOB", "password": "temp-pass-1"}
        )
        assert again.status_code == 409


class TestOrganizationsSeeOnlyTheirOwnData:
    """The point of tenancy, end to end: runs land in the caller's org and nowhere else."""

    def test_a_run_is_visible_to_its_org_only(self, stack: Stack) -> None:
        acme = stack.browser()
        acme.register("Acme", "alice")
        globex = stack.browser()
        globex.register("Globex", "gina")

        assert acme.run_demo().status_code == 200

        acme_alerts = acme.read.get("/alerts").json()
        assert len(acme_alerts) == 1
        assert globex.read.get("/alerts").json() == []
        assert globex.read.get("/events").json() == []
        assert globex.read.get("/assets").json() == []
        assert set(globex.read.get("/summary").json()["counts"].values()) == {0}

        # Another org's alert by id is "not found", never "forbidden": no existence leak.
        alert_id = acme_alerts[0]["alert_id"]
        assert globex.read.get(f"/alerts/{alert_id}").status_code == 404
        assert globex.read.get(f"/alerts/{alert_id}/detections").status_code == 404
        assert acme.read.get(f"/alerts/{alert_id}/detections").status_code == 200

    def test_two_orgs_can_run_the_same_device_id(self, stack: Stack) -> None:
        acme = stack.browser()
        acme.register("Acme", "alice")
        globex = stack.browser()
        globex.register("Globex", "gina")
        assert acme.run_demo().status_code == 200
        assert globex.run_demo().status_code == 200  # pump-02 again, in another tenant
        assert [a["asset_id"] for a in acme.read.get("/assets").json()] == ["pump-02"]
        assert [a["asset_id"] for a in globex.read.get("/assets").json()] == ["pump-02"]
        assert len(acme.read.get("/alerts").json()) == 1
        assert len(globex.read.get("/alerts").json()) == 1
