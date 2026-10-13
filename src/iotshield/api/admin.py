"""The super admin's routes: every organization, and the users inside any of them (ADR-0040).

Mounted on the Pipeline API beside ``/auth`` (it writes; the read API stays write-free), and
only when ``IOTSHIELD_AUTH`` is on. Every route depends on ``SuperAdminDep``, so an
organization admin is a 403 here however the request is shaped.

**Management only.** Nothing under ``/admin`` returns an organization's events, detections or
alerts — the summary carries *counts* so the super admin can see what a delete would destroy,
never the rows. The data routes themselves refuse a super admin (``_auth.current_account``).

Deleting an organization is irreversible and takes its data with it, so the request must
repeat the organization's id in ``confirm``. The server checks it, not just the page: a
script or a stray ``curl`` gets the same guard as the button.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from iotshield.api._auth import SuperAdminDep
from iotshield.api.auth import (
    NewUserRequest,
    Password,
    RegisterRequest,
    SessionDep,
    UserInfo,
    user_info,
)
from iotshield.auth.passwords import hash_password
from iotshield.storage import accounts
from iotshield.storage.accounts import (
    AccountConflict,
    OrganizationSummary,
    OrgRole,
    ProtectedAccount,
)


class OrganizationAdminInfo(BaseModel):
    """One row of the super admin's organization list."""

    org_id: str
    name: str
    suspended: bool
    created_at: datetime
    users: int
    admins: int
    events: int
    alerts: int


class RoleChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: OrgRole


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: Password


def _info(summary: OrganizationSummary) -> OrganizationAdminInfo:
    org = summary.organization
    return OrganizationAdminInfo(
        org_id=org.org_id,
        name=org.name,
        suspended=org.suspended,
        created_at=summary.created_at,
        users=summary.users,
        admins=summary.admins,
        events=summary.events,
        alerts=summary.alerts,
    )


_NOT_FOUND = HTTPException(status_code=404, detail="organization not found")


def create_admin_router() -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    def _summary(session: SessionDep, org_id: str) -> OrganizationAdminInfo:
        summary = accounts.organization_summary(session, org_id)
        if summary is None:
            raise _NOT_FOUND
        return _info(summary)

    @router.get("/organizations")
    def list_organizations(_: SuperAdminDep, session: SessionDep) -> list[OrganizationAdminInfo]:
        return [_info(s) for s in accounts.list_organizations(session)]

    @router.post("/organizations", status_code=201)
    def create_organization(
        body: RegisterRequest, _: SuperAdminDep, session: SessionDep
    ) -> OrganizationAdminInfo:
        """Create an organization and its first admin. Works when public registration is off."""
        try:
            account = accounts.register_organization(
                session,
                name=body.organization,
                username=body.username,
                password_hash=hash_password(body.password),
            )
        except AccountConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        return _summary(session, account.org_id)

    @router.get("/organizations/{org_id}")
    def get_organization(
        org_id: str, _: SuperAdminDep, session: SessionDep
    ) -> OrganizationAdminInfo:
        return _summary(session, org_id)

    @router.post("/organizations/{org_id}/suspend")
    def suspend(org_id: str, _: SuperAdminDep, session: SessionDep) -> OrganizationAdminInfo:
        """Block the organization's logins and sign its users out. Nothing is deleted.

        A super admin's home organization cannot be suspended (409): it would lock them out."""
        try:
            suspended = accounts.set_suspended(session, org_id, suspended=True)
        except ProtectedAccount as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if suspended is None:
            raise _NOT_FOUND
        session.commit()
        return _summary(session, org_id)

    @router.post("/organizations/{org_id}/reactivate")
    def reactivate(org_id: str, _: SuperAdminDep, session: SessionDep) -> OrganizationAdminInfo:
        if accounts.set_suspended(session, org_id, suspended=False) is None:
            raise _NOT_FOUND
        session.commit()
        return _summary(session, org_id)

    @router.delete("/organizations/{org_id}", status_code=204)
    def delete_organization(
        org_id: str, confirm: str, _: SuperAdminDep, session: SessionDep
    ) -> None:
        """Delete the organization, its users and all of its data. Irreversible.

        ``confirm`` must repeat ``org_id`` exactly.
        """
        if confirm != org_id:
            raise HTTPException(
                status_code=400,
                detail=f"to delete this organization, confirm by repeating its id {org_id!r}",
            )
        try:
            deleted = accounts.delete_organization(session, org_id)
        except ProtectedAccount as exc:  # a super admin's home: deleting it deletes them
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise _NOT_FOUND
        session.commit()

    # --- Users of any organization ---------------------------------------------------

    def _require_org(session: SessionDep, org_id: str) -> None:
        if accounts.get_organization(session, org_id) is None:
            raise _NOT_FOUND

    @router.get("/organizations/{org_id}/users")
    def list_users(org_id: str, _: SuperAdminDep, session: SessionDep) -> list[UserInfo]:
        _require_org(session, org_id)
        return [user_info(u) for u in accounts.list_users(session, org_id=org_id)]

    @router.post("/organizations/{org_id}/users", status_code=201)
    def add_user(
        org_id: str, body: NewUserRequest, _: SuperAdminDep, session: SessionDep
    ) -> UserInfo:
        _require_org(session, org_id)
        try:
            user = accounts.add_user(
                session,
                org_id=org_id,
                username=body.username,
                password_hash=hash_password(body.password),
                role=body.role,
            )
        except AccountConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        return user_info(user)

    @router.patch("/organizations/{org_id}/users/{user_id}")
    def change_role(
        org_id: str, user_id: UUID, body: RoleChangeRequest, _: SuperAdminDep, session: SessionDep
    ) -> UserInfo:
        """Make a user admin or member. Demoting the organization's last admin is a 409."""
        try:
            user = accounts.set_role(session, user_id, body.role, org_id=org_id)
        except AccountConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        session.commit()
        return user_info(user)

    @router.post("/organizations/{org_id}/users/{user_id}/password", status_code=204)
    def reset_password(
        org_id: str,
        user_id: UUID,
        body: PasswordResetRequest,
        _: SuperAdminDep,
        session: SessionDep,
    ) -> None:
        """Set a new temporary password and sign the user out everywhere."""
        user = accounts.get_user(session, user_id, org_id=org_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if user.role == "superadmin":
            raise HTTPException(
                status_code=409,
                detail=f"{user.username!r} is a super admin; they change their own password",
            )
        accounts.set_password(session, user_id, hash_password(body.password))
        accounts.end_other_sessions(session, user_id, keep_token=None)
        session.commit()

    @router.delete("/organizations/{org_id}/users/{user_id}", status_code=204)
    def remove_user(org_id: str, user_id: UUID, _: SuperAdminDep, session: SessionDep) -> None:
        """Remove a user. Removing the organization's last admin is a 409 — delete the
        organization instead, or promote someone first."""
        user = accounts.get_user(session, user_id, org_id=org_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if user.role == "admin" and accounts.count_admins(session, org_id=org_id) <= 1:
            raise HTTPException(
                status_code=409, detail=f"{user.username!r} is the organization's only admin"
            )
        try:
            accounts.delete_user(session, user_id, org_id=org_id)
        except ProtectedAccount as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()

    return router
