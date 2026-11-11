"""Saved jobs over the Pipeline API, run by the worker (ADR-0050).

The data comes from an HTTP API source behind a fake transport (ADR-0049), because it is the
one pull target a test can stand up without a database server; a database pull reaches a run
through the same ``PullTarget`` and the same ``api/execution.py``. What matters here: a job is
refused at save for what would refuse its run, "Run now" queues rather than runs, the worker
stores an ordinary saved run, a schedule fires once per slot and never stacks, a dead worker's
execution is failed, and one organization never reaches another's jobs.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from iotshield.api.control import create_control_app
from iotshield.connect.api import client as api_client
from iotshield.jobs.worker import Worker
from iotshield.storage import accounts, create_schema, make_engine, make_session_factory
from iotshield.storage import jobs as store
from iotshield.storage.orm import EventRow, JobExecutionRow, JobNotificationRow
from iotshield.storage.secrets import SECRET_KEY_ENV
from tests.api.conftest import Stack
from tests.api.test_api_sources import PLANT_API, Upstream, _resolve

NOW = datetime(2026, 11, 9, 1, 0, tzinfo=UTC)

SPEC: dict[str, Any] = {
    "sources": [
        {
            "normalizer": "passthrough",
            "source_format": "normalized-jsonl",
            "params": {"lane": "telemetry"},
        }
    ],
    "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0}}],
    "alert_engine": {"name": "stub"},
}


def job_body(source_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "name": "Nightly plant check",
        "spec": SPEC,
        "sources": [
            {
                "source_id": source_id,
                "window": {"kind": "fixed", "start": "2026-11-01", "end": "2026-11-01"},
            }
        ],
    } | changes


@pytest.fixture()
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[Upstream]:
    monkeypatch.setenv(SECRET_KEY_ENV, Fernet.generate_key().decode())
    fake = Upstream()
    api_client.use_transport(httpx.MockTransport(fake), _resolve)
    yield fake
    api_client.use_transport(None)


@pytest.fixture()
def engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = make_engine(f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def client(engine, upstream: Upstream) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    app = create_control_app(engine, auth=False)
    app.state.clock = lambda: NOW
    with TestClient(app) as test_client:
        yield test_client


WorkerAt = Callable[[datetime], Worker]


@pytest.fixture()
def worker_at(engine, tmp_path: Path) -> WorkerAt:  # type: ignore[no-untyped-def]
    def build(moment: datetime) -> Worker:
        return Worker(engine=engine, output_dir=tmp_path / "results", clock=lambda: moment)

    return build


@pytest.fixture()
def source_id(client: TestClient) -> str:
    created = client.post("/data-sources", json=PLANT_API)
    assert created.status_code == 201, created.text
    return created.json()["source_id"]


def _create(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/jobs", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _executions(client: TestClient, job_id: str) -> list[dict[str, Any]]:
    return client.get(f"/jobs/{job_id}/executions").json()


class TestSave:
    def test_a_job_without_a_schedule_waits_to_be_asked(
        self, client: TestClient, source_id: str, upstream: Upstream
    ) -> None:
        job = _create(client, job_body(source_id))
        assert (job["schedule"], job["schedule_text"], job["next_run_at"]) == (None, None, None)
        assert job["last"] is None and job["enabled"] is True
        assert upstream.requests == []  # checked, not read
        assert [j["job_id"] for j in client.get("/jobs").json()] == [job["job_id"]]

    def test_a_scheduled_job_knows_when_it_next_runs(
        self, client: TestClient, source_id: str
    ) -> None:
        job = _create(
            client, job_body(source_id, schedule={"every": "day", "hour": 2, "minute": 30})
        )
        assert job["next_run_at"] == "2026-11-09T02:30:00+00:00"
        assert job["schedule_text"] == "every day at 02:30 UTC"

    def test_a_disabled_job_keeps_its_schedule_but_is_not_due(
        self, client: TestClient, source_id: str
    ) -> None:
        job = _create(client, job_body(source_id, schedule={"every": "hour"}, enabled=False))
        assert job["schedule"]["every"] == "hour" and job["next_run_at"] is None

    @pytest.mark.parametrize(
        ("change", "why"),
        [
            (
                {"spec": SPEC | {"sources": [{"normalizer": "stub", "source_format": "stub-csv"}]}},
                "is read with 'stub'",
            ),
            (
                {
                    "spec": SPEC
                    | {
                        "sources": [
                            {
                                "normalizer": "passthrough",
                                "source_format": "normalized-jsonl",
                                "params": {"lane": "log"},
                            }
                        ]
                    }
                },
                "lane: telemetry",
            ),
            ({"spec": SPEC | {"sources": SPEC["sources"] * 2}}, "expected 2 pull(s)"),
            (
                {
                    "sources": [
                        {
                            "source_id": "00000000-0000-0000-0000-00000000dead",
                            "window": {"kind": "rolling", "days": 1},
                        }
                    ]
                },
                "source 1: data source not found",
            ),
            (
                {
                    "sources": [
                        {
                            "source_id": "SOURCE",
                            "window": {"kind": "fixed", "start": "2026-01-01", "end": "2026-12-31"},
                        }
                    ]
                },
                "at most 92",
            ),
        ],
    )
    def test_refuses_a_job_whose_run_would_be_refused(
        self, client: TestClient, source_id: str, change: dict[str, Any], why: str
    ) -> None:
        body = job_body(source_id) | change
        for source in body["sources"]:
            if source["source_id"] == "SOURCE":
                source["source_id"] = source_id
        response = client.post("/jobs", json=body)
        assert response.status_code == 422 and why in response.text, response.text
        assert client.get("/jobs").json() == []

    def test_an_upload_cannot_be_a_job_input(self, client: TestClient, source_id: str) -> None:
        body = job_body(source_id, sources=[{"content": "a,b,c,d", "window": {"kind": "fixed"}}])
        assert client.post("/jobs", json=body).status_code == 422

    def test_an_edit_replaces_the_job_and_moves_its_next_run(
        self, client: TestClient, source_id: str
    ) -> None:
        job = _create(client, job_body(source_id))
        changed = client.put(
            f"/jobs/{job['job_id']}",
            json=job_body(source_id, name="Weekly", schedule={"every": "week", "weekday": 4}),
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["name"] == "Weekly"
        assert changed.json()["next_run_at"] == "2026-11-13T00:00:00+00:00"  # a Friday
        assert client.get(f"/jobs/{job['job_id']}").json()["name"] == "Weekly"


class TestRunNow:
    def test_run_now_queues_and_the_worker_stores_an_ordinary_run(
        self,
        client: TestClient,
        source_id: str,
        worker_at: WorkerAt,
        engine,  # type: ignore[no-untyped-def]
    ) -> None:
        job = _create(client, job_body(source_id))
        queued = client.post(f"/jobs/{job['job_id']}/run")
        assert queued.status_code == 202, queued.text
        assert (queued.json()["status"], queued.json()["trigger"]) == ("queued", "manual")
        assert client.post(f"/jobs/{job['job_id']}/run").status_code == 409  # never twice

        worker_at(NOW).tick()

        [done] = _executions(client, job["job_id"])
        assert done["status"] == "succeeded", done["error"]
        assert done["requested_by"] == "local"
        run = client.get(f"/runs/{done['run_id']}").json()
        assert run["name"] == "Nightly plant check · 2026-11-09 01:00 UTC"
        assert (run["events"], run["malformed"], run["detections"]) == (2, 1, 1)
        assert run["sources"][0]["pull"]["start"] == "2026-11-01"
        with make_session_factory(engine)() as session:
            assert len(session.scalars(select(EventRow)).all()) == 2
        assert client.get("/jobs").json()[0]["last"]["run_id"] == done["run_id"]
        assert client.post(f"/jobs/{job['job_id']}/run").status_code == 202  # free again

    def test_a_rolling_window_is_worked_out_when_the_job_runs(
        self, client: TestClient, source_id: str, upstream: Upstream, worker_at: WorkerAt
    ) -> None:
        rolling = [{"source_id": source_id, "window": {"kind": "rolling", "days": 3}}]
        job = _create(client, job_body(source_id, sources=rolling))
        client.post(f"/jobs/{job['job_id']}/run")
        worker_at(datetime(2026, 12, 1, 3, 0, tzinfo=UTC)).tick()
        params = upstream.requests[-1].url.params
        assert (params["from"], params["to"]) == ("2026-11-28", "2026-11-30")

    def test_a_failed_run_records_why_and_stores_nothing(
        self, client: TestClient, worker_at: WorkerAt
    ) -> None:
        wrong = client.post("/data-sources", json=PLANT_API | {"credentials": {"api_key": "x"}})
        job = _create(client, job_body(wrong.json()["source_id"]))
        client.post(f"/jobs/{job['job_id']}/run")
        worker_at(NOW).tick()
        [failed] = _executions(client, job["job_id"])
        assert failed["status"] == "failed" and failed["run_id"] is None
        assert "Plant API" in failed["error"] and "credentials" in failed["error"]
        assert client.get("/runs").json() == []

    def test_a_data_source_deleted_after_saving_fails_the_run_by_name(
        self, client: TestClient, source_id: str, worker_at: WorkerAt
    ) -> None:
        job = _create(client, job_body(source_id))
        assert client.delete(f"/data-sources/{source_id}").status_code == 204
        client.post(f"/jobs/{job['job_id']}/run")
        worker_at(NOW).tick()
        [failed] = _executions(client, job["job_id"])
        assert failed["status"] == "failed" and "data source not found" in failed["error"]

    def test_deleting_a_job_keeps_the_runs_it_made(
        self, client: TestClient, source_id: str, worker_at: WorkerAt
    ) -> None:
        job = _create(client, job_body(source_id))
        client.post(f"/jobs/{job['job_id']}/run")
        worker_at(NOW).tick()
        assert client.delete(f"/jobs/{job['job_id']}").status_code == 204
        assert client.get(f"/jobs/{job['job_id']}").status_code == 404
        assert len(client.get("/runs").json()) == 1


class TestSchedule:
    def test_a_due_job_runs_once_per_slot(
        self, client: TestClient, source_id: str, worker_at: WorkerAt
    ) -> None:
        job = _create(client, job_body(source_id, schedule={"every": "day", "hour": 2}))
        worker_at(NOW + timedelta(minutes=30)).tick()  # 01:30 — not yet
        assert _executions(client, job["job_id"]) == []

        at_two = datetime(2026, 11, 9, 2, 0, 5, tzinfo=UTC)
        worker_at(at_two).tick()
        worker_at(at_two + timedelta(minutes=1)).tick()  # same slot: nothing new
        [done] = _executions(client, job["job_id"])
        assert (done["trigger"], done["status"]) == ("schedule", "succeeded")
        assert client.get("/jobs").json()[0]["next_run_at"] == "2026-11-10T02:00:00+00:00"

    def test_a_worker_down_for_days_runs_a_missed_job_once(
        self, client: TestClient, source_id: str, worker_at: WorkerAt
    ) -> None:
        job = _create(client, job_body(source_id, schedule={"every": "hour"}))
        worker_at(NOW + timedelta(days=3)).tick()
        assert len(_executions(client, job["job_id"])) == 1

    def test_a_slot_that_comes_while_the_job_is_busy_is_skipped_not_stacked(
        self, client: TestClient, source_id: str, worker_at: WorkerAt
    ) -> None:
        job = _create(client, job_body(source_id, schedule={"every": "day", "hour": 2}))
        client.post(f"/jobs/{job['job_id']}/run")  # queued, not yet run
        assert worker_at(datetime(2026, 11, 9, 2, 1, tzinfo=UTC)).schedule() == 0
        [only] = _executions(client, job["job_id"])
        assert only["trigger"] == "manual"
        assert client.get("/jobs").json()[0]["next_run_at"] == "2026-11-10T02:00:00+00:00"

    def test_a_disabled_job_is_never_queued(
        self, client: TestClient, source_id: str, worker_at: WorkerAt
    ) -> None:
        job = _create(client, job_body(source_id, schedule={"every": "hour"}, enabled=False))
        worker_at(NOW + timedelta(days=1)).tick()
        assert _executions(client, job["job_id"]) == []


class TestWorkerFailures:
    def test_a_running_execution_whose_worker_died_is_failed(
        self,
        client: TestClient,
        source_id: str,
        worker_at: WorkerAt,
        engine,  # type: ignore[no-untyped-def]
    ) -> None:
        job = _create(client, job_body(source_id))
        client.post(f"/jobs/{job['job_id']}/run")
        factory = make_session_factory(engine)
        with factory() as session:  # a worker claims it, then dies without a word
            assert store.claim_next(session, now=NOW) is not None
            session.commit()
        assert worker_at(NOW + timedelta(minutes=1)).recover() == 0  # still beating, maybe
        assert worker_at(NOW + timedelta(minutes=10)).recover() == 1
        [failed] = _executions(client, job["job_id"])
        assert failed["status"] == "failed" and "worker stopped" in failed["error"]

    def test_a_suspended_organizations_job_does_not_run(
        self,
        client: TestClient,
        source_id: str,
        worker_at: WorkerAt,
        engine,  # type: ignore[no-untyped-def]
        upstream: Upstream,
    ) -> None:
        job = _create(client, job_body(source_id))
        client.post(f"/jobs/{job['job_id']}/run")
        with make_session_factory(engine)() as session:
            accounts.set_suspended(session, "default", suspended=True)
            session.commit()
        upstream.requests.clear()
        worker_at(NOW).tick()
        [failed] = _executions(client, job["job_id"])
        assert failed["status"] == "failed" and "suspended" in failed["error"]
        assert upstream.requests == []


class TestTenancy:
    def test_another_organization_cannot_see_run_or_delete_a_job(
        self, stack: Stack, upstream: Upstream
    ) -> None:
        acme = stack.browser()
        acme.register("Acme", "alice@example.com")
        source = acme.pipeline.post("/data-sources", json=PLANT_API).json()["source_id"]
        job = acme.pipeline.post("/jobs", json=job_body(source))
        assert job.status_code == 201, job.text
        job_id = job.json()["job_id"]

        globex = stack.browser()
        globex.register("Globex", "gina@example.com")
        assert globex.pipeline.get("/jobs").json() == []
        for method, path in [
            ("GET", f"/jobs/{job_id}"),
            ("PUT", f"/jobs/{job_id}"),
            ("DELETE", f"/jobs/{job_id}"),
            ("POST", f"/jobs/{job_id}/run"),
            ("GET", f"/jobs/{job_id}/executions"),
        ]:
            body = job_body(source) if method == "PUT" else None
            response = globex.pipeline.request(method, path, json=body)
            assert response.status_code in {404, 422}, (method, path, response.text)
        # Nor can it point a job of its own at Acme's data source.
        stolen = globex.pipeline.post("/jobs", json=job_body(source))
        assert stolen.status_code == 422 and "data source not found" in stolen.text

    def test_a_member_runs_a_job_and_only_an_admin_deletes_one(
        self, stack: Stack, upstream: Upstream
    ) -> None:
        admin = stack.browser()
        admin.register("Acme", "alice@example.com")
        source = admin.pipeline.post("/data-sources", json=PLANT_API).json()["source_id"]
        admin.pipeline.post(
            "/auth/users", json={"username": "bob@example.com", "password": "temp-pass-1"}
        )
        member = stack.browser()
        member.login("bob@example.com", "temp-pass-1")
        job = member.pipeline.post("/jobs", json=job_body(source))
        assert job.status_code == 201, job.text
        job_id = job.json()["job_id"]
        assert job.json()["created_by"] == "bob@example.com"
        assert member.pipeline.post(f"/jobs/{job_id}/run").status_code == 202
        assert member.pipeline.delete(f"/jobs/{job_id}").status_code == 403
        assert admin.pipeline.delete(f"/jobs/{job_id}").status_code == 204

    def test_deleting_an_organization_deletes_its_jobs(
        self, stack: Stack, upstream: Upstream
    ) -> None:
        acme = stack.browser()
        acme.register("Acme", "alice@example.com")
        source = acme.pipeline.post("/data-sources", json=PLANT_API).json()["source_id"]
        job_id = acme.pipeline.post("/jobs", json=job_body(source)).json()["job_id"]
        acme.pipeline.post(f"/jobs/{job_id}/run")
        with make_session_factory(stack.engine)() as session:
            assert accounts.delete_organization(session, "acme")
            session.commit()
            assert session.scalars(select(JobExecutionRow)).all() == []
            assert store.list_jobs(session, org_id="acme") == []


@dataclass
class FakeMailer:
    """Stands in for SMTP: keeps what it was asked to send, or fails when told to."""

    sender: str = "iotshield@example.org"
    fail: bool = False
    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> None:
        if self.fail:
            raise ConnectionRefusedError("no SMTP server here")
        self.sent.append(message)


def _body(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain",))
    assert part is not None
    return part.get_content()


class TestEmails:
    """ADR-0052: each finished execution is emailed to the job's recipients."""

    @pytest.fixture()
    def mailer(self) -> FakeMailer:
        return FakeMailer()

    @pytest.fixture()
    def mailing_worker(self, engine, tmp_path: Path, mailer: FakeMailer) -> Worker:  # type: ignore[no-untyped-def]
        return Worker(
            engine=engine,
            output_dir=tmp_path / "results",
            clock=lambda: NOW,
            mailer=mailer,
            dashboard_url="http://localhost:8080/",
        )

    def test_recipients_are_saved_cleaned_and_kept_on_an_edit(
        self, client: TestClient, source_id: str
    ) -> None:
        body = job_body(source_id, notify_emails=[" Ops@Example.com", "ops@example.com", "b@x.io"])
        job = _create(client, body)
        assert job["notify_emails"] == ["ops@example.com", "b@x.io"]
        url = f"/jobs/{job['job_id']}"
        assert client.put(url, json=job_body(source_id)).json()["notify_emails"] == [
            "ops@example.com",
            "b@x.io",
        ]  # omitted on edit = unchanged
        cleared = client.put(url, json=job_body(source_id, notify_emails=[]))
        assert cleared.json()["notify_emails"] == []
        assert client.get(url).json()["notify_emails"] == []

    def test_a_recipient_that_is_not_an_address_is_422(
        self, client: TestClient, source_id: str
    ) -> None:
        response = client.post("/jobs", json=job_body(source_id, notify_emails=["ops"]))
        assert response.status_code == 422
        assert "not a valid email address" in response.text

    def test_the_creator_is_the_default_recipient(self, stack: Stack, upstream: Upstream) -> None:
        admin = stack.browser()
        admin.register("Acme", "alice@example.com")
        source = admin.pipeline.post("/data-sources", json=PLANT_API).json()["source_id"]
        job = admin.pipeline.post("/jobs", json=job_body(source)).json()
        assert job["notify_emails"] == ["alice@example.com"]
        nobody = admin.pipeline.post("/jobs", json=job_body(source, notify_emails=[])).json()
        assert nobody["notify_emails"] == []

    def test_without_login_nobody_is_mailed_by_default(
        self, client: TestClient, source_id: str
    ) -> None:
        # The auth-off user is "local", which is no address.
        assert _create(client, job_body(source_id))["notify_emails"] == []

    def test_a_success_is_mailed_with_its_counts_and_a_link(
        self,
        client: TestClient,
        source_id: str,
        mailing_worker: Worker,
        mailer: FakeMailer,
    ) -> None:
        job = _create(client, job_body(source_id, notify_emails=["ops@example.com"]))
        client.post(f"/jobs/{job['job_id']}/run")
        mailing_worker.tick()

        [done] = _executions(client, job["job_id"])
        [message] = mailer.sent
        assert message["To"] == "ops@example.com"
        assert message["From"] == "iotshield@example.org"
        assert message["Subject"] == "[iotshield] Job 'Nightly plant check' finished: 1 alert"
        text = _body(message)
        assert "events      2" in text and "malformed   1" in text and "detections  1" in text
        assert f"http://localhost:8080/runs/{done['run_id']}" in text
        assert "run now by local" in text

    def test_a_failure_is_mailed_with_its_reason(
        self, client: TestClient, mailing_worker: Worker, mailer: FakeMailer
    ) -> None:
        wrong = client.post("/data-sources", json=PLANT_API | {"credentials": {"api_key": "x"}})
        job = _create(client, job_body(wrong.json()["source_id"], notify_emails=["o@example.com"]))
        client.post(f"/jobs/{job['job_id']}/run")
        mailing_worker.tick()
        [message] = mailer.sent
        assert message["Subject"] == "[iotshield] Job 'Nightly plant check' failed"
        assert "credentials" in _body(message)

    def test_a_job_with_no_recipients_sends_nothing(
        self, client: TestClient, source_id: str, mailing_worker: Worker, mailer: FakeMailer
    ) -> None:
        job = _create(client, job_body(source_id))
        client.post(f"/jobs/{job['job_id']}/run")
        mailing_worker.tick()
        assert mailer.sent == []
        assert _executions(client, job["job_id"])[0]["status"] == "succeeded"

    def test_a_mail_that_cannot_be_sent_changes_nothing(
        self, client: TestClient, source_id: str, mailing_worker: Worker, mailer: FakeMailer
    ) -> None:
        mailer.fail = True
        job = _create(client, job_body(source_id, notify_emails=["ops@example.com"]))
        client.post(f"/jobs/{job['job_id']}/run")
        mailing_worker.tick()  # does not raise
        [done] = _executions(client, job["job_id"])
        assert done["status"] == "succeeded" and done["run_id"] is not None

    def test_a_scheduled_run_says_so(
        self,
        client: TestClient,
        source_id: str,
        engine,
        tmp_path: Path,
        mailer: FakeMailer,  # type: ignore[no-untyped-def]
    ) -> None:
        body = job_body(source_id, schedule={"every": "hour"}, notify_emails=["o@example.com"])
        _create(client, body)
        later = NOW + timedelta(hours=1)
        Worker(engine=engine, output_dir=tmp_path / "r", clock=lambda: later, mailer=mailer).tick()
        [message] = mailer.sent
        text = _body(message)
        assert "on its schedule" in text
        assert "Open the run" not in text  # no dashboard URL configured, so no link

    def test_deleting_a_job_or_its_organization_drops_its_recipients(
        self, stack: Stack, upstream: Upstream
    ) -> None:
        acme = stack.browser()
        acme.register("Acme", "alice@example.com")
        source = acme.pipeline.post("/data-sources", json=PLANT_API).json()["source_id"]
        first = acme.pipeline.post("/jobs", json=job_body(source)).json()["job_id"]
        acme.pipeline.post("/jobs", json=job_body(source, name="Second"))
        assert acme.pipeline.delete(f"/jobs/{first}").status_code == 204
        with make_session_factory(stack.engine)() as session:
            assert len(session.scalars(select(JobNotificationRow)).all()) == 1
            assert accounts.delete_organization(session, "acme")
            session.commit()
            assert session.scalars(select(JobNotificationRow)).all() == []
