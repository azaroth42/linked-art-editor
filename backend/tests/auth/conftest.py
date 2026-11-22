"""Test harness for ``lae.auth``: an in-process fake OIDC provider, the app, and DB helpers.

There is no identity provider on the development machine or in CI, so ``FakeOIDCProvider``
plays one over ``httpx.MockTransport`` (no network): discovery document, JWKS, and a token
endpoint that checks the client secret, the redirect URI, and the PKCE verifier, and returns an
RS256-signed ID token carrying the nonce from the authorization request. ``create_app`` passes
the transport to Authlib, so the real login code runs end to end. A manual login against a real
IdP is recorded as not performed in ``docs/questions.md`` (Step 1.7).

``serve_app`` runs the application (with its lifespan) plus ``protected_router``, a router that
exists only in tests so that 401/403 can be checked before Step 1.6 adds ``/api/records``. Its
client uses ``https://api.test`` because the session cookie is ``Secure`` outside development.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Annotated, Any
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Depends, FastAPI
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from lae.app import create_app
from lae.auth import CurrentUser, current_user, require
from lae.auth.tokens import generate_token, hash_token
from lae.db.models import ApiToken, Role, User, UserRole
from lae.settings import Settings

API_BASE_URL = "https://api.test"
ISSUER = "https://idp.test/realms/lae"
CLIENT_ID = "lae-editor"
CLIENT_SECRET = "idp-client-secret"
SESSION_SECRET = "test-session-secret"

AUTHORIZATION_ENDPOINT = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN_ENDPOINT = f"{ISSUER}/protocol/openid-connect/token"
JWKS_URI = f"{ISSUER}/protocol/openid-connect/certs"


@cache
def signing_key(kid: str = "idp-key-1") -> RSAKey:
    """An RSA signing key, generated once per test session and ``kid``."""
    return RSAKey.generate_key(2048, parameters={"kid": kid, "use": "sig", "alg": "RS256"})


def other_key_with_same_kid() -> RSAKey:
    """A different RSA key that claims the provider's ``kid`` (a forged signature)."""
    key = signing_key("forger")
    data = key.as_dict(private=True)
    data["kid"] = "idp-key-1"
    return RSAKey.import_key(data)


@dataclass
class Grant:
    """An authorization code the fake provider issued."""

    redirect_uri: str
    nonce: str
    code_challenge: str
    claims: dict[str, Any]


@dataclass
class FakeOIDCProvider:
    """An OpenID Connect provider answering ``httpx`` requests in-process."""

    grants: dict[str, Grant] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    down: bool = False
    """When true, every request fails as if the provider were unreachable."""
    id_token_overrides: dict[str, Any] = field(default_factory=dict)
    """Claims replacing the computed ones in the next ID tokens (for negative tests)."""
    signing_key: RSAKey | None = None
    """Key signing ID tokens; ``None`` is the key published in the JWKS."""

    def discovery(self) -> dict[str, Any]:
        return {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZATION_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "jwks_uri": JWKS_URI,
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        }

    def authorize(self, location: str, sub: str, **claims: Any) -> tuple[str, str]:
        """Play the user approving the login at ``location``; return ``(code, state)``."""
        url = httpx.URL(location)
        assert str(url.copy_with(query=None)) == AUTHORIZATION_ENDPOINT
        params = url.params
        assert params["response_type"] == "code"
        assert params["client_id"] == CLIENT_ID
        assert params["code_challenge_method"] == "S256"
        assert "openid" in params["scope"].split()
        code = secrets.token_urlsafe(16)
        self.grants[code] = Grant(
            redirect_uri=params["redirect_uri"],
            nonce=params["nonce"],
            code_challenge=params["code_challenge"],
            claims={"sub": sub, **claims},
        )
        return code, params["state"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        """The ``httpx.MockTransport`` handler."""
        self.requests.append(request)
        if request.url.host != "idp.test":
            raise AssertionError(f"unexpected outgoing request: {request.method} {request.url}")
        if self.down:
            raise httpx.ConnectError("identity provider unreachable", request=request)
        url = str(request.url.copy_with(query=None))
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery())
        if url == JWKS_URI:
            return httpx.Response(200, json={"keys": [signing_key().as_dict(private=False)]})
        if url == TOKEN_ENDPOINT and request.method == "POST":
            return self.token(request)
        return httpx.Response(404)

    def token(self, request: httpx.Request) -> httpx.Response:
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return httpx.Response(401, json={"error": "invalid_client"})
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        grant = self.grants.pop(form.get("code", ""), None)
        if grant is None or form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        if form.get("redirect_uri") != grant.redirect_uri:
            return httpx.Response(400, json={"error": "invalid_grant"})
        verifier = form.get("code_verifier", "")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if challenge != grant.code_challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": grant.nonce,
            **grant.claims,
            **self.id_token_overrides,
        }
        key = self.signing_key or signing_key()
        id_token = jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)
        return httpx.Response(
            200,
            json={
                "access_token": secrets.token_urlsafe(16),
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": id_token,
            },
        )


