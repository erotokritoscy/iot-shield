"""The account routes: register an organization, log in and out, manage its users (ADR-0039).

Mounted on the **Pipeline API** only, and only when ``IOTSHIELD_AUTH`` is on. The Pipeline
API because registration and user management *write*, and ADR-0007 keeps the read API
write-free (it still resolves sessions — a read). Absent rather than guarded when auth is
off, the same stance as the eval router (ADR-0021 D1): the dashboard probes ``/auth/me`` and
a 404 tells it this deployment has no login, so the one flag decides login end to end.

Inside an organization the roles are two: an **admin** manages the organization's users, a
**member** does everything else. An admin cannot remove themselves, which is also what
guarantees every organization keeps at least one admin — no route here changes a role. Above
them sits the **super admin** (ADR-0040), who belongs to no organization: ``/auth/me`` and
``/auth/password`` serve them, and everything else they do is under ``/admin``.

Registration can be closed (``IOTSHIELD_OPEN_REGISTRATION=0``); ``/auth/options`` tells the
login page whether to offer it.
"""

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session, sessionmaker

from iotshield.api._auth import (
    SESSION_COOKIE,
    AdminDep,
    PrincipalDep,
    clear_session_cookie,
    open_registration,
    set_session_cookie,
)
from iotshield.auth.passwords import DUMMY_HASH, hash_password, verify_password
from iotshield.storage import accounts
from iotshield.storage.accounts import (
    Account,
    AccountConflict,
    OrgRole,
    ProtectedAccount,
    Role,
    User,
)

MIN_PASSWORD = 8
MAX_PASSWORD = 128


# --- Wire models ----------------------------------------------------------------------


def _check_username(value: str) -> str:
    return accounts.validate_username(value)


Password = Annotated[str, Field(min_length=MIN_PASSWORD, max_length=MAX_PASSWORD)]


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization: str = Field(min_length=2, max_length=100)
    username: str
    password: Password

    _username = field_validator("username")(_check_username)

    @field_validator("organization")
    @classmethod
    def _organization(cls, value: str) -> str:
        accounts.slugify(value)  # refuses a name that yields no usable id
        return value.strip()


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=64)
    password: str = Field(max_length=MAX_PASSWORD)


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(max_length=MAX_PASSWORD)
    new_password: Password


class NewUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: Password = Field(description="A temporary password the admin shares with the user")
    role: OrgRole = "member"

    _username = field_validator("username")(_check_username)


class OrganizationInfo(BaseModel):
    org_id: str
    name: str


class UserInfo(BaseModel):
    user_id: UUID
    username: str
    role: Role
    created_at: str


class MeResponse(BaseModel):
    """Who is logged in, and which organization's data they see (none for the super admin)."""

    user_id: UUID
    username: str
    role: Role
    organization: OrganizationInfo | None


class AuthOptions(BaseModel):
    """What the login page needs to know before anyone is logged in."""

    open_registration: bool


def user_info(user: User) -> UserInfo:
    return UserInfo(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        created_at=user.created_at.isoformat(),
    )


def _me(account: Account) -> MeResponse:
    organization = account.organization
    return MeResponse(
        user_id=account.user.user_id,
        username=account.user.username,
        role=account.user.role,
        organization=(
            OrganizationInfo(org_id=organization.org_id, name=organization.name)
            if organization is not None
            else None
        ),
    )


# --- Router ---------------------------------------------------------------------------


def _session(request: Request) -> Iterator[Session]:
    # Each writing route commits explicitly as its last step, so the commit cannot depend on
    # when FastAPI runs a dependency's teardown, and a route that raises commits nothing.
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(_session)]


