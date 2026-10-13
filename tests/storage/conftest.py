from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from iotshield.storage import (
    DEFAULT_ORG_ID,
    create_schema,
    make_engine,
    make_session_factory,
    repository,
)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    factory = make_session_factory(engine)
    with factory() as sess:
        # Every data row belongs to an organization (ADR-0039); the tests write as the default.
        repository.ensure_organization(sess, DEFAULT_ORG_ID)
        yield sess
    engine.dispose()
