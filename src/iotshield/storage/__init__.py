"""Persistence: SQLAlchemy tables mirroring the contracts + repository functions.

Postgres in compose, SQLite in tests — same code via the engine URL. Malformed
inputs are stored, never dropped. Asset rows are auto-discovered from data;
registration enriches them (ADR-0005). Callers use the repository functions and
speak contract objects only. Every data row belongs to one organization (ADR-0039);
``accounts`` holds the organizations, users and sessions that decide which.
"""

from iotshield.storage import accounts, data_sources, repository
from iotshield.storage.accounts import DEFAULT_ORG_ID
from iotshield.storage.db import (
    DATABASE_URL_ENV,
    create_schema,
    database_url_from_env,
    describe_location,
    make_engine,
    make_session_factory,
)

__all__ = [
    "DATABASE_URL_ENV",
    "DEFAULT_ORG_ID",
    "accounts",
    "create_schema",
    "data_sources",
    "database_url_from_env",
    "describe_location",
    "make_engine",
    "make_session_factory",
    "repository",
]
