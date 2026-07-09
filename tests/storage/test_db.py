"""describe_location: credential-safe, absolute description of the database URL,
so a cwd-relative SQLite path pointing two processes at different files is visible
rather than silently empty."""

from pathlib import Path

from iotshield.storage import describe_location


def test_sqlite_relative_url_resolves_to_absolute_path() -> None:
    location = describe_location("sqlite:///demo.db")
    assert Path(location).is_absolute()
    assert location.endswith("demo.db")


def test_sqlite_memory_url_shown_as_is() -> None:
    assert describe_location("sqlite+pysqlite:///:memory:") == "sqlite+pysqlite:///:memory:"


def test_postgres_url_password_is_redacted() -> None:
    location = describe_location("postgresql+psycopg://user:secret@host:5432/iotshield")
    assert "secret" not in location  # never leak the password into logs
    assert "iotshield" in location  # but the database is still identifiable
