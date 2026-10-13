"""The super admin and organization management (ADR-0040).

What is being held: the super admin manages every organization and sees none of their data;
nobody else can reach ``/admin``; suspension blocks logins without deleting; deletion takes
everything and needs its id repeated; registration can be closed.
"""

from typing import Any

import pytest

from tests.api.conftest import PASSWORD, Browser, Stack


def _org_with_data(stack: Stack, org: str = "Acme", admin: str = "alice") -> Browser:
    browser = stack.browser()
    assert browser.register(org, admin).status_code == 201
    assert browser.run_demo().status_code == 200
    return browser


def _orgs(root: Browser) -> dict[str, dict[str, Any]]:
    return {o["org_id"]: o for o in root.pipeline.get("/admin/organizations").json()}


class TestWhoTheSuperAdminIs:
    def test_me_has_the_role_and_no_organization(self, stack: Stack) -> None:
        me = stack.superadmin().pipeline.get("/auth/me").json()
        assert me["role"] == "superadmin" and me["organization"] is None

    @pytest.mark.parametrize(
        "path", ["/alerts", "/events", "/assets", "/detections", "/malformed", "/summary"]
    )
    def test_no_organization_data_is_readable(self, stack: Stack, path: str) -> None:
        _org_with_data(stack)
        root = stack.superadmin()
        response = root.read.get(path)
        assert response.status_code == 403
        assert "cannot see their data" in response.json()["detail"]

    def test_cannot_run_a_pipeline_or_use_org_user_routes(self, stack: Stack) -> None:
        root = stack.superadmin()
        assert root.run_demo().status_code == 403
        assert root.pipeline.get("/auth/users").status_code == 403

    def test_can_change_their_own_password(self, stack: Stack) -> None:
        root = stack.superadmin()
        changed = root.pipeline.post(
            "/auth/password", json={"current_password": PASSWORD, "new_password": "new-root-pw"}
        )
        assert changed.status_code == 204
        assert stack.browser().login("root", "new-root-pw").status_code == 200

    def test_appears_in_no_organizations_user_list(self, stack: Stack) -> None:
        alice = _org_with_data(stack)
        stack.superadmin()
        assert [u["username"] for u in alice.pipeline.get("/auth/users").json()] == ["alice"]


class TestOnlyTheSuperAdminReachesAdmin:
    def test_an_org_admin_is_403_everywhere_under_admin(self, stack: Stack) -> None:
        alice = _org_with_data(stack)
        assert alice.pipeline.get("/admin/organizations").status_code == 403
        assert alice.pipeline.post("/admin/organizations/acme/suspend").status_code == 403
        assert alice.pipeline.delete("/admin/organizations/acme?confirm=acme").status_code == 403

    def test_logged_out_is_401(self, stack: Stack) -> None:
        assert stack.browser().pipeline.get("/admin/organizations").status_code == 401


class TestListingAndCreating:
    def test_lists_every_org_with_counts_but_no_rows(self, stack: Stack) -> None:
        _org_with_data(stack, "Acme", "alice")
        stack.browser().register("Globex", "gina")
        orgs = _orgs(stack.superadmin())
        assert set(orgs) == {"acme", "globex"}
        acme = orgs["acme"]
        assert (acme["users"], acme["admins"], acme["alerts"]) == (1, 1, 1)
        assert acme["events"] == 2 and acme["suspended"] is False
        assert orgs["globex"]["events"] == 0
        assert "rows" not in acme and set(acme) == {
            "org_id", "name", "suspended", "created_at", "users", "admins", "events", "alerts"
        }  # fmt: skip

    def test_creates_an_org_whose_admin_can_log_in(self, stack: Stack) -> None:
        root = stack.superadmin()
        created = root.pipeline.post(
            "/admin/organizations",
            json={"organization": "Initech", "username": "dan", "password": "dan-pass-12"},
        )
        assert created.status_code == 201
        assert created.json()["org_id"] == "initech" and created.json()["admins"] == 1
        assert stack.browser().login("dan", "dan-pass-12").json()["role"] == "admin"

    def test_a_taken_name_is_409(self, stack: Stack) -> None:
        stack.browser().register("Acme", "alice")
        taken = stack.superadmin().pipeline.post(
            "/admin/organizations",
            json={"organization": "ACME", "username": "zed", "password": PASSWORD},
        )
        assert taken.status_code == 409

    def test_an_unknown_org_is_404(self, stack: Stack) -> None:
        root = stack.superadmin()
        assert root.pipeline.get("/admin/organizations/nope").status_code == 404
        assert root.pipeline.get("/admin/organizations/nope/users").status_code == 404


