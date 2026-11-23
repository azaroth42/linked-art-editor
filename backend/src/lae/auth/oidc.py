"""Login routes for every provider, the OpenID Connect provider, and the signed session cookie.

Routes (Step 1.7; provider routes added with GitHub login, ADR-0026):

- ``GET /auth/providers``: the enabled sign-in providers ``[{id, display_name}]``, the default
  one first, so the SPA can offer one button per provider. Public.
- ``GET /auth/login/{provider}?next=/path`` redirects to the provider (authorization code flow,
  ``state``, PKCE ``S256``); ``GET /auth/callback/{provider}`` finishes it: exchange the code,
  identify the user (``lae.auth.providers.LoginProvider.identify``), upsert ``users`` by
  ``subject``, start a session, and redirect to ``next``. An id that is not an enabled provider
  is 404 ``LAE-AUTH-011``.
- ``GET /auth/login`` and ``GET /auth/callback`` (Step 1.7's routes) do the same for the default
  provider, the first enabled one: OIDC when ``LAE_OIDC_ISSUER`` is set, else GitHub. Without any
  provider they are 503 ``LAE-AUTH-006``.
- ``POST /auth/logout`` ends the session; ``GET /auth/me`` returns ``{id, display_name, roles,
  permissions}``.

Each provider returns to its own callback: ``/auth/callback/<id>``, except that the OIDC provider
keeps returning to ``/auth/callback`` so that identity provider registrations made for Step 1.7
stay valid (``/auth/callback/oidc`` finishes an OIDC login too).

The OIDC provider (``oidc``) uses discovery at ``<LAE_OIDC_ISSUER>/.well-known/openid-
configuration``, scopes ``openid email profile``, and validates the ID token (signature against
the issuer's JWKS, ``iss``, ``aud``, ``exp``, ``nonce``); ``users.subject`` is the ``sub``
claim. GitHub (``github``) is in ``lae.auth.github``.

The session is Starlette's ``SessionMiddleware`` cookie ``lae_session``: signed with
``LAE_SESSION_SECRET`` (itsdangerous, not encrypted), ``HttpOnly``, ``SameSite=Lax``, ``Secure``
unless ``LAE_ENV=development``, and re-issued on every response, so it lapses after
``SESSION_MAX_AGE_SECONDS`` without a request. It holds the user id, the time it was issued
(``iat``, for revoking sessions later), and, during a login, Authlib's state, nonce, and PKCE
verifier. A first-time user gets the ``Viewer`` role (``DEFAULT_ROLE``) whatever the provider. A
deactivated user (``users.is_active = false``) cannot log in, and an existing session of theirs
stops working.

Authlib uses ``httpx2`` when installed and otherwise ``httpx`` (decision F2), announcing the
fallback with a deprecation warning that is silenced at import below. The provider factories pass
the application's test transport to Authlib's clients, so tests run the whole flow against
in-process fake providers.
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
from lae.auth.errors import ACCOUNT_INACTIVE
from lae.auth.permissions import (
    DEFAULT_ROLE,
    CurrentUser,
    current_user,
    user_principal,
)
from lae.auth.providers import (
    PROVIDER_CALLBACK_ROUTE,
    LoginIdentity,
    LoginProvider,
    ProviderInfo,
    default_provider,
    get_provider,
    login_failed,
    login_providers,
    oauth_client_kwargs,
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
    from authlib.integrations.starlette_client import OAuth, StarletteOAuth2App

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

ID_TOKEN_ERROR: Final = "invalid_id_token"
"""``error`` parameter of ``LAE-AUTH-007`` when the ID token is missing or does not validate."""

LEGACY_CALLBACK_ROUTE: Final = "auth_callback"
"""Route name of ``GET /auth/callback`` (the default provider's callback since Step 1.7)."""

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
    """Return the Authlib client for ``LAE_OIDC_ISSUER``, or ``None`` when OIDC is not set up."""
    if not settings.oidc_issuer:
        return None
    secret = settings.oidc_client_secret
    oauth = OAuth()
    client: StarletteOAuth2App = oauth.register(
        OIDC_CLIENT_NAME,
        client_id=settings.oidc_client_id,
        client_secret=secret.get_secret_value() if secret is not None else None,
        server_metadata_url=discovery_url(settings.oidc_issuer),
        client_kwargs=oauth_client_kwargs(OIDC_SCOPE, http_transport),
    )
    return client


def _display_name(claims: dict[str, Any], subject: str) -> str:
    """The first non-empty of the ``name``, ``preferred_username``, and ``email`` claims."""
    for claim in ("name", "preferred_username", "email"):
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return subject


def oidc_identity(claims: dict[str, Any]) -> LoginIdentity:
    """The person an ID token's claims describe: ``subject`` is ``sub``."""
    subject = str(claims["sub"])
    email = claims.get("email")
    return LoginIdentity(
        subject=subject,
        display_name=_display_name(claims, subject),
        email=email if isinstance(email, str) else None,
    )


class OIDCProvider(LoginProvider):
    """Sign in with any OpenID Connect issuer (``LAE_OIDC_ISSUER``)."""

    id = OIDC_CLIENT_NAME
    display_name = "OpenID Connect"

    def redirect_uri(self, request: Request) -> str:
        """``/auth/callback``, as registered with identity providers since Step 1.7."""
        return str(request.url_for(LEGACY_CALLBACK_ROUTE))

    async def fetch_token(self, request: Request) -> dict[str, Any]:
        """Exchange the code; Authlib validates the ID token against the nonce and the JWKS."""
        try:
            return await super().fetch_token(request)
        except JoseError as exc:
            raise login_failed(self.id, ID_TOKEN_ERROR) from exc

    async def identify(self, token: dict[str, Any]) -> LoginIdentity:
        """The validated ID token's claims; 400 ``invalid_id_token`` without one."""
        claims = token.get("userinfo")
        if not claims or not claims.get("sub"):
            raise login_failed(self.id, ID_TOKEN_ERROR)
        return oidc_identity(dict(claims))


def create_oidc_provider(
    settings: Settings, http_transport: httpx.AsyncBaseTransport | None = None
) -> OIDCProvider | None:
    """The OIDC provider, or ``None`` when ``LAE_OIDC_ISSUER`` is not set."""
    client = create_oidc_client(settings, http_transport)
    return OIDCProvider(client) if client is not None else None


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


async def upsert_login_user(session: AsyncSession, identity: LoginIdentity) -> User:
    """Insert or update the user with ``subject = identity.subject``; give a new user ``Viewer``.

    ``display_name`` and ``email`` follow the identity provider on every login. A user without
    any role (a new one) gets ``DEFAULT_ROLE``. Commits.
    """
    values = {"display_name": identity.display_name, "email": identity.email}
    user_id: UUID | None = await session.scalar(
        insert(User)
        .values(subject=identity.subject, **values)
        .on_conflict_do_update(
            index_elements=[User.subject], set_={**values, "updated_at": func.now()}
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


def has_signed_in_session(request: Request) -> bool:
    """True if the request carries a valid session cookie of a signed-in user.

    Only the cookie is read (its signature and age are checked by the session middleware); the
    user is not looked up, so this costs nothing on public pages.
    """
    return "session" in request.scope and request.session.get(SESSION_USER_KEY) is not None


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


async def start_login(request: Request, provider: LoginProvider, next_path: str | None) -> Response:
    """Remember ``next`` and redirect to ``provider``."""
    request.session[NEXT_KEY] = safe_next(next_path)
    return await provider.authorize_redirect(request)


async def finish_login(
    request: Request, session: AsyncSession, provider: LoginProvider
) -> RedirectResponse:
    """Identify the user through ``provider``, upsert them, sign them in, and go to ``next``."""
    identity = await provider.authenticate(request)
    user = await upsert_login_user(session, identity)
    if not user.is_active:
        raise ApiError(status.HTTP_403_FORBIDDEN, ACCOUNT_INACTIVE)
    next_path = safe_next(request.session.get(NEXT_KEY))
    start_session(request, user.id)
    logger.info("login", user_id=str(user.id), provider=provider.id)
    return RedirectResponse(next_path, status_code=status.HTTP_303_SEE_OTHER)


NextQuery = Annotated[str | None, Query(alias="next")]


@router.get("/providers")
async def providers(request: Request) -> list[ProviderInfo]:
    """The enabled sign-in providers, the default one first (empty when none is configured)."""
    return [provider.info() for provider in login_providers(request).values()]


@router.get("/login")
async def login(request: Request, next_path: NextQuery = None) -> Response:
    """Redirect to the default provider; after the callback, go to ``next`` (default ``/``)."""
    return await start_login(request, default_provider(request), next_path)


@router.get("/login/{provider}")
async def provider_login(request: Request, provider: str, next_path: NextQuery = None) -> Response:
    """Redirect to ``provider``; after its callback, go to ``next`` (default ``/``)."""
    return await start_login(request, get_provider(request, provider), next_path)


@router.get("/callback", name=LEGACY_CALLBACK_ROUTE)
async def callback(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> RedirectResponse:
    """Finish a login with the default provider (the OIDC provider's redirect URI)."""
    return await finish_login(request, session, default_provider(request))


@router.get("/callback/{provider}", name=PROVIDER_CALLBACK_ROUTE)
async def provider_callback(
    request: Request, provider: str, session: Annotated[AsyncSession, Depends(get_session)]
) -> RedirectResponse:
    """Finish a login with ``provider``: exchange the code, upsert the user, sign them in."""
    return await finish_login(request, session, get_provider(request, provider))


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
