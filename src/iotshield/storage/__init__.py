"""Persistence: SQLAlchemy models mirroring the contracts + repository functions.

Postgres in compose; SQLite in tests. Malformed inputs are stored, never dropped.
"""
