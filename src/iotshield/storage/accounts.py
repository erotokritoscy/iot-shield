"""Account persistence: organizations, users and login sessions (ADR-0039).

The SQL for access control, kept beside ``repository`` for the same reason — the rest of
the codebase never writes a query. Callers get frozen records, never ORM rows, and the
functions flush but never commit: the transaction belongs to the caller.

Passwords arrive here already hashed (``iotshield.auth.passwords``), so this module never
sees a plaintext password. Session tokens are the reverse: the caller receives the raw
token exactly once, from ``create_session``, and only its SHA-256 is stored.

Two kinds of user (ADR-0040): an **organization user** (``admin`` or ``member``) belongs to
exactly one organization and sees its data; a **super admin** manages every organization and
may have a **home organization** — whose data it sees, as that organization's admin would —
or none, in which case it sees no organization's data. ``Account.organization`` is None only
for a super admin without a home.

A super admin is managed only from the server (CLI or seed): no organization route may
remove, demote or re-key one, and its home organization cannot be suspended or deleted —
either would lock the platform out of itself.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from iotshield.storage.orm import (
    AlertDetectionRow,
    AlertRow,
    AssetRow,
    DetectionRow,
    EventRow,
    MalformedRow,
    OrganizationRow,
    SessionRow,
    UserRow,
)

OrgRole = Literal["admin", "member"]
"""The roles a user *inside* an organization can hold."""
ORG_ROLES: tuple[OrgRole, ...] = get_args(OrgRole)

Role = Literal["admin", "member", "superadmin"]
ROLES: tuple[Role, ...] = get_args(Role)

SESSION_TTL = timedelta(hours=12)

DEFAULT_ORG_ID = "default"
"""The organization a run belongs to when nobody said which.

Used where there is no logged-in user to ask: a CLI run without ``--org``, and every request
when auth is off (``IOTSHIELD_AUTH`` unset — host runs and tests). With auth off there is
exactly one tenant, so the product behaves as it did before organizations existed.
"""


class AccountConflict(ValueError):
    """A name that must be unique is already taken, or a change would break an invariant
    (an organization with no admin) — the API maps this to 409."""


@dataclass(frozen=True)
class Organization:
    org_id: str
    name: str
    suspended: bool = False


@dataclass(frozen=True)
class OrganizationSummary:
    """An organization as the super admin's list shows it: who is in it and how much it holds."""

    organization: Organization
    created_at: datetime
    users: int
    admins: int
    events: int
    alerts: int


@dataclass(frozen=True)
class User:
    user_id: UUID
    org_id: str | None  # None exactly for a super admin
    username: str
    role: Role
    created_at: datetime


@dataclass(frozen=True)
class Account:
    """Who a request is: the user and the organization whose data it may see (none for a
    super admin)."""

    user: User
    organization: Organization | None

    @property
    def org_id(self) -> str:
        """The tenant this account reads and writes. A super admin has none, so asking is a
        bug in the caller — the API's tenant dependency refuses super admins before any route
        could ask (``api/_auth.current_account``)."""
        if self.organization is None:
            raise ValueError("a super admin belongs to no organization")
        return self.organization.org_id

    @property
    def is_admin(self) -> bool:
        """May manage this organization's users: its admins, and a super admin at home here."""
        return self.user.role == "admin" or (
            self.user.role == "superadmin" and self.organization is not None
        )

    @property
    def is_superadmin(self) -> bool:
        return self.user.role == "superadmin"


