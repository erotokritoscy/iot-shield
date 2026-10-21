"""Where each source database is: declared by the deployment, never by a request (ADR-0042).

``config/connections.yaml`` (``IOTSHIELD_CONNECTIONS_PATH`` overrides) names each database a
connector may read::

    connections:
      sensipro:
        connector: sensipro          # which schema this database holds (connect.CONNECTORS)
        driver: postgresql           # postgresql | mysql
        host: host.docker.internal
        port: 5433
        database: sensipro
        user: grafana
        password_env: SENSIPRO_DB_PASSWORD   # the variable holding it, never the password
        organizations: [ucy]         # which organizations may pull; "*" for every one

A data source refers to a connection **by name**, the stance ADR-0037 took for rule sets: if
a request could carry a host, any organization could aim the server at any address it can
reach (other databases, internal services) and read back the error to learn what answered.
The password stays in the environment, so neither the file nor a database row holds it.

Every session a connection opens is **read-only** — Postgres ``default_transaction_read_only``,
MySQL ``SET SESSION TRANSACTION READ ONLY`` — and time-limited, so a connector query that
went wrong can neither change the source's data nor hold its server.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL

from iotshield._paths import REPO_ROOT, configured_root

log = logging.getLogger(__name__)

CONNECTIONS_PATH_ENV: Final = "IOTSHIELD_CONNECTIONS_PATH"
DEFAULT_CONNECTIONS_PATH: Final = REPO_ROOT / "config" / "connections.yaml"

CONNECT_TIMEOUT_S: Final = 5
QUERY_TIMEOUT_S: Final = 30

_DRIVERS: Final = {"postgresql": "postgresql+psycopg", "mysql": "mysql+pymysql"}


class ConnectionUnavailable(Exception):
    """A connection cannot be used right now. The message is safe to show a client: it names
    the connection, never its host, user or password."""


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connector: str
    driver: Literal["postgresql", "mysql"]
    host: str
    port: int = Field(gt=0, lt=65536)
    database: str
    user: str
    password_env: str
    organizations: list[str] | Literal["*"]


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connections: dict[str, _Entry] = Field(default_factory=dict)


@dataclass(frozen=True)
class Connection:
    """One declared database. ``url`` carries no password; ``engine()`` adds it."""

    name: str
    connector: str
    url: URL
    password_env: str | None
    organizations: frozenset[str] | None
    """Which organizations may pull through it; None means every one."""

    def allows(self, org_id: str) -> bool:
        return self.organizations is None or org_id in self.organizations

    def engine(self) -> Engine:
        """A pooled, read-only, time-limited engine for this connection (built once)."""
        return _engine_for(self)


def connections_path() -> Path:
    return configured_root(CONNECTIONS_PATH_ENV, DEFAULT_CONNECTIONS_PATH)


def load_connections(path: Path | None = None) -> dict[str, Connection]:
    """Every declared connection, by name. No file means no connections, not an error.

    Raises ``ValueError`` naming the file for a file that exists and is malformed: a typo in
    deployment config should fail loudly, not quietly offer nothing.
    """
    source = path if path is not None else connections_path()
    if not source.is_file():
        return {}
    try:
        raw: Any = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        parsed = _File.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"{source} is not a valid connections file: {exc}") from exc
    return {name: _connection(name, entry) for name, entry in parsed.connections.items()}


def _connection(name: str, entry: _Entry) -> Connection:
    return Connection(
        name=name,
        connector=entry.connector,
        url=URL.create(
            _DRIVERS[entry.driver],
            username=entry.user,
            host=entry.host,
            port=entry.port,
            database=entry.database,
        ),
        password_env=entry.password_env,
        organizations=None if entry.organizations == "*" else frozenset(entry.organizations),
    )


_ENGINES: dict[tuple[str, str], Engine] = {}


def _engine_for(connection: Connection) -> Engine:
    key = (connection.name, connection.url.render_as_string(hide_password=True))
    engine = _ENGINES.get(key)
    if engine is None:
        engine = _build_engine(connection)
        _ENGINES[key] = engine
    return engine


def _password(connection: Connection) -> str | None:
    if connection.password_env is None:
        return None
    password = os.environ.get(connection.password_env)
    if password is None:
        raise ConnectionUnavailable(
            f"connection {connection.name!r} has no password configured on the server"
        )
    return password


def _build_engine(connection: Connection) -> Engine:
    url = connection.url
    password = _password(connection)
    if password is not None:
        url = url.set(password=password)
    backend = url.get_backend_name()
    connect_args: Mapping[str, Any]
    if backend == "postgresql":
        connect_args = {
            "connect_timeout": CONNECT_TIMEOUT_S,
            "options": (
                f"-c default_transaction_read_only=on -c statement_timeout={QUERY_TIMEOUT_S * 1000}"
            ),
        }
    elif backend == "mysql":
        connect_args = {"connect_timeout": CONNECT_TIMEOUT_S, "read_timeout": QUERY_TIMEOUT_S}
    else:
        connect_args = {}
    engine = create_engine(url, connect_args=dict(connect_args), pool_pre_ping=True)
    if backend == "mysql":
        # Two statements, so not `init_command` (one string, one statement): run on every new
        # DBAPI connection, before the pool hands it out.
        @event.listens_for(engine, "connect")
        def _read_only(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute(f"SET SESSION max_execution_time = {QUERY_TIMEOUT_S * 1000}")
            cursor.close()

    return engine


def register_engine(connection: Connection, engine: Engine) -> None:
    """Use ``engine`` for ``connection`` — for tests, which stand a SQLite file in for it."""
    _ENGINES[(connection.name, connection.url.render_as_string(hide_password=True))] = engine
