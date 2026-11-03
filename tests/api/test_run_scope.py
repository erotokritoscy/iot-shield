"""A run's results read back as a set (ADR-0045).

The joint under test spans both APIs: ``POST /runs`` returns a ``run_id``, and every list route
of the read API narrows to exactly that run's rows when given it — not the organization's
whole store, which is what the dashboard's run tiles used to open. Driven through the
logged-in stack so the tenant boundary is exercised too: a run id is not a capability, and
another organization's id must find nothing.
"""

from typing import Any

from tests.api.conftest import RUN_SPEC, STUB_CSV, Browser, Stack

# One unparseable line beside the stub rows, so the malformed route has something to scope.
CSV_WITH_GARBAGE = STUB_CSV + "garbage\n"
SCOPED_ROUTES = ("/events", "/malformed", "/detections", "/alerts")


def _run(browser: Browser) -> dict[str, Any]:
    response = browser.pipeline.post(
        "/runs", json={"spec": RUN_SPEC, "inputs": [{"content": CSV_WITH_GARBAGE}]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _counts(browser: Browser, run_id: str) -> dict[str, int]:
    return {
        route: len(browser.read.get(route, params={"run_id": run_id}).json())
        for route in SCOPED_ROUTES
    }


def _expected(body: dict[str, Any]) -> dict[str, int]:
    return {f"/{key}": body[key] for key in ("events", "malformed", "detections", "alerts")}


def test_each_run_reads_back_exactly_its_own_rows(stack: Stack) -> None:
    analyst = stack.browser()
    assert analyst.register("Acme", "ana").status_code == 201
    first, second = _run(analyst), _run(analyst)

    assert first["run_id"] != second["run_id"]
    # Every count is non-zero, so "matches" cannot be satisfied by two empty answers.
    assert all(_expected(first).values())
    assert _counts(analyst, first["run_id"]) == _expected(first)
    assert _counts(analyst, second["run_id"]) == _expected(second)
    # Unfiltered, the store holds both runs — the filter is what made the answers differ.
    assert len(analyst.read.get("/events").json()) == first["events"] + second["events"]


def test_a_scoped_read_returns_that_runs_ids(stack: Stack) -> None:
    analyst = stack.browser()
    assert analyst.register("Acme", "ana").status_code == 201
    body = _run(analyst)
    _run(analyst)  # a second run's alert must not appear under the first one's id

    alerts = analyst.read.get("/alerts", params={"run_id": body["run_id"]}).json()
    assert [a["alert_id"] for a in alerts] == [a["alert_id"] for a in body["top_alerts"]]


def test_another_organizations_run_id_finds_nothing(stack: Stack) -> None:
    owner, outsider = stack.browser(), stack.browser()
    assert owner.register("Acme", "ana").status_code == 201
    assert outsider.register("Globex", "bob").status_code == 201
    body = _run(owner)

    assert _counts(outsider, body["run_id"]) == dict.fromkeys(SCOPED_ROUTES, 0)


def test_a_malformed_run_id_is_422(stack: Stack) -> None:
    analyst = stack.browser()
    assert analyst.register("Acme", "ana").status_code == 201
    for route in SCOPED_ROUTES:
        assert analyst.read.get(route, params={"run_id": "not-a-uuid"}).status_code == 422