def slugify(name: str) -> str:
    """An organization's id, derived from its name: lowercase ``a-z0-9`` runs joined by ``-``.

    The id is what the runner's ``--org`` names and what a file sink's subdirectory is called,
    so it must be safe in both. Refused when nothing usable remains (a name of only symbols).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:64].strip("-")
    if not slug:
        raise ValueError("organization name must contain at least one letter or digit")
    return slug


def normalize_username(username: str) -> str:
    """Usernames compare case-insensitively, so they are stored lowercased."""
    return username.strip().lower()


_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._@-]{2,63}$")


def validate_username(username: str) -> str:
    """The normalized username, or ValueError. One rule for the API and the CLI alike."""
    username = normalize_username(username)
    if not _USERNAME.fullmatch(username):
        raise ValueError(
            "username must be 3-64 characters of letters, digits and . _ @ -, "
            "starting with a letter or digit"
        )
    return username


def _user(row: UserRow) -> User:
    role = row.role
    if role not in ROLES:  # pragma: no cover - the column is only written from Role
        raise ValueError(f"unknown role {role!r} on user {row.username!r}")
    return User(
        user_id=row.user_id,
        org_id=row.org_id,
        username=row.username,
        role=role,
        created_at=row.created_at,
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _organization(row: OrganizationRow) -> Organization:
    return Organization(row.org_id, row.name, suspended=bool(row.suspended))


def get_organization(session: Session, org_id: str) -> Organization | None:
    row = session.get(OrganizationRow, org_id)
    return _organization(row) if row is not None else None


def _username_taken(session: Session, username: str) -> bool:
    return session.scalar(select(UserRow.user_id).where(UserRow.username == username)) is not None


def register_organization(
    session: Session, *, name: str, username: str, password_hash: str
) -> Account:
    """Create an organization and its first user, who is its admin."""
    org_id = slugify(name)
    username = normalize_username(username)
    if session.get(OrganizationRow, org_id) is not None:
        raise AccountConflict(f"an organization named {name.strip()!r} already exists")
    if _username_taken(session, username):
        raise AccountConflict(f"the username {username!r} is taken")
    now = datetime.now(UTC)
    session.add(OrganizationRow(org_id=org_id, name=name.strip(), created_at=now, suspended=False))
    session.flush()
    user = add_user(
        session, org_id=org_id, username=username, password_hash=password_hash, role="admin"
    )
    return Account(user=user, organization=Organization(org_id, name.strip()))


def add_user(
    session: Session, *, org_id: str, username: str, password_hash: str, role: OrgRole
) -> User:
    return _insert_user(
        session, org_id=org_id, username=username, password_hash=password_hash, role=role
    )


def create_superadmin(
    session: Session, *, username: str, password_hash: str, org_id: str | None = None
) -> User:
    """A platform-level user who manages every organization (ADR-0040), optionally with a home
    organization ``org_id`` whose data it sees.

    Reachable only from the server side (the CLI and the seeder), never from an HTTP route, so
    nobody can make themselves super admin through the website.
    """
    return _insert_user(
        session, org_id=org_id, username=username, password_hash=password_hash, role="superadmin"
    )


class ProtectedAccount(AccountConflict):
    """The change would touch a super admin, or the organization one calls home."""


def _refuse_superadmin(row: UserRow) -> None:
    if row.role == "superadmin":
        raise ProtectedAccount(
            f"{row.username!r} is a super admin; they are managed from the server, not here"
        )


def home_of_superadmin(session: Session, org_id: str) -> str | None:
    """The username of a super admin whose home is ``org_id``, if any."""
    return session.scalar(
        select(UserRow.username).where(UserRow.org_id == org_id, UserRow.role == "superadmin")
    )


def _refuse_home_org(session: Session, org_id: str, action: str) -> None:
    owner = home_of_superadmin(session, org_id)
    if owner is not None:
        raise ProtectedAccount(
            f"cannot {action} {org_id!r}: it is the home organization of super admin {owner!r}"
        )


def list_superadmins(session: Session) -> list[User]:
    rows = session.scalars(
        select(UserRow).where(UserRow.role == "superadmin").order_by(UserRow.username)
    )
    return [_user(row) for row in rows]


def _insert_user(
    session: Session, *, org_id: str | None, username: str, password_hash: str, role: Role
) -> User:
    username = normalize_username(username)
    if _username_taken(session, username):
        raise AccountConflict(f"the username {username!r} is taken")
    row = UserRow(
        user_id=uuid4(),
        org_id=org_id,
        username=username,
        password_hash=password_hash,
        role=role,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return _user(row)


def credentials_for(session: Session, username: str) -> tuple[User, str] | None:
    """The user and their stored password hash, for a login attempt."""
    row = session.scalars(
        select(UserRow).where(UserRow.username == normalize_username(username))
    ).one_or_none()
    return (_user(row), row.password_hash) if row is not None else None


def password_hash_of(session: Session, user_id: UUID) -> str | None:
    row = session.get(UserRow, user_id)
    return row.password_hash if row is not None else None


def list_users(session: Session, *, org_id: str) -> list[User]:
    rows = session.scalars(
        select(UserRow).where(UserRow.org_id == org_id).order_by(UserRow.username)
    )
    return [_user(row) for row in rows]


def count_admins(session: Session, *, org_id: str) -> int:
    stmt = select(func.count()).where(UserRow.org_id == org_id, UserRow.role == "admin")
    return session.scalar(stmt) or 0


def get_user(session: Session, user_id: UUID, *, org_id: str) -> User | None:
    """A user of this organization; another organization's user is simply not found."""
    row = session.get(UserRow, user_id)
    return _user(row) if row is not None and row.org_id == org_id else None


