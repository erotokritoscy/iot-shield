"""API test harness: a TestClient over a file-backed SQLite app, plus a session
factory on the same database for seeding through the repository."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from iotshield.api import create_app
from iotshield.storage import make_engine, make_session_factory


@dataclass
class ApiHarness:
    client: TestClient
    session_factory: sessionmaker[Session]


@pytest.fixture()
def api(tmp_path: Path) -> Iterator[ApiHarness]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    app = create_app(engine)
    with TestClient(app) as client:
        yield ApiHarness(client=client, session_factory=make_session_factory(engine))
    engine.dispose()