def create_auth_router() -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.get("/options")
    def options() -> AuthOptions:
        """Public: whether the login page should offer registration."""
        return AuthOptions(open_registration=open_registration())

    @router.post("/register", status_code=201)
    def register(
        body: RegisterRequest, request: Request, response: Response, session: SessionDep
    ) -> MeResponse:
        """Create an organization and its first admin, and log that admin in."""
        if not open_registration():
            raise HTTPException(
                status_code=403,
                detail="registration is closed; ask the platform administrator for an account",
            )
        try:
            account = accounts.register_organization(
                session,
                name=body.organization,
                username=body.username,
                password_hash=hash_password(body.password),
            )
        except AccountConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        token = accounts.create_session(session, account.user.user_id)
        session.commit()
        set_session_cookie(request, response, token)
        return _me(account)

    @router.post("/login")
    def login(
        body: LoginRequest, request: Request, response: Response, session: SessionDep
    ) -> MeResponse:
        found = accounts.credentials_for(session, body.username)
        # Verify against a dummy hash when the user does not exist, so a wrong username and a
        # wrong password cost the same time and say the same thing.
        stored = found[1] if found is not None else DUMMY_HASH
        if not verify_password(body.password, stored) or found is None:
            raise HTTPException(status_code=401, detail="invalid username or password")
        user = found[0]
        organization = None
        if user.org_id is not None:  # None is the super admin, who belongs to no organization
            organization = accounts.get_organization(session, user.org_id)
            if organization is None:  # pragma: no cover - the FK makes this unreachable
                raise HTTPException(status_code=401, detail="invalid username or password")
            # Said only after a correct password, so it reveals nothing to a guesser.
            if organization.suspended:
                raise HTTPException(
                    status_code=403,
                    detail="this organization is suspended; contact the platform administrator",
                )
        accounts.purge_expired_sessions(session)
        token = accounts.create_session(session, user.user_id)
        session.commit()
        set_session_cookie(request, response, token)
        return _me(Account(user=user, organization=organization))

    @router.post("/logout", status_code=204)
    def logout(request: Request, response: Response, session: SessionDep) -> None:
        """End this session. Public, so it can clear a cookie whose session already expired."""
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            accounts.end_session(session, token)
            session.commit()
        clear_session_cookie(response)

    @router.get("/me")
    def me(account: PrincipalDep) -> MeResponse:
        return _me(account)

    @router.post("/password", status_code=204)
    def change_password(
        body: PasswordChangeRequest, request: Request, account: PrincipalDep, session: SessionDep
    ) -> None:
        """Replace your own password; every other session of yours is logged out."""
        stored = accounts.password_hash_of(session, account.user.user_id)
        if stored is None or not verify_password(body.current_password, stored):
            raise HTTPException(status_code=403, detail="current password is incorrect")
        accounts.set_password(session, account.user.user_id, hash_password(body.new_password))
        accounts.end_other_sessions(
            session, account.user.user_id, keep_token=request.cookies.get(SESSION_COOKIE)
        )
        session.commit()

    @router.get("/users")
    def list_users(account: AdminDep, session: SessionDep) -> list[UserInfo]:
        return [user_info(u) for u in accounts.list_users(session, org_id=account.org_id)]

    @router.post("/users", status_code=201)
    def add_user(body: NewUserRequest, account: AdminDep, session: SessionDep) -> UserInfo:
        """Add a user to your organization with a temporary password you share with them."""
        try:
            user = accounts.add_user(
                session,
                org_id=account.org_id,
                username=body.username,
                password_hash=hash_password(body.password),
                role=body.role,
            )
        except AccountConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        return user_info(user)

    @router.delete("/users/{user_id}", status_code=204)
    def remove_user(user_id: UUID, account: AdminDep, session: SessionDep) -> None:
        if user_id == account.user.user_id:
            raise HTTPException(status_code=400, detail="you cannot remove your own account")
        # Another organization's user is a 404, exactly like an id that never existed.
        try:
            removed = accounts.delete_user(session, user_id, org_id=account.org_id)
        except ProtectedAccount as exc:  # a super admin at home in this organization
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not removed:
            raise HTTPException(status_code=404, detail="user not found")
        session.commit()

    return router
