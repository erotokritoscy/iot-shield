"""Seed the platform: a home organization, its super admin, and its demo data (ADR-0040).

    IOTSHIELD_SEED_ORGANIZATION=UCY \\
    IOTSHIELD_SEED_SUPERADMIN_USERNAME=admin@admin.com \\
    IOTSHIELD_SEED_SUPERADMIN_PASSWORD=password \\
    IOTSHIELD_SEED_DEMO_CONFIG=config/demo.yaml \\
    python -m iotshield.auth.seed

Run by compose's one-shot ``init`` step after the schema exists, so a fresh stack comes up
ready to sign in to. Three steps, each **create-only** and skipped when its variable is unset
(which is the default everywhere outside compose):

1. ``IOTSHIELD_SEED_ORGANIZATION`` — the organization, by name (its id is the slug: ``ucy``).
   An existing one is left exactly as it is.
2. ``IOTSHIELD_SEED_SUPERADMIN_*`` — the super admin, at home in that organization: it manages
   every organization and sees that one's data. An existing username is left exactly as it is,
   so a password changed after the first start is never reset by a restart.
3. ``IOTSHIELD_SEED_DEMO_CONFIG`` — a pipeline config run into that organization, **only while
   the organization has no events**, so restarting never duplicates it.

This is the CLI's ``create`` driven by the environment, so the two cannot disagree about what a
super admin is. The credentials compose supplies by default are well known; the stack binds to
loopback only, and the seeder says so on every start until the password is changed.
"""

import os
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from iotshield.auth.passwords import hash_password, verify_password
from iotshield.storage import (
    accounts,
    database_url_from_env,
    make_engine,
    make_session_factory,
    repository,
)
from iotshield.storage.accounts import AccountConflict

ORGANIZATION_ENV = "IOTSHIELD_SEED_ORGANIZATION"
USERNAME_ENV = "IOTSHIELD_SEED_SUPERADMIN_USERNAME"
PASSWORD_ENV = "IOTSHIELD_SEED_SUPERADMIN_PASSWORD"
DEMO_CONFIG_ENV = "IOTSHIELD_SEED_DEMO_CONFIG"
MIN_PASSWORD = 8  # the same floor the API and the CLI enforce

_WEAK = {"password", "admin", "changeme", "12345678"}


def _say(message: str) -> None:
    print(message, file=sys.stderr)


def _seed_organization(session: Session, name: str) -> str:
    org_id = accounts.slugify(name)
    existed = accounts.get_organization(session, org_id) is not None
    repository.ensure_organization(session, org_id, name.strip())
    _say(f"organization {org_id!r} {'already exists; left unchanged' if existed else 'seeded'}")
    return org_id


def _seed_superadmin(session: Session, username: str, password: str, org_id: str | None) -> None:
    found = accounts.credentials_for(session, username)
    if found is None:
        accounts.create_superadmin(
            session, username=username, password_hash=hash_password(password), org_id=org_id
        )
        home = f" at home in {org_id!r}" if org_id else ""
        _say(f"super admin {username!r} seeded{home}")
        if password.lower() in _WEAK:
            _say("warning: that is a well-known default password; change it on the Account page")
        return
    user, stored = found
    if user.role != "superadmin":
        # Not fatal: refusing would stop the whole stack from starting over a name clash an
        # operator can fix from the dashboard.
        _say(
            f"warning: {username!r} exists as a user of organization {user.org_id!r}; "
            "no super admin seeded"
        )
        return
    _say(f"super admin {username!r} already exists; left unchanged")
    if user.org_id != org_id:
        _say(
            f"note: its home organization is {user.org_id!r}, not {org_id!r}; "
            "a seed never moves an existing account"
        )
    if verify_password(password, stored) and password.lower() in _WEAK:
        _say(
            f"warning: super admin {username!r} still has the seeded default password; "
            "change it on the Account page"
        )


def _seed_demo(session: Session, org_id: str, config_path: str) -> None:
    if repository.count_summary(session, org_id=org_id)["events"] > 0:
        _say(f"demo data not loaded: {org_id!r} already has events")
        return
    # Imported here: only this step needs the pipeline, and a seed with no demo should not
    # pay for (or depend on) the detector stack.
    from iotshield.pipeline.config import load_config
    from iotshield.pipeline.runner import run_pipeline

    report = run_pipeline(
        load_config(Path(config_path)), org_id=org_id, name="Demo walkthrough (seed)"
    )
    _say(
        f"demo data loaded into {org_id!r} from {config_path}: {report.events} events, "
        f"{report.detections} detections, {report.alerts} alerts"
    )


def main() -> int:
    org_name = os.environ.get(ORGANIZATION_ENV, "").strip()
    username = os.environ.get(USERNAME_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "")
    demo_config = os.environ.get(DEMO_CONFIG_ENV, "").strip()
    seed_admin = bool(username and password)
    if not (org_name or seed_admin):
        _say("nothing seeded (seed variables not set)")
        return 0
    if demo_config and not org_name:
        _say(f"error: {DEMO_CONFIG_ENV} needs {ORGANIZATION_ENV} to know whose data it is")
        return 2
    url = database_url_from_env()
    if url is None:
        return 2
    try:
        if org_name:
            accounts.slugify(org_name)  # refuse a name with no usable id before writing
        if seed_admin:
            username = accounts.validate_username(username)
            if len(password) < MIN_PASSWORD:
                raise ValueError(f"{PASSWORD_ENV} must be at least {MIN_PASSWORD} characters")
    except ValueError as exc:
        _say(f"error: {exc}")
        return 2

    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as session:
            org_id = _seed_organization(session, org_name) if org_name else None
            if seed_admin:
                _seed_superadmin(session, username, password, org_id)
            session.commit()
            if org_id is not None and demo_config:
                _seed_demo(session, org_id, demo_config)
    except AccountConflict as exc:  # pragma: no cover - a concurrent seed won the race
        _say(f"not seeded: {exc}")
        return 0
    except (OSError, ValueError) as exc:  # a missing or invalid demo config
        _say(f"error: demo data not loaded: {exc}")
        return 2
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
