"""OIDC login against the in-process fake provider, the session cookie, logout, ``/auth/me``."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.auth.oidc import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS, safe_next
from lae.db.models import User
from tests.auth.conftest import (
    API_BASE_URL,
    CLIENT_ID,
    JWKS_URI,
    TOKEN_ENDPOINT,
    FakeOIDCProvider,
    login,
    other_key_with_same_kid,
    serve_app,
    set_active,
    unique_subject,
)


def set_cookie_header(response: Any) -> str:
    """The ``Set-Cookie`` header for the session cookie, lower-cased."""
    [header] = [
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(SESSION_COOKIE + "=")
    ]
    return str(header).lower()


async def test_login_redirects_to_the_issuer_with_state_nonce_and_pkce(db_url: str) -> None:
    async with serve_app(db_url) as served:
        response = await served.client.get("/auth/login")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://idp.test/realms/lae/protocol/openid-connect/auth?")
    params = dict(httpx.URL(location).params)
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == f"{API_BASE_URL}/auth/callback"
    assert set(params["scope"].split()) == {"openid", "email", "profile"}
    assert params["code_challenge_method"] == "S256"
    for key in ("state", "nonce", "code_challenge"):
        assert len(params[key]) >= 20, key


async def test_full_login_creates_a_viewer_and_a_session(
    db_url: str, db_engine: AsyncEngine
) -> None:
    idp = FakeOIDCProvider()
    sub = unique_subject()
    async with serve_app(db_url, idp) as served:
        callback = await login(
            served.client,
            idp,
            sub,
            next_path="/edit/0193",
            name="Ada Lovelace",
            email="ada@example.org",
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/edit/0193"
        me = await served.client.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert set(body) == {"id", "display_name", "roles", "permissions"}
    assert body["display_name"] == "Ada Lovelace"
    assert body["roles"] == ["Viewer"]
    assert body["permissions"] == []
    async with AsyncSession(db_engine) as session:
        user = await session.scalar(select(User).where(User.subject == sub))
    assert user is not None
    assert str(user.id) == body["id"]
    assert user.email == "ada@example.org"
    # Discovery, JWKS, and the code exchange all went to the provider.
    urls = {str(request.url.copy_with(query=None)) for request in idp.requests}
    assert {JWKS_URI, TOKEN_ENDPOINT} <= urls


async def test_second_login_updates_the_same_user(db_url: str) -> None:
    idp = FakeOIDCProvider()
    sub = unique_subject()
    async with serve_app(db_url, idp) as served:
        await login(served.client, idp, sub, name="Ada")
        first = (await served.client.get("/auth/me")).json()
        await served.client.post("/auth/logout")
        await login(served.client, idp, sub, name="Ada King", email="ada@example.org")
        second = (await served.client.get("/auth/me")).json()
    assert second["id"] == first["id"]
    assert second["display_name"] == "Ada King"
    assert second["roles"] == ["Viewer"]


async def test_display_name_falls_back_to_username_email_and_subject(db_url: str) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        names = []
        for claims in ({"preferred_username": "ada"}, {"email": "ada@example.org"}, {}):
            sub = unique_subject()
            await login(served.client, idp, sub, **claims)
            names.append(((await served.client.get("/auth/me")).json()["display_name"], sub))
    assert [name for name, _ in names[:2]] == ["ada", "ada@example.org"]
    assert names[2][0] == names[2][1]


async def test_session_cookie_is_signed_httponly_lax_and_secure(db_url: str) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        callback = await login(served.client, idp, unique_subject())
    header = set_cookie_header(callback)
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "secure" in header
    assert f"max-age={SESSION_MAX_AGE_SECONDS}" in header
    assert "path=/" in header


async def test_session_cookie_is_not_secure_in_development(db_url: str) -> None:
    async with serve_app(db_url, env="development") as served:
        response = await served.client.get("/auth/login")
    header = set_cookie_header(response)
    assert "secure" not in header
    assert "httponly" in header
    assert "samesite=lax" in header


async def test_tampered_session_cookie_counts_as_anonymous(db_url: str) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        await login(served.client, idp, unique_subject())
        assert (await served.client.get("/auth/me")).status_code == 200
        value = served.client.cookies[SESSION_COOKIE]
        payload, _, signature = value.partition(".")
        forged = payload[:-2] + ("A" if payload[-2] != "A" else "B") + payload[-1]
        async with served.new_client(cookies={SESSION_COOKIE: f"{forged}.{signature}"}) as other:
            response = await other.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-002"


async def test_logout_ends_the_session(db_url: str) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        await login(served.client, idp, unique_subject())
        response = await served.client.post("/auth/logout")
        assert response.status_code == 204
        me = await served.client.get("/auth/me")
        # Logging out without a session is a no-op.
        again = await served.client.post("/auth/logout")
    assert me.status_code == 401
    assert again.status_code == 204


@pytest.mark.parametrize(
    "next_path",
    ["https://evil.example/", "//evil.example/x", "/\\evil.example", "javascript:alert(1)"],
)
async def test_next_only_accepts_local_paths(db_url: str, next_path: str) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        callback = await login(served.client, idp, unique_subject(), next_path=next_path)
    assert callback.status_code == 303
    assert callback.headers["location"] == "/"


def test_safe_next() -> None:
    assert safe_next("/edit/1?tab=history#x") == "/edit/1?tab=history#x"
    assert safe_next(None) == "/"
    assert safe_next("") == "/"
    assert safe_next("/a\r\nSet-Cookie: x") == "/"
    assert safe_next(42) == "/"


async def test_callback_with_a_forged_state_fails(db_url: str) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        response = await served.client.get("/auth/login")
        code, _ = idp.authorize(response.headers["location"], unique_subject())
        callback = await served.client.get(
            "/auth/callback", params={"code": code, "state": "forged"}
        )
        me = await served.client.get("/auth/me")
    assert callback.status_code == 400
    assert callback.json() == {
        "code": "LAE-AUTH-007",
        "path": "/auth/callback",
        "params": {"error": "mismatching_state"},
    }
    assert me.status_code == 401


async def test_callback_without_a_login_fails(db_url: str) -> None:
    async with serve_app(db_url) as served:
        callback = await served.client.get("/auth/callback", params={"code": "x", "state": "y"})
    assert callback.status_code == 400
    assert callback.json()["params"] == {"error": "mismatching_state"}


async def test_provider_error_is_reported(db_url: str) -> None:
    async with serve_app(db_url) as served:
        response = await served.client.get("/auth/login")
        state = httpx.URL(response.headers["location"]).params["state"]
        callback = await served.client.get(
            "/auth/callback", params={"error": "access_denied", "state": state}
        )
    assert callback.status_code == 400
    assert callback.json()["params"] == {"error": "access_denied"}


async def test_wrong_pkce_verifier_is_rejected_by_the_provider(db_url: str) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        response = await served.client.get("/auth/login")
        code, state = idp.authorize(response.headers["location"], unique_subject())
        idp.grants[code].code_challenge = "not-the-challenge"
        callback = await served.client.get("/auth/callback", params={"code": code, "state": state})
    assert callback.status_code == 400
    assert callback.json()["params"] == {"error": "invalid_grant"}


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"nonce": "replayed"}, id="nonce"),
        pytest.param({"aud": "another-client"}, id="audience"),
        pytest.param({"iss": "https://evil.test/realms/lae"}, id="issuer"),
        pytest.param({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, id="exp"),
    ],
)
async def test_invalid_id_token_claims_are_rejected(db_url: str, overrides: dict[str, Any]) -> None:
    idp = FakeOIDCProvider(id_token_overrides=overrides)
    async with serve_app(db_url, idp) as served:
        callback = await login(served.client, idp, unique_subject())
        me = await served.client.get("/auth/me")
    assert callback.status_code == 400
    assert callback.json()["params"] == {"error": "invalid_id_token"}
    assert me.status_code == 401


async def test_id_token_signed_with_another_key_is_rejected(db_url: str) -> None:
    idp = FakeOIDCProvider(signing_key=other_key_with_same_kid())
    async with serve_app(db_url, idp) as served:
        callback = await login(served.client, idp, unique_subject())
    assert callback.status_code == 400
    assert callback.json()["params"] == {"error": "invalid_id_token"}


async def test_login_is_503_when_oidc_is_not_configured(db_url: str) -> None:
    async with serve_app(db_url, oidc_issuer=None, oidc_client_id=None) as served:
        login_response = await served.client.get("/auth/login")
        callback = await served.client.get("/auth/callback")
    for response, path in ((login_response, "/auth/login"), (callback, "/auth/callback")):
        assert response.status_code == 503
        assert response.json() == {"code": "LAE-AUTH-006", "path": path, "params": {}}


async def test_unreachable_provider_is_502(db_url: str) -> None:
    async with serve_app(db_url, FakeOIDCProvider(down=True)) as served:
        response = await served.client.get("/auth/login")
    assert response.status_code == 502
    assert response.json()["code"] == "LAE-AUTH-008"


async def test_deactivated_user_loses_the_session_and_cannot_log_in(
    db_url: str, db_engine: AsyncEngine
) -> None:
    idp = FakeOIDCProvider()
    sub = unique_subject()
    async with serve_app(db_url, idp) as served:
        await login(served.client, idp, sub)
        user_id = (await served.client.get("/auth/me")).json()["id"]
        await set_active(db_engine, user_id, False)
        me = await served.client.get("/auth/me")
        callback = await login(served.client, idp, sub)
        after = await served.client.get("/auth/me")
    assert me.status_code == 401
    assert callback.status_code == 403
    assert callback.json()["code"] == "LAE-AUTH-009"
    assert after.status_code == 401
