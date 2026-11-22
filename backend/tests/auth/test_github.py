"""GitHub login against the in-process fake GitHub; ``/auth/providers`` and the provider routes."""

from __future__ import annotations

import itertools
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.app import create_app
from lae.auth.github import primary_verified_email
from lae.auth.setup import AuthConfigError
from lae.db.models import User
from tests.auth.conftest import (
    API_BASE_URL,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_TOKEN_URL,
    FakeGitHub,
    FakeOIDCProvider,
    GitHubUser,
    github_settings,
    login,
    make_settings,
    serve_app,
    unique_subject,
)

UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://localhost:1/unused"
OIDC_INFO = {"id": "oidc", "display_name": "OpenID Connect"}
GITHUB_INFO = {"id": "github", "display_name": "GitHub"}

# Tests share one database (recreated per session): every account gets an id of its own.
_ids = itertools.count(1_000_001)


def github_user(
    login_name: str = "ada",
    *,
    name: str | None = "Ada Lovelace",
    email: str | None = "ada@example.org",
    verified: bool = True,
    orgs: dict[str, str] | None = None,
) -> GitHubUser:
    """A GitHub account with a unique id, a primary e-mail (unless ``None``), and orgs."""
    emails = [
        {"email": f"{login_name}@users.noreply.github.com", "primary": False, "verified": True},
    ]
    if email is not None:
        emails.append({"email": email, "primary": True, "verified": verified})
    return GitHubUser(id=next(_ids), login=login_name, name=name, emails=emails, orgs=orgs or {})


async def github_login(
    client: httpx.AsyncClient,
    github: FakeGitHub,
    user: GitHubUser,
    *,
    next_path: str | None = None,
) -> httpx.Response:
    """Go through ``/auth/login/github`` and ``/auth/callback/github``; return the callback."""
    response = await client.get(
        "/auth/login/github", params={"next": next_path} if next_path else None
    )
    assert response.status_code == 302, response.text
    code, state = github.authorize(response.headers["location"], user)
    return await client.get("/auth/callback/github", params={"code": code, "state": state})


async def user_by_subject(engine: AsyncEngine, subject: str) -> User | None:
    async with AsyncSession(engine) as session:
        return await session.scalar(select(User).where(User.subject == subject))


async def test_providers_lists_the_enabled_providers_default_first(db_url: str) -> None:
    async with serve_app(db_url, **github_settings()) as served:
        both = await served.client.get("/auth/providers")
    async with serve_app(db_url) as served:
        oidc_only = await served.client.get("/auth/providers")
    async with serve_app(
        db_url, oidc_issuer=None, oidc_client_id=None, **github_settings()
    ) as served:
        github_only = await served.client.get("/auth/providers")
    async with serve_app(db_url, oidc_issuer=None, oidc_client_id=None) as served:
        none = await served.client.get("/auth/providers")
    assert both.status_code == 200
    assert both.json() == [OIDC_INFO, GITHUB_INFO]
    assert oidc_only.json() == [OIDC_INFO]
    assert github_only.json() == [GITHUB_INFO]
    assert none.json() == []


async def test_login_redirects_to_github_with_state_pkce_and_scopes(db_url: str) -> None:
    async with serve_app(db_url, **github_settings()) as served:
        response = await served.client.get("/auth/login/github")
    assert response.status_code == 302
    location = httpx.URL(response.headers["location"])
    assert str(location.copy_with(query=None)) == "https://github.com/login/oauth/authorize"
    params = dict(location.params)
    assert params["response_type"] == "code"
    assert params["client_id"] == GITHUB_CLIENT_ID
    assert params["redirect_uri"] == f"{API_BASE_URL}/auth/callback/github"
    assert params["scope"] == "read:user user:email"
    assert params["code_challenge_method"] == "S256"
    assert len(params["state"]) >= 20
    assert len(params["code_challenge"]) == 43


async def test_org_allowlist_also_asks_for_read_org(db_url: str) -> None:
    async with serve_app(db_url, **github_settings(github_allowed_orgs=["linked-art"])) as served:
        response = await served.client.get("/auth/login/github")
    scope = httpx.URL(response.headers["location"]).params["scope"]
    assert scope == "read:user user:email read:org"


