"""API test harness: a TestClient over a file-backed SQLite app, plus a session
factory on the same database for seeding through the repository.

``stack`` is the logged-in variant (ADR-0039/0040): both apps with ``auth=True`` over one
SQLite file, as compose runs them over one Postgres. A ``Browser`` is a pair of TestClients
sharing one session cookie — the read API on one, the Pipeline API on the other — which is
what nginx's single origin gives the real dashboard."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from iotshield.api import create_app
from iotshield.api._auth import SESSION_COOKIE
from iotshield.api.control import create_control_app
from iotshield.auth.passwords import hash_password
from iotshield.storage import (
    DEFAULT_ORG_ID,
    accounts,
    create_schema,
    make_engine,
    make_session_factory,
    repository,
)


@dataclass
class ApiHarness:
    client: TestClient
    session_factory: sessionmaker[Session]


@pytest.fixture()
def api(tmp_path: Path) -> Iterator[ApiHarness]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    create_schema(engine)  # the API no longer self-creates it (ADR-0010)
    factory = make_session_factory(engine)
    # With auth off every request is the default organization, so seeded rows go there.
    with factory() as session, session.begin():
        repository.ensure_organization(session, DEFAULT_ORG_ID)
    app = create_app(engine, auth=False)
    with TestClient(app) as client:
        yield ApiHarness(client=client, session_factory=factory)
    engine.dispose()


STUB_CSV = (
    "2026-07-07T12:00:00+00:00,pump-02,Pressure,45.0\n"
    "2026-07-07T12:01:00+00:00,pump-02,Pressure,96.0\n"
)
RUN_SPEC: dict[str, Any] = {
    "source_format": "stub-csv",
    "normalizer": "stub",
    "detectors": [{"name": "stub_threshold", "params": {"threshold": 80.0, "lane": "telemetry"}}],
    "alert_engine": {"name": "stub"},
}
PASSWORD = "s3cret-pass"


@dataclass
class Browser:
    """One logged-in (or not) user: the same cookie jar on both APIs."""

    read: TestClient
    pipeline: TestClient

    def adopt_session(self) -> None:
        token = self.pipeline.cookies.get(SESSION_COOKIE)
        if token is None:
            self.read.cookies.clear()
        else:
            self.read.cookies.set(SESSION_COOKIE, token)

    def register(self, org: str, username: str, password: str = PASSWORD) -> Any:
        response = self.pipeline.post(
            "/auth/register",
            json={"organization": org, "username": username, "password": password},
        )
        self.adopt_session()
        return response

    def login(self, username: str, password: str = PASSWORD) -> Any:
        response = self.pipeline.post(
            "/auth/login", json={"username": username, "password": password}
        )
        self.adopt_session()
        return response

    def run_demo(self) -> Any:
        return self.pipeline.post(
            "/runs", json={"spec": RUN_SPEC, "inputs": [{"content": STUB_CSV}]}
        )


@dataclass
class Stack:
    read_app: FastAPI
    pipeline_app: FastAPI
    engine: Engine
    clients: list[TestClient]

    def browser(self) -> Browser:
        read = TestClient(self.read_app)
        pipeline = TestClient(self.pipeline_app)
        self.clients += [read, pipeline]
        return Browser(read=read, pipeline=pipeline)

    def superadmin(
        self, username: str = "root", password: str = PASSWORD, *, org_id: str | None = None
    ) -> Browser:
        """A browser logged in as a super admin — created as the CLI or the seed would, never
        over HTTP. ``org_id`` gives it a home organization."""
        with make_session_factory(self.engine)() as session:
            accounts.create_superadmin(
                session, username=username, password_hash=hash_password(password), org_id=org_id
            )
            session.commit()
        browser = self.browser()
        assert browser.login(username, password).status_code == 200
        return browser


@pytest.fixture()
def stack(tmp_path: Path) -> Iterator[Stack]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'auth.db').as_posix()}")
    create_schema(engine)
    built = Stack(
        read_app=create_app(engine, auth=True),
        pipeline_app=create_control_app(engine, auth=True),
        engine=engine,
        clients=[],
    )
    yield built
    for client in built.clients:
        client.close()
    engine.dispose()
