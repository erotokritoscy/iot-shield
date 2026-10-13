"""The login gate both APIs share: who is asking, and which organization they may see.

Installed as an **app-level dependency**, so it runs before every route either app has or
will ever have — a route added next month is protected without anyone remembering to
protect it. The opposite default (each route opting in) is how an unauthenticated endpoint
ships. The few routes that must be reachable logged-out are named in ``PUBLIC_ROUTES``.

One flag, ``IOTSHIELD_AUTH``, off unless a deployment opts in (compose sets it). Off is the
single-tenant product as it was before organizations existed: every request acts as an admin
of ``DEFAULT_ORG_ID``, so tests, host runs and ``npm run dev`` against a local API need no
account. On, a request without a valid session cookie is a 401 — on the *API*, not only in
the dashboard, because a login page in front of an open API protects nothing (ADR-0039).

The session token rides in an ``HttpOnly`` cookie (page scripts cannot read it) with
``SameSite=Strict`` (another site cannot make the browser send it), which is also the CSRF
stance: no cross-site request carries the session at all.
"""

import os
from datetime import UTC, datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session, sessionmaker

from iotshield.storage import DEFAULT_ORG_ID, accounts
from iotshield.storage.accounts import Account, Organization, User

AUTH_ENV: Final = "IOTSHIELD_AUTH"
OPEN_REGISTRATION_ENV: Final = "IOTSHIELD_OPEN_REGISTRATION"
SESSION_COOKIE: Final = "iotshield_session"

PUBLIC_ROUTES: Final = frozenset(
    {"/health", "/auth/login", "/auth/register", "/auth/logout", "/auth/options"}
)
"""Route templates reachable without a session. ``/health`` because compose's healthcheck has
no account; login and register because they are how one gets a session; logout because it
must be able to clear a cookie whose session has already expired; options because the login
page must know whether to offer registration before anyone is logged in."""

LOCAL_ACCOUNT: Final = Account(
    user=User(
        user_id=UUID(int=0),
        org_id=DEFAULT_ORG_ID,
        username="local",
        role="admin",
        created_at=datetime(1970, 1, 1, tzinfo=UTC),
    ),
    organization=Organization(DEFAULT_ORG_ID, "Default"),
)
"""Who every request is when auth is off: an admin of the one default tenant."""


def auth_enabled() -> bool:
    """Whether this deployment requires login. Off unless opted in."""
    return os.environ.get(AUTH_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def open_registration() -> bool:
    """Whether anyone may register an organization (ADR-0040). **On** unless switched off.

    Off, only the super admin creates organizations. Read per request, not at startup, so it
    is the environment's current value that the register route and ``/auth/options`` agree on.
    """
    return os.environ.get(OPEN_REGISTRATION_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _route_path(request: Request) -> str:
    # The matched route's TEMPLATE, not the URL: robust to a proxy prefix and to path
    # parameters, and it cannot be spoofed with `//health` or `/health/../alerts`.
    route = request.scope.get("route")
    return route.path if isinstance(route, APIRoute) else request.url.path


def authenticate(request: Request) -> None:
    """App-level dependency: attach the caller's account to the request, or refuse with 401."""
    if _route_path(request) in PUBLIC_ROUTES:
        return
    if not request.app.state.auth_enabled:
        request.state.account = LOCAL_ACCOUNT
        return
    token = request.cookies.get(SESSION_COOKIE)
    account = None
    if token:
        factory: sessionmaker[Session] = request.app.state.session_factory
        with factory() as session:
            account = accounts.resolve_session(session, token)
    if account is None:
        raise HTTPException(status_code=401, detail="not logged in")
    request.state.account = account


def current_principal(request: Request) -> Account:
    """Whoever is logged in — an organization user *or* the super admin.

    For the few routes about the caller themselves (``/auth/me``, ``/auth/password``). Any
    route that touches an organization's data takes ``AccountDep`` instead.
    """
    account = getattr(request.state, "account", None)
    if not isinstance(account, Account):
        raise HTTPException(status_code=401, detail="not logged in")
    return account


PrincipalDep = Annotated[Account, Depends(current_principal)]


def current_account(principal: PrincipalDep) -> Account:
    """The caller as a member of an organization — what every data route depends on.

    A super admin **without a home organization** is refused here, once, rather than in each
    route: they manage organizations and see none of their data (ADR-0040). One *with* a home
    passes, scoped to that home only — so no super admin ever reads another organization's
    data, on any route, including ones added later, because a data route cannot be written
    without naming the tenant this dependency supplies.
    """
    if principal.organization is None:
        raise HTTPException(
            status_code=403,
            detail="the super admin manages organizations and cannot see their data",
        )
    return principal


AccountDep = Annotated[Account, Depends(current_account)]


def require_superadmin(principal: PrincipalDep) -> Account:
    if not principal.is_superadmin:
        raise HTTPException(status_code=403, detail="only the super admin can do this")
    return principal


SuperAdminDep = Annotated[Account, Depends(require_superadmin)]


def require_admin(account: AccountDep) -> Account:
    if not account.is_admin:
        raise HTTPException(status_code=403, detail="only an organization admin can do this")
    return account


AdminDep = Annotated[Account, Depends(require_admin)]


def _is_https(request: Request) -> bool:
    # Behind nginx the app sees plain HTTP; the proxy forwards the browser's scheme.
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(accounts.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="strict",
        # Secure whenever the browser is on HTTPS. Not unconditionally: the compose stack is
        # served over plain http://localhost, where some browsers drop a Secure cookie.
        secure=_is_https(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
