"""Saved runs (ADR-0046): every run is recorded, named, listable, renamable, deletable by an admin.

Driven through the logged-in stack, because who ran it, who may delete it and whose list it
appears in are all decided by the session — none of them by the request body.
"""

from typing import Any
from uuid import UUID

from iotshield.storage import make_session_factory, runs
from tests.api.conftest import RUN_SPEC, STUB_CSV, Browser, Stack

SCOPED_ROUTES = ("/events", "/malformed", "/detections", "/alerts")
CSV = STUB_CSV + "garbage\n"


def _run(browser: Browser, name: str | None = None, origin: str = "pumps.csv") -> dict[str, Any]:
    body: dict[str, Any] = {"spec": RUN_SPEC, "inputs": [{"content": CSV, "origin": origin}]}
    if name is not None:
        body["name"] = name
    response = browser.pipeline.post("/runs", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _admin(stack: Stack, org: str = "Acme", username: str = "ana@example.com") -> Browser:
    browser = stack.browser()
    assert browser.register(org, username).status_code == 201
    return browser


def _member(stack: Stack, admin: Browser) -> Browser:
    created = admin.pipeline.post(
        "/auth/users", json={"username": "bob@example.com", "password": "temp-pass-1"}
    )
    assert created.status_code == 201
    member = stack.browser()
    assert member.login("bob@example.com", "temp-pass-1").status_code == 200
    return member


def _stored(browser: Browser, run_id: str) -> dict[str, int]:
    return {
        route: len(browser.read.get(route, params={"run_id": run_id}).json())
        for route in SCOPED_ROUTES
    }


class TestSaving:
    def test_a_run_is_saved_with_its_name_author_chain_and_counts(self, stack: Stack) -> None:
        admin = _admin(stack)
        result = _run(admin, name="  Night shift\tcheck  ")

        saved = admin.pipeline.get(f"/runs/{result['run_id']}").json()
        assert saved["name"] == "Night shift check"  # whitespace collapsed, not kept verbatim
        assert saved["created_by"] == "ana@example.com"
        assert {key: saved[key] for key in ("events", "malformed", "detections", "alerts")} == {
            key: result[key] for key in ("events", "malformed", "detections", "alerts")
        }
        assert saved["sources"] == [
            {"normalizer": "stub", "source_format": "stub-csv", "origin": "pumps.csv", "pull": None}
        ]
        # The chain as it ran — reopenable — and never the deployment's database location.
        assert saved["spec"]["detectors"] == RUN_SPEC["detectors"]
        assert "database_url" not in saved["spec"]

    def test_an_unnamed_run_is_named_after_its_inputs_and_time(self, stack: Stack) -> None:
        admin = _admin(stack)
        for blank in (None, "   "):
            result = _run(admin, name=blank, origin="meters.csv")
            name = admin.pipeline.get(f"/runs/{result['run_id']}").json()["name"]
            assert name.startswith("meters.csv — ") and name.endswith(" UTC")

    def test_the_list_is_newest_first_and_counts_itself(self, stack: Stack) -> None:
        admin = _admin(stack)
        first, second = _run(admin, name="first"), _run(admin, name="second")
        response = admin.pipeline.get("/runs")
        assert [run["run_id"] for run in response.json()] == [second["run_id"], first["run_id"]]
        assert response.headers["X-Total-Count"] == "2"
        assert admin.pipeline.get("/runs", params={"limit": 1}).headers["X-Total-Count"] == "2"


class TestRenaming:
    def test_any_member_may_rename_a_run(self, stack: Stack) -> None:
        admin = _admin(stack)
        result = _run(admin)
        member = _member(stack, admin)
        response = member.pipeline.patch(f"/runs/{result['run_id']}", json={"name": "Renamed"})
        assert response.status_code == 200
        assert admin.pipeline.get(f"/runs/{result['run_id']}").json()["name"] == "Renamed"

    def test_a_blank_name_is_refused(self, stack: Stack) -> None:
        admin = _admin(stack)
        result = _run(admin)
        response = admin.pipeline.patch(f"/runs/{result['run_id']}", json={"name": "  "})
        assert response.status_code == 422


class TestDeleting:
    def test_an_admin_deletes_a_run_and_everything_it_stored(self, stack: Stack) -> None:
        admin = _admin(stack)
        doomed, kept = _run(admin), _run(admin)

        assert admin.pipeline.delete(f"/runs/{doomed['run_id']}").status_code == 204
        assert admin.pipeline.get(f"/runs/{doomed['run_id']}").status_code == 404
        assert _stored(admin, doomed["run_id"]) == dict.fromkeys(SCOPED_ROUTES, 0)
        # The other run is untouched, record and rows alike.
        assert admin.pipeline.get(f"/runs/{kept['run_id']}").status_code == 200
        assert _stored(admin, kept["run_id"])["/events"] == kept["events"]
        # Assets outlive the runs that saw them.
        assert [a["asset_id"] for a in admin.read.get("/assets").json()] == ["pump-02"]

    def test_a_member_may_not_delete_a_run(self, stack: Stack) -> None:
        admin = _admin(stack)
        result = _run(admin)
        member = _member(stack, admin)
        assert member.pipeline.delete(f"/runs/{result['run_id']}").status_code == 403
        assert admin.pipeline.get(f"/runs/{result['run_id']}").status_code == 200


class TestTenancy:
    def test_another_organizations_run_is_not_found_by_any_route(self, stack: Stack) -> None:
        owner, outsider = _admin(stack), _admin(stack, "Globex", "gus@example.com")
        result = _run(owner)
        path = f"/runs/{result['run_id']}"

        assert outsider.pipeline.get("/runs").json() == []
        assert outsider.pipeline.get(path).status_code == 404
        assert outsider.pipeline.patch(path, json={"name": "mine now"}).status_code == 404
        assert outsider.pipeline.delete(path).status_code == 404
        assert owner.pipeline.get(path).json()["name"] != "mine now"

    def test_deleting_an_organization_deletes_its_runs(self, stack: Stack) -> None:
        owner, other = _admin(stack), _admin(stack, "Globex", "gus@example.com")
        _run(owner)
        kept = _run(other)
        root = stack.superadmin()
        response = root.pipeline.delete("/admin/organizations/acme", params={"confirm": "acme"})
        assert response.status_code == 204, response.text
        with make_session_factory(stack.engine)() as session:
            assert len(runs.list_runs(session, org_id="acme")) == 0
            assert [r.run_id for r in runs.list_runs(session, org_id="globex")] == [
                UUID(kept["run_id"])
            ]