protected_router = APIRouter(prefix="/test", tags=["test"])
"""Routes that exist only in tests, standing in for Step 1.6's protected endpoints."""


@protected_router.get("/whoami")
async def whoami(user: Annotated[CurrentUser, Depends(current_user)]) -> dict[str, Any]:
    return {"id": str(user.id), "via": user.via}


@protected_router.post("/drafts")
async def edit_draft(
    user: Annotated[CurrentUser, Depends(require("edit_draft"))],
) -> dict[str, str]:
    return {"id": str(user.id)}


@protected_router.post("/publish")
async def publish(user: Annotated[CurrentUser, Depends(require("publish"))]) -> dict[str, str]:
    return {"id": str(user.id)}


@protected_router.post(
    "/both",
    dependencies=[Depends(require("edit_draft")), Depends(require("publish"))],
)
async def both(user: Annotated[CurrentUser, Depends(current_user)]) -> dict[str, str]:
    return {"id": str(user.id)}


def make_settings(database_url: str, **overrides: Any) -> Settings:
    """Settings for an app with OIDC against ``FakeOIDCProvider`` and no dev user."""
    values: dict[str, Any] = {
        "database_url": database_url,
        "env": "test",
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "session_secret": SESSION_SECRET,
        "dev_user": None,
        "qlever_url": None,
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class Served:
    """A running application and an HTTP client calling it in-process."""

    app: FastAPI
    client: httpx.AsyncClient

    def new_client(self, **kwargs: Any) -> httpx.AsyncClient:
        """Another client of the same app, with its own (empty) cookie jar."""
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=API_BASE_URL, **kwargs
        )


@asynccontextmanager
async def serve_app(
    database_url: str, idp: FakeOIDCProvider | None = None, **overrides: Any
) -> AsyncIterator[Served]:
    """Run ``create_app`` (plus ``protected_router``) with its lifespan; yield app and client."""
    provider = idp or FakeOIDCProvider()
    app = create_app(
        make_settings(database_url, **overrides),
        http_transport=httpx.MockTransport(provider.handle),
    )
    app.include_router(protected_router)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
            yield Served(app, client)


async def login(
    client: httpx.AsyncClient,
    idp: FakeOIDCProvider,
    sub: str,
    *,
    next_path: str | None = None,
    **claims: Any,
) -> httpx.Response:
    """Go through ``/auth/login`` and ``/auth/callback``; return the callback response."""
    response = await client.get("/auth/login", params={"next": next_path} if next_path else None)
    assert response.status_code == 302, response.text
    code, state = idp.authorize(response.headers["location"], sub, **claims)
    return await client.get("/auth/callback", params={"code": code, "state": state})


def unique_subject() -> str:
    """A ``sub`` claim no other test uses (tests share one database)."""
    return f"test|{uuid7()}"


async def create_user(
    engine: AsyncEngine,
    *roles: str,
    active: bool = True,
    subject: str | None = None,
    display_name: str = "Test User",
) -> Any:
    """Insert a user with the given role names (committed); return its id."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = User(subject=subject or unique_subject(), display_name=display_name)
        user.is_active = active
        session.add(user)
        await session.flush()
        await set_roles_in(session, user.id, roles)
        await session.commit()
        return user.id


async def set_roles_in(session: AsyncSession, user_id: Any, roles: tuple[str, ...]) -> None:
    """Replace a user's roles with ``roles`` inside ``session`` (not committed)."""
    for membership in await session.scalars(select(UserRole).where(UserRole.user_id == user_id)):
        await session.delete(membership)
    await session.flush()
    for role in roles:
        role_id = await session.scalar(select(Role.id).where(Role.name == role))
        assert role_id is not None, f"role {role!r} is not seeded"
        session.add(UserRole(user_id=user_id, role_id=role_id))


async def set_roles(engine: AsyncEngine, user_id: Any, *roles: str) -> None:
    """Replace a user's roles (committed)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await set_roles_in(session, user_id, roles)
        await session.commit()


async def set_active(engine: AsyncEngine, user_id: Any, active: bool) -> None:
    """Activate or deactivate a user (committed)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.is_active = active
        await session.commit()


async def create_token(
    engine: AsyncEngine,
    user_id: Any,
    *scopes: str,
    expires_at: datetime | None = None,
) -> tuple[str, Any]:
    """Insert a token row directly (committed); return ``(plaintext, id)``."""
    plaintext = generate_token()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        token = ApiToken(
            user_id=user_id,
            name="test",
            token_hash=hash_token(plaintext),
            scopes=list(scopes),
            expires_at=expires_at,
        )
        session.add(token)
        await session.commit()
        return plaintext, token.id


def bearer(token: str) -> dict[str, str]:
    """``Authorization`` header for a bearer token."""
    return {"Authorization": f"Bearer {token}"}