def delete_user(session: Session, user_id: UUID, *, org_id: str) -> bool:
    """Remove a user of this organization (their sessions go with them). False if absent.

    A super admin at home here is refused (``ProtectedAccount``)."""
    row = session.get(UserRow, user_id)
    if row is None or row.org_id != org_id:
        return False
    _refuse_superadmin(row)
    # Explicit rather than relying on ON DELETE CASCADE, which SQLite only honours with
    # foreign keys enabled — the test engine has them on, but a revoked user's sessions
    # surviving must not depend on a connection pragma.
    session.execute(delete(SessionRow).where(SessionRow.user_id == user_id))
    session.execute(delete(UserRow).where(UserRow.user_id == user_id))
    session.flush()
    return True


def set_role(session: Session, user_id: UUID, role: OrgRole, *, org_id: str) -> User | None:
    """Change an organization user's role. None if they are not in this organization.

    Refuses to demote the organization's last admin: an organization nobody can manage is
    only recoverable by the super admin, and no route should be able to create one.
    """
    row = session.get(UserRow, user_id)
    if row is None or row.org_id != org_id:
        return None
    _refuse_superadmin(row)
    if row.role == "admin" and role != "admin" and count_admins(session, org_id=org_id) <= 1:
        raise AccountConflict(f"{row.username!r} is the organization's only admin")
    row.role = role
    session.flush()
    return _user(row)


def set_password(session: Session, user_id: UUID, password_hash: str) -> None:
    row = session.get(UserRow, user_id)
    if row is None:
        raise ValueError("no such user")
    row.password_hash = password_hash
    session.flush()