class TestSuspension:
    def test_suspending_signs_users_out_and_blocks_login_keeping_data(self, stack: Stack) -> None:
        alice = _org_with_data(stack)
        root = stack.superadmin()
        suspended = root.pipeline.post("/admin/organizations/acme/suspend")
        assert suspended.json()["suspended"] is True

        assert alice.read.get("/alerts").status_code == 401  # the live session is gone
        refused = stack.browser().login("alice")
        assert refused.status_code == 403 and "suspended" in refused.json()["detail"]
        # A wrong password still says only "invalid": suspension is not an existence oracle.
        assert stack.browser().login("alice", "wrong-pass").status_code == 401
        assert _orgs(root)["acme"]["alerts"] == 1  # nothing deleted

    def test_reactivating_restores_login_and_data(self, stack: Stack) -> None:
        _org_with_data(stack)
        root = stack.superadmin()
        root.pipeline.post("/admin/organizations/acme/suspend")
        assert (
            root.pipeline.post("/admin/organizations/acme/reactivate").json()["suspended"] is False
        )
        alice = stack.browser()
        assert alice.login("alice").status_code == 200
        assert len(alice.read.get("/alerts").json()) == 1

    def test_suspension_touches_only_that_org(self, stack: Stack) -> None:
        _org_with_data(stack, "Acme", "alice")
        stack.browser().register("Globex", "gina")
        stack.superadmin().pipeline.post("/admin/organizations/acme/suspend")
        assert stack.browser().login("gina").status_code == 200


class TestDeletion:
    def test_deleting_needs_the_id_repeated(self, stack: Stack) -> None:
        _org_with_data(stack)
        root = stack.superadmin()
        for confirm in ("", "Acme", "acme "):
            refused = root.pipeline.delete(f"/admin/organizations/acme?confirm={confirm}")
            assert refused.status_code == 400
        assert root.pipeline.delete("/admin/organizations/acme").status_code == 422  # no confirm
        assert "acme" in _orgs(root)

    def test_deleting_removes_the_org_its_users_and_all_its_data(self, stack: Stack) -> None:
        alice = _org_with_data(stack, "Acme", "alice")
        globex = _org_with_data(stack, "Globex", "gina")  # same device id, other tenant
        root = stack.superadmin()
        assert root.pipeline.delete("/admin/organizations/acme?confirm=acme").status_code == 204

        assert set(_orgs(root)) == {"globex"}
        assert alice.read.get("/alerts").status_code == 401
        assert stack.browser().login("alice").status_code == 401
        # The name is free again, and starts empty — nothing of the old tenant survived.
        reborn = stack.browser()
        assert reborn.register("Acme", "alice2").status_code == 201
        assert reborn.read.get("/alerts").json() == []
        assert reborn.read.get("/assets").json() == []
        # The other tenant is untouched.
        assert len(globex.read.get("/alerts").json()) == 1

    def test_deleting_an_unknown_org_is_404(self, stack: Stack) -> None:
        root = stack.superadmin()
        assert root.pipeline.delete("/admin/organizations/nope?confirm=nope").status_code == 404


class TestManagingUsersOfAnyOrg:
    def _setup(self, stack: Stack) -> tuple[Browser, dict[str, Any]]:
        stack.browser().register("Acme", "alice")
        root = stack.superadmin()
        bob = root.pipeline.post(
            "/admin/organizations/acme/users", json={"username": "bob", "password": "bob-temp-1"}
        )
        assert bob.status_code == 201
        return root, bob.json()

    def test_lists_and_adds_users(self, stack: Stack) -> None:
        root, bob = self._setup(stack)
        assert bob["role"] == "member"
        users = root.pipeline.get("/admin/organizations/acme/users").json()
        assert [u["username"] for u in users] == ["alice", "bob"]
        assert stack.browser().login("bob", "bob-temp-1").status_code == 200

    def test_promotes_and_demotes_but_never_the_last_admin(self, stack: Stack) -> None:
        root, bob = self._setup(stack)
        users = {
            u["username"]: u for u in root.pipeline.get("/admin/organizations/acme/users").json()
        }
        alice_url = f"/admin/organizations/acme/users/{users['alice']['user_id']}"
        bob_url = f"/admin/organizations/acme/users/{bob['user_id']}"

        refused = root.pipeline.patch(alice_url, json={"role": "member"})
        assert refused.status_code == 409 and "only admin" in refused.json()["detail"]
        assert root.pipeline.patch(bob_url, json={"role": "admin"}).json()["role"] == "admin"
        assert root.pipeline.patch(alice_url, json={"role": "member"}).json()["role"] == "member"

    def test_superadmin_is_not_a_role_that_can_be_granted(self, stack: Stack) -> None:
        root, bob = self._setup(stack)
        url = f"/admin/organizations/acme/users/{bob['user_id']}"
        assert root.pipeline.patch(url, json={"role": "superadmin"}).status_code == 422
        new = {"username": "eve", "password": PASSWORD, "role": "superadmin"}
        assert root.pipeline.post("/admin/organizations/acme/users", json=new).status_code == 422

    def test_resetting_a_password_signs_the_user_out(self, stack: Stack) -> None:
        root, bob = self._setup(stack)
        bobs_browser = stack.browser()
        bobs_browser.login("bob", "bob-temp-1")
        reset = root.pipeline.post(
            f"/admin/organizations/acme/users/{bob['user_id']}/password",
            json={"password": "fresh-temp-2"},
        )
        assert reset.status_code == 204
        assert bobs_browser.read.get("/alerts").status_code == 401
        assert stack.browser().login("bob", "bob-temp-1").status_code == 401
        assert stack.browser().login("bob", "fresh-temp-2").status_code == 200

    def test_removes_a_user_but_not_the_last_admin(self, stack: Stack) -> None:
        root, bob = self._setup(stack)
        users = {
            u["username"]: u for u in root.pipeline.get("/admin/organizations/acme/users").json()
        }
        last_admin = f"/admin/organizations/acme/users/{users['alice']['user_id']}"
        assert root.pipeline.delete(last_admin).status_code == 409
        assert (
            root.pipeline.delete(f"/admin/organizations/acme/users/{bob['user_id']}").status_code
            == 204
        )
        assert stack.browser().login("bob", "bob-temp-1").status_code == 401

    def test_a_user_addressed_through_the_wrong_org_is_404(self, stack: Stack) -> None:
        root, bob = self._setup(stack)
        stack.browser().register("Globex", "gina")
        wrong = f"/admin/organizations/globex/users/{bob['user_id']}"
        assert root.pipeline.patch(wrong, json={"role": "admin"}).status_code == 404
        assert root.pipeline.delete(wrong).status_code == 404


