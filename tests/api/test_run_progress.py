"""``GET /runs/progress/{id}`` and the board behind it (ADR-0051).

The board is tested directly for what a poll sees *during* a run (a synchronous TestClient
cannot poll while its own request is running), and the route through the logged-in stack for
what crosses HTTP: the finished state, and the tenant boundary — a progress id is not a
capability, so another organization's finds nothing.
"""

from typing import Any
from uuid import UUID, uuid4

from iotshield.api.progress import KEEP_FINISHED_S, MAX_ENTRIES, ProgressBoard
from iotshield.pipeline.runner import RunProgress
from tests.api.conftest import RUN_SPEC, STUB_CSV, Browser, Stack


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class TestBoard:
    def test_a_poll_reads_the_latest_step_and_the_time_since_the_start(self) -> None:
        clock = Clock()
        board = ProgressBoard(now=clock)
        run = uuid4()
        assert board.start(run, org_id="acme")
        first = board.read(run, org_id="acme")
        assert first is not None and (first.phase, first.done, first.total) == ("starting", 0, None)

        clock.now += 12.34
        board.update(run, RunProgress("reading", 4_000, 10_000))
        info = board.read(run, org_id="acme")
        assert info is not None
        assert (info.phase, info.done, info.total, info.elapsed_s) == (
            "reading",
            4_000,
            10_000,
            12.3,
        )

    def test_the_clock_stops_when_the_run_ends(self) -> None:
        clock = Clock()
        board = ProgressBoard(now=clock)
        run = uuid4()
        board.start(run, org_id="acme")
        clock.now += 5
        board.end(run, failed=False)
        clock.now += 30
        info = board.read(run, org_id="acme")
        assert info is not None and (info.phase, info.elapsed_s) == ("finished", 5.0)
        board.update(run, RunProgress("judging", 1, 2))  # a late report changes nothing
        assert board.read(run, org_id="acme") == info

    def test_a_failure_is_reported_as_failed(self) -> None:
        board = ProgressBoard()
        run = uuid4()
        board.start(run, org_id="acme")
        board.end(run, failed=True)
        info = board.read(run, org_id="acme")
        assert info is not None and info.phase == "failed"

    def test_another_organization_reads_nothing_and_cannot_take_the_id(self) -> None:
        board = ProgressBoard()
        run = uuid4()
        board.start(run, org_id="acme")
        assert board.read(run, org_id="globex") is None
        assert not board.start(run, org_id="globex")
        board.end(run, failed=False)
        assert not board.start(run, org_id="globex"), "nor once it has finished"

    def test_a_live_id_cannot_be_restarted_but_a_finished_one_can(self) -> None:
        board = ProgressBoard()
        run = uuid4()
        assert board.start(run, org_id="acme")
        assert not board.start(run, org_id="acme")
        board.end(run, failed=False)
        assert board.start(run, org_id="acme")

    def test_finished_entries_are_forgotten_and_the_board_is_bounded(self) -> None:
        clock = Clock()
        board = ProgressBoard(now=clock)
        old = uuid4()
        board.start(old, org_id="acme")
        board.end(old, failed=False)
        clock.now += KEEP_FINISHED_S + 1
        board.start(uuid4(), org_id="acme")  # pruning happens on the way in
        assert board.read(old, org_id="acme") is None

        ids = [uuid4() for _ in range(MAX_ENTRIES + 5)]
        for run in ids:
            board.start(run, org_id="acme")
        assert board.read(ids[-1], org_id="acme") is not None
        assert board.read(ids[0], org_id="acme") is None


def _run(browser: Browser, progress_id: UUID | None) -> Any:
    body: dict[str, Any] = {"spec": RUN_SPEC, "inputs": [{"content": STUB_CSV}]}
    if progress_id is not None:
        body["progress_id"] = str(progress_id)
    return browser.pipeline.post("/runs", json=body)


class TestRoute:
    def test_a_finished_run_reads_back_as_finished(self, stack: Stack) -> None:
        analyst = stack.browser()
        assert analyst.register("Acme", "ana@example.com").status_code == 201
        progress_id = uuid4()
        assert _run(analyst, progress_id).status_code == 200
        response = analyst.pipeline.get(f"/runs/progress/{progress_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["phase"] == "finished"
        assert body["elapsed_s"] >= 0

    def test_a_run_without_a_progress_id_runs_as_before(self, stack: Stack) -> None:
        analyst = stack.browser()
        assert analyst.register("Acme", "ana@example.com").status_code == 201
        assert _run(analyst, None).status_code == 200

    def test_an_unknown_id_is_404_and_another_organizations_too(self, stack: Stack) -> None:
        owner, outsider = stack.browser(), stack.browser()
        assert owner.register("Acme", "ana@example.com").status_code == 201
        assert outsider.register("Globex", "bob@example.com").status_code == 201
        progress_id = uuid4()
        assert _run(owner, progress_id).status_code == 200

        assert outsider.pipeline.get(f"/runs/progress/{progress_id}").status_code == 404
        assert owner.pipeline.get(f"/runs/progress/{uuid4()}").status_code == 404
        # Nor can the outsider post a run under the owner's id and so read it.
        assert _run(outsider, progress_id).status_code == 409

    def test_an_id_still_running_is_refused(self, stack: Stack) -> None:
        analyst = stack.browser()
        assert analyst.register("Acme", "ana@example.com").status_code == 201
        progress_id = uuid4()
        board: ProgressBoard = stack.pipeline_app.state.progress
        org_id = analyst.pipeline.get("/auth/me").json()["organization"]["org_id"]
        assert board.start(progress_id, org_id=org_id)  # a run under this id is still going
        assert _run(analyst, progress_id).status_code == 409

    def test_a_refused_run_reads_back_as_failed(self, stack: Stack) -> None:
        analyst = stack.browser()
        assert analyst.register("Acme", "ana@example.com").status_code == 201
        progress_id = uuid4()
        bad = RUN_SPEC | {"detectors": [{"name": "no_such_detector"}]}
        response = analyst.pipeline.post(
            "/runs",
            json={
                "spec": bad,
                "inputs": [{"content": STUB_CSV}],
                "progress_id": str(progress_id),
            },
        )
        assert response.status_code == 422
        progress = analyst.pipeline.get(f"/runs/progress/{progress_id}").json()
        assert progress["phase"] == "failed"
