"""OpenID Connect login (Authlib) and the signed session cookie.

``GET /auth/login?next=/path`` redirects to the issuer's authorization endpoint (authorization
code flow with PKCE ``S256``, scopes ``openid email profile``); ``GET /auth/callback`` exchanges
the code, validates the ID token (signature against the issuer's JWKS, ``iss``, ``aud``, ``exp``,
``nonce``), upserts ``users`` by ``subject`` (the ``sub`` claim), and starts a session;
``POST /auth/logout`` ends it; ``GET /auth/me`` returns ``{id, display_name, roles,
permissions}``. Discovery is ``<LAE_OIDC_ISSUER>/.well-known/openid-configuration``.

The session is Starlette's ``SessionMiddleware`` cookie ``lae_session``: signed with
``LAE_SESSION_SECRET`` (itsdangerous, not encrypted), ``HttpOnly``, ``SameSite=Lax``, ``Secure``
unless ``LAE_ENV=development``, and re-issued on every response, so it lapses after
``SESSION_MAX_AGE_SECONDS`` without a request. It holds the user id, the time it was issued
(``iat``, for revoking sessions later), and, during a login, Authlib's state, nonce, and PKCE
verifier. A first-time user gets the ``Viewer`` role (``DEFAULT_ROLE``). A deactivated user
(``users.is_active = false``) cannot log in, and an existing session of theirs stops working.

Authlib uses ``httpx2`` when installed and otherwise ``httpx`` (decision F2), announcing the
fallback with a deprecation warning that is silenced at import below. ``create_oidc_client``
passes the application's test transport to Authlib's client, so tests run the whole flow against
an in-process fake provider.
"""

from __future__ import annotations

import time
import warnings
from typing import Annotated, Any, Final
from uuid import UUID

import authlib.deprecate
import httpx
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from joserfc.errors import JoseError
from pydantic import BaseModel
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Uuid

from lae.api.errors import ApiError
from lae.auth.errors import ACCOUNT_INACTIVE, IDP_UNAVAILABLE, LOGIN_FAILED, LOGIN_NOT_CONFIGURED
from lae.auth.permissions import (
    DEFAULT_ROLE,
    CurrentUser,
    current_user,
    user_principal,
)
from lae.db.models import Role, User, UserRole
from lae.db.session import get_session
from lae.logging import get_logger
from lae.settings import Settings

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="The httpx module is deprecated",
        category=authlib.deprecate.AuthlibDeprecationWarning,
    )
    from authlib.integrations.starlette_client import (
        OAuth,
        OAuthError,
        StarletteOAuth2App,
    )

SESSION_COOKIE: Final = "lae_session"
"""Name of the session cookie."""

SESSION_MAX_AGE_SECONDS: Final = 12 * 60 * 60
"""Idle lifetime of a session: the cookie's signature expires this long after the last request."""

SESSION_USER_KEY: Final = "user_id"
"""Session key holding the signed-in user's id."""

SESSION_ISSUED_KEY: Final = "iat"
"""Session key holding the Unix time the session started."""

NEXT_KEY: Final = "lae_next"
"""Session key holding where to go after the login callback."""

OIDC_CLIENT_NAME: Final = "oidc"
OIDC_SCOPE: Final = "openid email profile"
IDP_TIMEOUT_SECONDS: Final = 10.0
"""Timeout of each request to the identity provider."""

ID_TOKEN_ERROR: Final = "invalid_id_token"
"""``error`` parameter of ``LAE-AUTH-007`` when the ID token is missing or does not validate."""

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class Me(BaseModel):
    """Body of ``GET /auth/me``."""

    id: UUID
    display_name: str
    roles: list[str]
    permissions: list[str]


def discovery_url(issuer: str) -> str:
    """Return the OpenID Connect discovery document URL of an issuer."""
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def create_oidc_client(
    settings: Settings, http_transport: httpx.AsyncBaseTransport | None = None
) -> StarletteOAuth2App | None:
    """Return the Authlib client for ``LAE_OIDC_ISSUER``, or ``None`` when login is not set up."""
    if not settings.oidc_issuer:
        return None
    client_kwargs: dict[str, Any] = {
        "scope": OIDC_SCOPE,
        "code_challenge_method": "S256",
        "timeout": IDP_TIMEOUT_SECONDS,
    }
    if http_transport is not None:
        client_kwargs["transport"] = http_transport
    secret = settings.oidc_client_secret
    oauth = OAuth()
    client: StarletteOAuth2App = oauth.register(
        OIDC_CLIENT_NAME,
        client_id=settings.oidc_client_id,
        client_secret=secret.get_secret_value() if secret is not None else None,
        server_metadata_url=discovery_url(settings.oidc_issuer),
        client_kwargs=client_kwargs,
    )
    return client