class TestRegistrationSwitch:
    def test_open_by_default(self, stack: Stack) -> None:
        browser = stack.browser()
        assert browser.pipeline.get("/auth/options").json() == {"open_registration": True}
        assert browser.register("Acme", "alice").status_code == 201

    @pytest.mark.parametrize("value", ["0", "false", "off", "no"])
    def test_closed_refuses_public_registration_but_not_the_super_admin(
        self, stack: Stack, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("IOTSHIELD_OPEN_REGISTRATION", value)
        browser = stack.browser()
        assert browser.pipeline.get("/auth/options").json() == {"open_registration": False}
        refused = browser.register("Acme", "alice")
        assert refused.status_code == 403 and "closed" in refused.json()["detail"]
        created = stack.superadmin().pipeline.post(
            "/admin/organizations",
            json={"organization": "Acme", "username": "alice", "password": PASSWORD},
        )
        assert created.status_code == 201


class TestASuperAdminWithAHomeOrganization:
    """The seeded shape: admin@admin.com manages everything and is at home in UCY."""

    def _home(self, stack: Stack) -> Browser:
        ucy = stack.browser()
        assert ucy.register("UCY", "ucy-admin").status_code == 201
        assert ucy.run_demo().status_code == 200
        return stack.superadmin("admin@admin.com", org_id="ucy")

    def test_sees_its_home_data_and_only_that(self, stack: Stack) -> None:
        _org_with_data(stack, "Acme", "alice")
        root = self._home(stack)
        me = root.pipeline.get("/auth/me").json()
        assert me["role"] == "superadmin" and me["organization"]["org_id"] == "ucy"
        assert len(root.read.get("/alerts").json()) == 1  # UCY's, not Acme's
        assert root.read.get("/summary").json()["counts"]["alerts"] == 1
        assert {o["org_id"] for o in root.pipeline.get("/admin/organizations").json()} == {
            "acme",
            "ucy",
        }  # still manages every organization

    def test_can_run_pipelines_and_manage_home_users(self, stack: Stack) -> None:
        root = self._home(stack)
        assert root.run_demo().status_code == 200
        assert len(root.read.get("/alerts").json()) == 2
        added = root.pipeline.post("/auth/users", json={"username": "carol", "password": PASSWORD})
        assert added.status_code == 201

    def test_its_home_cannot_be_suspended_or_deleted(self, stack: Stack) -> None:
        root = self._home(stack)
        assert root.pipeline.post("/admin/organizations/ucy/suspend").status_code == 409
        assert root.pipeline.delete("/admin/organizations/ucy?confirm=ucy").status_code == 409
        assert root.pipeline.get("/auth/me").status_code == 200  # still signed in

    def test_the_home_orgs_admin_cannot_remove_it(self, stack: Stack) -> None:
        root = self._home(stack)
        ucy_admin = stack.browser()
        ucy_admin.login("ucy-admin")
        users = {u["username"]: u for u in ucy_admin.pipeline.get("/auth/users").json()}
        assert users["admin@admin.com"]["role"] == "superadmin"
        removed = ucy_admin.pipeline.delete(f"/auth/users/{users['admin@admin.com']['user_id']}")
        assert removed.status_code == 403
        assert root.pipeline.get("/auth/me").status_code == 200

    def test_admin_routes_cannot_demote_reset_or_remove_it(self, stack: Stack) -> None:
        root = self._home(stack)
        users = {
            u["username"]: u for u in root.pipeline.get("/admin/organizations/ucy/users").json()
        }
        url = f"/admin/organizations/ucy/users/{users['admin@admin.com']['user_id']}"
        assert root.pipeline.patch(url, json={"role": "member"}).status_code == 409
        assert (
            root.pipeline.post(f"{url}/password", json={"password": "other-pass"}).status_code
            == 409
        )
        assert root.pipeline.delete(url).status_code == 409
