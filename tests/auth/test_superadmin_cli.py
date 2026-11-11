"""The server-side command that creates super admins (ADR-0040)."""

import io
from pathlib import Path

import pytest

from iotshield.auth.passwords import verify_password
from iotshield.auth.superadmin import main
from iotshield.storage import accounts, make_engine, make_session_factory


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    monkeypatch.setenv("IOTSHIELD_DATABASE_URL", url)
    return url


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def _stored_hash(url: str, username: str) -> str | None:
    engine = make_engine(url)
    with make_session_factory(engine)() as session:
        found = accounts.credentials_for(session, username)
    engine.dispose()
    return found[1] if found is not None else None


def test_create_makes_a_super_admin_on_a_fresh_database(
    db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, "root-password\n")
    assert main(["create", "--username", "Root@example.com", "--password-stdin"]) == 0
    stored = _stored_hash(db, "root@example.com")
    assert stored is not None and verify_password("root-password", stored)
    assert main(["list"]) == 0
    assert capsys.readouterr().out.strip() == "root@example.com"


def test_a_short_password_or_bad_username_is_refused(
    db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, "short\n")
    assert main(["create", "--username", "root@example.com", "--password-stdin"]) == 2
    assert "at least 8" in capsys.readouterr().err
    _stdin(monkeypatch, "long-enough\n")
    assert main(["create", "--username", "a b", "--password-stdin"]) == 2
    _stdin(monkeypatch, "long-enough\n")
    assert main(["create", "--username", "root", "--password-stdin"]) == 2  # not an email
    assert "valid email address" in capsys.readouterr().err
    assert _stored_hash(db, "root@example.com") is None


def test_a_taken_username_is_refused(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _stdin(monkeypatch, "root-password\n")
    assert main(["create", "--username", "root@example.com", "--password-stdin"]) == 0
    _stdin(monkeypatch, "root-password\n")
    assert main(["create", "--username", "ROOT@EXAMPLE.COM", "--password-stdin"]) == 2


def test_set_password_replaces_it_for_super_admins_only(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stdin(monkeypatch, "root-password\n")
    main(["create", "--username", "root@example.com", "--password-stdin"])
    _stdin(monkeypatch, "new-root-password\n")
    assert main(["set-password", "--username", "root@example.com", "--password-stdin"]) == 0
    stored = _stored_hash(db, "root@example.com")
    assert stored is not None and verify_password("new-root-password", stored)
    _stdin(monkeypatch, "whatever-pass\n")
    assert main(["set-password", "--username", "nobody@example.com", "--password-stdin"]) == 2


def test_without_a_database_url_it_exits_two() -> None:
    assert main(["list"]) == 2
