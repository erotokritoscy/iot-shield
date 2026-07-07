"""Persistence: SQLAlchemy tables mirroring the contracts + repository functions.

Postgres in compose, SQLite in tests — same code via the engine URL. Malformed
inputs are stored, never dropped. Asset rows are auto-discovered from data;
registration enriches them (ADR-0005). Callers use the repository functions and
speak contract objects only.
"""

from iotshield.storage import repository
from iotshield.storage.db import create_schema, make_engine, make_session_factory

__all__ = ["create_schema", "make_engine", "make_session_factory", "repository"]
