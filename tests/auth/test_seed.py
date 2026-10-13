"""The super admin seeder compose's init step runs (ADR-0040)."""

from pathlib import Path

import pytest

from iotshield.auth.passwords import hash_password, verify_password
from iotshield.auth.seed import DEMO_CONFIG_ENV, ORGANIZATION_ENV, PASSWORD_ENV, USERNAME_ENV, main
from iotshield.storage import (
    accounts,
    create_schema,
    make_engine,
    make_session_factory,
    repository,
)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{(tmp_path / 'seed.db').as_posix()}"
    monkeypatch.setenv("IOTSHIELD_DATABASE_URL", url)
    engine = make_engine(url)
    create_schema(engine)  # compose runs storage.init first
    engine.dispose()
    return url


def _found(url: str, username: str) -> tuple[accounts.User, str] | None:
    engine = make_engine(url)
    with make_session_factory(engine)() as session:
        found = accounts.credentials_for(session, username)
    engine.dispose()
    return found


def _seed(monkeypatch: pytest.MonkeyPatch, username: str, password: str) -> int:
    monkeypatch.setenv(USERNAME_ENV, username)
    monkeypatch.setenv(PASSWORD_ENV, password)
    return main()


def test_seeds_the_super_admin(
    db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _seed(monkeypatch, "admin@admin.com", "password") == 0
    found = _found(db, "admin@admin.com")
    assert found is not None
    user, stored = found
    assert (user.role, user.org_id) == ("superadmin", None)
    assert verify_password("password", stored)
    assert "well-known default password" in capsys.readouterr().err


def test_a_second_run_never_resets_a_changed_password(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(monkeypatch, "admin@admin.com", "password")
    found = _found(db, "admin@admin.com")
    assert found is not None
    engine = make_engine(db)
    with make_session_factory(engine)() as session:
        accounts.set_password(session, found[0].user_id, hash_password("changed-later"))
        session.commit()
    engine.dispose()

    assert _seed(monkeypatch, "admin@admin.com", "password") == 0  # e.g. the next `up`
    after = _found(db, "admin@admin.com")
    assert after is not None and verify_password("changed-later", after[1])


def test_nothing_is_seeded_without_both_variables(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(USERNAME_ENV, "admin@admin.com")
    assert main() == 0
    assert _found(db, "admin@admin.com") is None


def test_an_org_user_with_that_name_is_left_alone(
    db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = make_engine(db)
    with make_session_factory(engine)() as session:
        accounts.register_organization(
            session, name="Acme", username="admin@admin.com", password_hash=hash_password("x" * 8)
        )
        session.commit()
    engine.dispose()
    assert _seed(monkeypatch, "admin@admin.com", "password") == 0  # never blocks the stack
    found = _found(db, "admin@admin.com")
    assert found is not None and found[0].role == "admin"
    assert "no super admin seeded" in capsys.readouterr().err


@pytest.mark.parametrize(("username", "password"), [("a b", "password"), ("admin", "short")])
def test_invalid_seed_values_fail_loudly(
    db: str, monkeypatch: pytest.MonkeyPatch, username: str, password: str
) -> None:
    assert _seed(monkeypatch, username, password) == 2


def _counts(url: str, org_id: str) -> dict[str, int]:
    engine = make_engine(url)
    with make_session_factory(engine)() as session:
        counts = repository.count_summary(session, org_id=org_id)
    engine.dispose()
    return counts


def test_seeds_ucy_with_the_super_admin_at_home_in_it(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ORGANIZATION_ENV, "UCY")
    assert _seed(monkeypatch, "admin@admin.com", "password") == 0
    engine = make_engine(db)
    with make_session_factory(engine)() as session:
        assert accounts.get_organization(session, "ucy") == accounts.Organization("ucy", "UCY")
    engine.dispose()
    found = _found(db, "admin@admin.com")
    assert found is not None and (found[0].role, found[0].org_id) == ("superadmin", "ucy")


def test_loads_the_walkthrough_into_ucy_exactly_once(
    db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ORGANIZATION_ENV, "UCY")
    monkeypatch.setenv(DEMO_CONFIG_ENV, "config/demo.yaml")
    assert _seed(monkeypatch, "admin@admin.com", "password") == 0
    first = _counts(db, "ucy")
    assert (first["events"], first["alerts"]) == (110, 2)  # the documented walkthrough

    assert _seed(monkeypatch, "admin@admin.com", "password") == 0  # the next `up`
    assert _counts(db, "ucy") == first
    assert "already has events" in capsys.readouterr().err


def test_an_existing_super_admin_is_not_moved_into_the_seed_org(
    db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _seed(monkeypatch, "admin@admin.com", "password") == 0  # no org: homeless
    monkeypatch.setenv(ORGANIZATION_ENV, "UCY")
    assert _seed(monkeypatch, "admin@admin.com", "password") == 0
    found = _found(db, "admin@admin.com")
    assert found is not None and found[0].org_id is None
    assert "never moves an existing account" in capsys.readouterr().err


def test_a_demo_without_an_org_is_refused(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEMO_CONFIG_ENV, "config/demo.yaml")
    assert _seed(monkeypatch, "admin@admin.com", "password") == 2


def test_a_missing_demo_config_fails_loudly(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ORGANIZATION_ENV, "UCY")
    monkeypatch.setenv(DEMO_CONFIG_ENV, "config/does-not-exist.yaml")
    assert _seed(monkeypatch, "admin@admin.com", "password") == 2
