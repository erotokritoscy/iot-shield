from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from iotshield.storage import create_schema, make_engine, make_session_factory


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess
    engine.dispose()