def _oidc_client(request: Request) -> StarletteOAuth2App:
    """The application's OIDC client; 503 ``LAE-AUTH-006`` when login is not configured."""
    client: StarletteOAuth2App | None = request.app.state.oidc_client
    if client is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, LOGIN_NOT_CONFIGURED)
    return client


def safe_next(value: object) -> str:
    """Return ``value`` if it is a same-origin absolute path, else ``/`` (no open redirect)."""
    if (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and value.isprintable()
    ):
        return value
    return "/"


def _idp_unavailable(exc: Exception) -> ApiError:
    logger.warning("oidc_provider_unavailable", error=repr(exc))
    return ApiError(status.HTTP_502_BAD_GATEWAY, IDP_UNAVAILABLE)


def _login_failed(error: str) -> ApiError:
    logger.info("oidc_login_failed", error=error)
    return ApiError(status.HTTP_400_BAD_REQUEST, LOGIN_FAILED, {"error": error})


def _display_name(claims: dict[str, Any], subject: str) -> str:
    """The first non-empty of the ``name``, ``preferred_username``, and ``email`` claims."""
    for claim in ("name", "preferred_username", "email"):
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return subject


async def upsert_oidc_user(session: AsyncSession, claims: dict[str, Any]) -> User:
    """Insert or update the user with ``subject = claims["sub"]``; give a new user ``Viewer``.

    ``display_name`` and ``email`` follow the identity provider on every login. A user without
    any role (a new one) gets ``DEFAULT_ROLE``. Commits.
    """
    subject = str(claims["sub"])
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    display_name = _display_name(claims, subject)
    user_id: UUID | None = await session.scalar(
        insert(User)
        .values(subject=subject, display_name=display_name, email=email)
        .on_conflict_do_update(
            index_elements=[User.subject],
            set_={"display_name": display_name, "email": email, "updated_at": func.now()},
        )
        .returning(User.id)
    )
    assert user_id is not None
    has_role = select(UserRole.user_id).where(UserRole.user_id == user_id).exists()
    await session.execute(
        insert(UserRole)
        .from_select(
            ["user_id", "role_id"],
            select(literal(user_id, Uuid), Role.id).where(Role.name == DEFAULT_ROLE, ~has_role),
        )
        .on_conflict_do_nothing()
    )
    await session.commit()
    user = await session.get(User, user_id, populate_existing=True)
    assert user is not None
    return user


def start_session(request: Request, user_id: UUID) -> None:
    """Replace the session's contents with a signed-in session for ``user_id``."""
    request.session.clear()
    request.session[SESSION_USER_KEY] = str(user_id)
    request.session[SESSION_ISSUED_KEY] = int(time.time())


async def session_principal(request: Request, session: AsyncSession) -> CurrentUser | None:
    """Return the session cookie's user, or ``None`` (and clear the session) if there is none.

    A session whose user no longer exists or is deactivated is cleared.
    """
    if "session" not in request.scope:
        return None
    raw_id = request.session.get(SESSION_USER_KEY)
    if raw_id is None:
        return None
    try:
        user_id = UUID(str(raw_id))
    except ValueError:
        user_id = None
    user = await session.get(User, user_id) if user_id is not None else None
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return await user_principal(session, user, "session")


@router.get("/login")
async def login(
    request: Request, next_path: Annotated[str | None, Query(alias="next")] = None
) -> Response:
    """Redirect to the identity provider; after the callback, go to ``next`` (default ``/``)."""
    client = _oidc_client(request)
    request.session[NEXT_KEY] = safe_next(next_path)
    try:
        response: Response = await client.authorize_redirect(
            request, request.url_for("auth_callback")
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise _idp_unavailable(exc) from exc
    return response


@router.get("/callback", name="auth_callback")
async def callback(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> RedirectResponse:
    """Finish the login: exchange the code, validate the ID token, upsert the user, sign in."""
    client = _oidc_client(request)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        raise _login_failed(exc.error or "oauth_error") from exc
    except JoseError as exc:
        raise _login_failed(ID_TOKEN_ERROR) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise _idp_unavailable(exc) from exc
    claims = token.get("userinfo")
    if not claims or not claims.get("sub"):
        raise _login_failed(ID_TOKEN_ERROR)
    user = await upsert_oidc_user(session, dict(claims))
    if not user.is_active:
        raise ApiError(status.HTTP_403_FORBIDDEN, ACCOUNT_INACTIVE)
    next_path = safe_next(request.session.get(NEXT_KEY))
    start_session(request, user.id)
    logger.info("login", user_id=str(user.id))
    return RedirectResponse(next_path, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    """End the session (a no-op without one)."""
    request.session.clear()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me")
async def me(user: Annotated[CurrentUser, Depends(current_user)]) -> Me:
    """The caller: id, display name, role names, and the permissions in effect, sorted."""
    return Me(
        id=user.id,
        display_name=user.display_name,
        roles=sorted(user.roles),
        permissions=sorted(user.permissions),
    )