async def test_full_login_creates_a_viewer_keyed_by_the_numeric_id(
    db_url: str, db_engine: AsyncEngine
) -> None:
    github = FakeGitHub()
    account = github_user("ada", name="Ada Lovelace", email="ada@example.org")
    async with serve_app(db_url, github=github, **github_settings()) as served:
        callback = await github_login(served.client, github, account, next_path="/edit/0193")
        me = await served.client.get("/auth/me")
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "/edit/0193"
    assert me.status_code == 200
    assert me.json()["display_name"] == "Ada Lovelace"
    assert me.json()["roles"] == ["Viewer"]
    assert me.json()["permissions"] == []
    user = await user_by_subject(db_engine, f"github:{account.id}")
    assert user is not None
    assert str(user.id) == me.json()["id"]
    assert user.email == "ada@example.org"
    # The code went to the token endpoint with the client secret in the body and the PKCE
    # verifier; the token was then used for the user and the e-mail addresses only.
    [token_request] = [r for r in github.requests if str(r.url) == GITHUB_TOKEN_URL]
    form = dict(httpx.QueryParams(token_request.content.decode()))
    assert form["client_secret"] == GITHUB_CLIENT_SECRET
    assert form["code_verifier"]
    assert "authorization" not in token_request.headers
    assert github.api_paths() == ["/user", "/user/emails"]


async def test_a_renamed_account_is_the_same_user(db_url: str, db_engine: AsyncEngine) -> None:
    github = FakeGitHub()
    account = github_user("ada", name="Ada")
    async with serve_app(db_url, github=github, **github_settings()) as served:
        await github_login(served.client, github, account)
        first = (await served.client.get("/auth/me")).json()
        await served.client.post("/auth/logout")
        account.login = "ada-king"
        account.name = None
        await github_login(served.client, github, account)
        second = (await served.client.get("/auth/me")).json()
    assert second["id"] == first["id"]
    # Without a name the login is the display name.
    assert second["display_name"] == "ada-king"
    async with AsyncSession(db_engine) as session:
        count = await session.scalar(
            select(func.count()).where(User.subject == f"github:{account.id}")
        )
    assert count == 1


async def test_a_blank_name_falls_back_to_the_login(db_url: str) -> None:
    github = FakeGitHub()
    async with serve_app(db_url, github=github, **github_settings()) as served:
        await github_login(served.client, github, github_user("grace", name="   "))
        me = (await served.client.get("/auth/me")).json()
    assert me["display_name"] == "grace"


async def test_accounts_are_not_linked_by_email(db_url: str) -> None:
    idp = FakeOIDCProvider()
    github = FakeGitHub()
    async with serve_app(db_url, idp, github=github, **github_settings()) as served:
        await login(served.client, idp, unique_subject(), email="shared@example.org")
        oidc_id = (await served.client.get("/auth/me")).json()["id"]
        await served.client.post("/auth/logout")
        await github_login(served.client, github, github_user(email="shared@example.org"))
        github_id = (await served.client.get("/auth/me")).json()["id"]
    assert github_id != oidc_id


@pytest.mark.parametrize(
    ("email", "verified"),
    [pytest.param("ada@example.org", False, id="unverified"), pytest.param(None, True, id="none")],
)
async def test_an_unverified_primary_email_is_not_stored(
    db_url: str, db_engine: AsyncEngine, email: str | None, verified: bool
) -> None:
    github = FakeGitHub()
    account = github_user(email=email, verified=verified)
    async with serve_app(db_url, github=github, **github_settings()) as served:
        callback = await github_login(served.client, github, account)
        me = await served.client.get("/auth/me")
    # The user signs in, but neither the unverified primary nor a verified secondary (the
    # noreply address) becomes users.email.
    assert callback.status_code == 303
    assert me.status_code == 200
    user = await user_by_subject(db_engine, f"github:{account.id}")
    assert user is not None
    assert user.email is None


def test_primary_verified_email() -> None:
    assert primary_verified_email([{"email": "a@x", "primary": True, "verified": True}]) == "a@x"
    assert primary_verified_email([{"email": "a@x", "primary": True, "verified": False}]) is None
    assert primary_verified_email([{"email": "a@x", "primary": False, "verified": True}]) is None
    assert primary_verified_email([{"email": "a@x", "primary": True, "verified": "yes"}]) is None
    assert primary_verified_email({"email": "a@x"}) is None
    assert primary_verified_email([]) is None


async def test_callback_with_a_forged_state_fails(db_url: str) -> None:
    github = FakeGitHub()
    async with serve_app(db_url, github=github, **github_settings()) as served:
        response = await served.client.get("/auth/login/github")
        code, _ = github.authorize(response.headers["location"], github_user())
        callback = await served.client.get(
            "/auth/callback/github", params={"code": code, "state": "forged"}
        )
        me = await served.client.get("/auth/me")
    assert callback.status_code == 400
    assert callback.json() == {
        "code": "LAE-AUTH-007",
        "path": "/auth/callback/github",
        "params": {"error": "mismatching_state"},
    }
    assert me.status_code == 401
    # The code was never exchanged.
    assert not any(str(r.url) == GITHUB_TOKEN_URL for r in github.requests)


