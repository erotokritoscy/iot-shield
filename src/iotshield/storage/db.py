"""Engine and session plumbing — no import-time side effects.

Whoever runs the app supplies the database URL: tests pass
``sqlite+pysqlite:///:memory:``, compose will pass the Postgres URL. There is
deliberately no default URL — config must say where the database is.
"""

import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy import event as sa_event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from iotshield.storage.orm import Base

DATABASE_URL_ENV = "IOTSHIELD_DATABASE_URL"
"""Env var naming the database (Postgres in compose, SQLite on the host).

The one config value read from the environment: the API service and the
schema-init step require it, and the pipeline config lets it override the file's
``database_url``. Named once so the operator-facing contract lives in one place.
"""


def _enable_sqlite_fks(dbapi_connection: Any, _connection_record: Any) -> None:
    # SQLite ships with foreign-key enforcement OFF per connection; without this
    # pragma the FK integrity our tests rely on would silently not be tested.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(url: str) -> Engine:
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        sa_event.listen(engine, "connect", _enable_sqlite_fks)
    return engine


def create_schema(engine: Engine) -> None:
    """Create all tables. MVP uses create_all instead of migrations (ADR-0005)."""
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def database_url_from_env() -> str | None:
    """The database URL from the environment, or None after printing the error.

    Shared by the entrypoints that require it (the API service and the schema-init
    step) so the env-var name and the missing-variable message live in one place;
    each caller maps None to exit code 2.
    """
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        print(f"error: {DATABASE_URL_ENV} is not set", file=sys.stderr)
    return url


def describe_location(url: str) -> str:
    """Credential-safe, absolute description of where the database lives.

    SQLite file URLs are cwd-relative, so two processes launched from different
    directories silently open different files (empty dashboard over a populated
    run). Resolving to an absolute path makes that visible in the run report and
    API startup log; compose removes the hazard by sharing one database.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return parsed.render_as_string(hide_password=True)
    database = parsed.database
    if database and database != ":memory:":
        return str(Path(database).resolve())
    return url