def create_session(session: Session, user_id: UUID, *, ttl: timedelta = SESSION_TTL) -> str:
    """Start a login session and return its raw token — the only time it exists in clear."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session.add(
        SessionRow(
            token_hash=_token_hash(token), user_id=user_id, created_at=now, expires_at=now + ttl
        )
    )
    session.flush()
    return token


def resolve_session(session: Session, token: str, *, now: datetime | None = None) -> Account | None:
    """The account a session token belongs to, or None if unknown or expired.

    Read-only on purpose: the read API resolves sessions too, and ADR-0007 keeps it
    write-free. An expired row is therefore ignored here and purged by the next login.
    """
    now = now or datetime.now(UTC)
    row = session.execute(
        select(UserRow, OrganizationRow)
        .join(SessionRow, SessionRow.user_id == UserRow.user_id)
        # Outer: a super admin has no organization and must still resolve.
        .outerjoin(OrganizationRow, OrganizationRow.org_id == UserRow.org_id)
        .where(
            SessionRow.token_hash == _token_hash(token),
            SessionRow.expires_at > now,
            # A suspended organization's sessions are dead even if a row somehow survived
            # `set_suspended`'s purge — suspension is checked where it is enforced.
            or_(OrganizationRow.org_id.is_(None), OrganizationRow.suspended.is_(False)),
        )
    ).one_or_none()
    if row is None:
        return None
    user_row, org_row = row
    organization = _organization(org_row) if org_row is not None else None
    return Account(user=_user(user_row), organization=organization)


def end_session(session: Session, token: str) -> None:
    session.execute(delete(SessionRow).where(SessionRow.token_hash == _token_hash(token)))
    session.flush()


def end_other_sessions(session: Session, user_id: UUID, *, keep_token: str | None) -> None:
    """Log a user out everywhere except (optionally) the session making the request."""
    stmt = delete(SessionRow).where(SessionRow.user_id == user_id)
    if keep_token is not None:
        stmt = stmt.where(SessionRow.token_hash != _token_hash(keep_token))
    session.execute(stmt)
    session.flush()


def purge_expired_sessions(session: Session, *, now: datetime | None = None) -> None:
    session.execute(delete(SessionRow).where(SessionRow.expires_at <= (now or datetime.now(UTC))))
    session.flush()


# --- Super admin: every organization (ADR-0040) ---------------------------------------


def list_organizations(session: Session, *, org_id: str | None = None) -> list[OrganizationSummary]:
    """Every organization (or just ``org_id``) with its head-counts, in one round-trip,
    newest first."""
    in_org = OrganizationRow.org_id
    users = select(func.count()).where(UserRow.org_id == in_org)
    admins = users.where(UserRow.role == "admin")
    events = select(func.count()).where(EventRow.org_id == in_org)
    alerts = select(func.count()).where(AlertRow.org_id == in_org)
    stmt = select(
        OrganizationRow,
        *(
            count.correlate(OrganizationRow).scalar_subquery()
            for count in (users, admins, events, alerts)
        ),
    ).order_by(OrganizationRow.created_at.desc(), OrganizationRow.org_id)
    if org_id is not None:
        stmt = stmt.where(OrganizationRow.org_id == org_id)
    return [
        OrganizationSummary(
            organization=_organization(org),
            created_at=org.created_at,
            users=int(n_users),
            admins=int(n_admins),
            events=int(n_events),
            alerts=int(n_alerts),
        )
        for org, n_users, n_admins, n_events, n_alerts in session.execute(stmt).tuples()
    ]


def organization_summary(session: Session, org_id: str) -> OrganizationSummary | None:
    found = list_organizations(session, org_id=org_id)
    return found[0] if found else None


def set_suspended(session: Session, org_id: str, *, suspended: bool) -> Organization | None:
    """Suspend (or reactivate) an organization. Suspending signs every one of its users out.

    Nothing is deleted: suspension is the reversible half of "this organization should stop".
    """
    row = session.get(OrganizationRow, org_id)
    if row is None:
        return None
    if suspended:
        _refuse_home_org(session, org_id, "suspend")
    row.suspended = suspended
    if suspended:
        members = select(UserRow.user_id).where(UserRow.org_id == org_id)
        session.execute(delete(SessionRow).where(SessionRow.user_id.in_(members)))
    session.flush()
    return _organization(row)


def delete_organization(session: Session, org_id: str) -> bool:
    """Delete an organization, its users and **all of its data**. Irreversible.

    Children before parents, explicitly, in the order the foreign keys require: the alert ->
    detection links, alerts, detections, events and malformed records, then the assets they
    reference, then sessions, users and the organization itself. Not left to ``ON DELETE
    CASCADE``, which SQLite only honours with a per-connection pragma — "deleted everything"
    must not depend on how a connection was opened.
    """
    if session.get(OrganizationRow, org_id) is None:
        return False
    _refuse_home_org(session, org_id, "delete")
    org_alerts = select(AlertRow.alert_id).where(AlertRow.org_id == org_id)
    session.execute(delete(AlertDetectionRow).where(AlertDetectionRow.alert_id.in_(org_alerts)))
    for row_type in (AlertRow, DetectionRow, EventRow, MalformedRow, AssetRow):
        session.execute(delete(row_type).where(row_type.org_id == org_id))
    members = select(UserRow.user_id).where(UserRow.org_id == org_id)
    session.execute(delete(SessionRow).where(SessionRow.user_id.in_(members)))
    session.execute(delete(UserRow).where(UserRow.org_id == org_id))
    session.execute(delete(OrganizationRow).where(OrganizationRow.org_id == org_id))
    session.flush()
    return True