async def test_a_state_issued_for_another_provider_is_rejected(db_url: str) -> None:
    async with serve_app(db_url, **github_settings()) as served:
        response = await served.client.get("/auth/login/oidc")
        state = httpx.URL(response.headers["location"]).params["state"]
        callback = await served.client.get(
            "/auth/callback/github", params={"code": "x", "state": state}
        )
    assert callback.status_code == 400
    assert callback.json()["params"] == {"error": "mismatching_state"}


async def test_the_user_denying_access_is_reported(db_url: str) -> None:
    async with serve_app(db_url, **github_settings()) as served:
        response = await served.client.get("/auth/login/github")
        state = httpx.URL(response.headers["location"]).params["state"]
        callback = await served.client.get(
            "/auth/callback/github", params={"error": "access_denied", "state": state}
        )
    assert callback.status_code == 400
    assert callback.json()["params"] == {"error": "access_denied"}


async def test_a_rejected_code_is_a_failed_login(db_url: str) -> None:
    github = FakeGitHub()
    async with serve_app(db_url, github=github, **github_settings()) as served:
        response = await served.client.get("/auth/login/github")
        _, state = github.authorize(response.headers["location"], github_user())
        callback = await served.client.get(
            "/auth/callback/github", params={"code": "expired", "state": state}
        )
        me = await served.client.get("/auth/me")
    assert callback.status_code == 400
    assert callback.json()["params"] == {"error": "bad_verification_code"}
    assert me.status_code == 401


async def test_a_wrong_pkce_verifier_is_rejected_by_github(db_url: str) -> None:
    github = FakeGitHub()
    async with serve_app(db_url, github=github, **github_settings()) as served:
        response = await served.client.get("/auth/login/github")
        code, state = github.authorize(response.headers["location"], github_user())
        github.grants[code].code_challenge = "x" * 43
        callback = await served.client.get(
            "/auth/callback/github", params={"code": code, "state": state}
        )
    assert callback.status_code == 400
    assert callback.json()["params"] == {"error": "bad_verification_code"}


async def test_wrong_client_credentials_are_a_failed_login(db_url: str) -> None:
    github = FakeGitHub()
    settings = github_settings(github_client_secret="not-the-secret")
    async with serve_app(db_url, github=github, **settings) as served:
        callback = await github_login(served.client, github, github_user())
    assert callback.status_code == 400
    assert callback.json()["params"] == {"error": "incorrect_client_credentials"}


@pytest.mark.parametrize("token_status", [500, 503])
async def test_a_failing_token_endpoint_is_502(db_url: str, token_status: int) -> None:
    github = FakeGitHub(token_status=token_status)
    async with serve_app(db_url, github=github, **github_settings()) as served:
        callback = await github_login(served.client, github, github_user())
        me = await served.client.get("/auth/me")
    assert callback.status_code == 502
    assert callback.json()["code"] == "LAE-AUTH-008"
    assert me.status_code == 401


async def test_a_failing_api_is_502(db_url: str, db_engine: AsyncEngine) -> None:
    github = FakeGitHub(api_status=500)
    account = github_user()
    async with serve_app(db_url, github=github, **github_settings()) as served:
        callback = await github_login(served.client, github, account)
    assert callback.status_code == 502
    assert callback.json()["code"] == "LAE-AUTH-008"
    assert await user_by_subject(db_engine, f"github:{account.id}") is None


async def test_unreachable_github_is_502(db_url: str) -> None:
    github = FakeGitHub()
    async with serve_app(db_url, github=github, **github_settings()) as served:
        response = await served.client.get("/auth/login/github")
        code, state = github.authorize(response.headers["location"], github_user())
        github.down = True
        callback = await served.client.get(
            "/auth/callback/github", params={"code": code, "state": state}
        )
    assert callback.status_code == 502
    assert callback.json()["code"] == "LAE-AUTH-008"


@pytest.mark.parametrize(
    "allowlists",
    [
        pytest.param({"github_allowed_users": ["grace"]}, id="users"),
        pytest.param({"github_allowed_orgs": ["linked-art"]}, id="orgs"),
        pytest.param(
            {"github_allowed_users": ["grace"], "github_allowed_orgs": ["linked-art"]}, id="both"
        ),
    ],
)
@pytest.mark.parametrize(
    "orgs",
    [
        pytest.param({}, id="no-org"),
        pytest.param({"other-org": "active"}, id="other-org"),
        pytest.param({"linked-art": "pending"}, id="invited-only"),
    ],
)
async def test_an_account_on_no_allowlist_is_rejected(
    db_url: str, db_engine: AsyncEngine, allowlists: dict[str, Any], orgs: dict[str, str]
) -> None:
    github = FakeGitHub()
    account = github_user("mallory", orgs=orgs)
    async with serve_app(db_url, github=github, **github_settings(**allowlists)) as served:
        callback = await github_login(served.client, github, account)
        me = await served.client.get("/auth/me")
    assert callback.status_code == 403
    assert callback.json() == {
        "code": "LAE-AUTH-012",
        "path": "/auth/callback/github",
        "params": {"provider": "github"},
    }
    assert me.status_code == 401
    # A rejected account is never created.
    assert await user_by_subject(db_engine, f"github:{account.id}") is None


@pytest.mark.parametrize(
    ("allowlists", "account"),
    [
        pytest.param({"github_allowed_users": ["Grace"]}, github_user("grace"), id="user"),
        pytest.param(
            {"github_allowed_orgs": ["other", "Linked-Art"]},
            github_user("grace", orgs={"linked-art": "active"}),
            id="org-member",
        ),
        pytest.param(
            {"github_allowed_users": ["ada"], "github_allowed_orgs": ["linked-art"]},
            github_user("grace", orgs={"linked-art": "active"}),
            id="either-list",
        ),
    ],
)
async def test_an_allowlisted_account_signs_in_as_viewer(
    db_url: str, allowlists: dict[str, Any], account: GitHubUser
) -> None:
    github = FakeGitHub()
    async with serve_app(db_url, github=github, **github_settings(**allowlists)) as served:
        callback = await github_login(served.client, github, account)
        me = await served.client.get("/auth/me")
    assert callback.status_code == 303, callback.text
    assert me.json()["roles"] == ["Viewer"]


async def test_unknown_providers_are_404(db_url: str) -> None:
    async with serve_app(db_url) as served:
        # GitHub is not configured here, so it is as unknown as a made-up id.
        responses = [
            await served.client.get("/auth/login/github"),
            await served.client.get("/auth/callback/github", params={"code": "x", "state": "y"}),
            await served.client.get("/auth/login/nope"),
        ]
    for response, provider in zip(responses, ["github", "github", "nope"], strict=True):
        assert response.status_code == 404
        assert response.json()["code"] == "LAE-AUTH-011"
        assert response.json()["params"] == {"provider": provider}


async def test_the_default_provider_is_oidc_when_configured(db_url: str) -> None:
    async with serve_app(db_url, **github_settings()) as served:
        response = await served.client.get("/auth/login")
    location = httpx.URL(response.headers["location"])
    assert location.host == "idp.test"
    # The OIDC provider keeps the Step 1.7 redirect URI.
    assert location.params["redirect_uri"] == f"{API_BASE_URL}/auth/callback"


async def test_oidc_through_the_provider_routes(db_url: str) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp, **github_settings()) as served:
        response = await served.client.get("/auth/login/oidc", params={"next": "/x"})
        assert httpx.URL(response.headers["location"]).params["redirect_uri"] == (
            f"{API_BASE_URL}/auth/callback"
        )
        code, state = idp.authorize(response.headers["location"], unique_subject(), name="Ada")
        callback = await served.client.get(
            "/auth/callback/oidc", params={"code": code, "state": state}
        )
        me = await served.client.get("/auth/me")
    assert callback.status_code == 303
    assert callback.headers["location"] == "/x"
    assert me.json()["display_name"] == "Ada"


async def test_without_oidc_github_is_the_default(db_url: str) -> None:
    github = FakeGitHub()
    async with serve_app(
        db_url, github=github, oidc_issuer=None, oidc_client_id=None, **github_settings()
    ) as served:
        response = await served.client.get("/auth/login", params={"next": "/y"})
        location = httpx.URL(response.headers["location"])
        # GitHub always returns to its own callback, even when started from /auth/login.
        assert location.params["redirect_uri"] == f"{API_BASE_URL}/auth/callback/github"
        code, state = github.authorize(str(location), github_user())
        callback = await served.client.get(
            "/auth/callback/github", params={"code": code, "state": state}
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/y"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"github_client_id": GITHUB_CLIENT_ID}, id="id-only"),
        pytest.param({"github_client_secret": GITHUB_CLIENT_SECRET}, id="secret-only"),
    ],
)
def test_half_configured_github_refuses_to_start(overrides: dict[str, Any]) -> None:
    with pytest.raises(AuthConfigError, match="LAE_GITHUB_CLIENT_ID and LAE_GITHUB_CLIENT_SECRET"):
        create_app(make_settings(UNREACHABLE_DATABASE_URL, **overrides))


def test_the_client_secret_never_appears_in_settings_output() -> None:
    settings = make_settings(UNREACHABLE_DATABASE_URL, **github_settings())
    assert GITHUB_CLIENT_SECRET not in repr(settings)
    assert GITHUB_CLIENT_SECRET not in str(settings.model_dump())
