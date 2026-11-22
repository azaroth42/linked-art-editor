"""``LAE_DEV_USER`` and the settings checks that make ``lae api`` refuse to start."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

import lae.cli
from lae.app import create_app
from lae.auth import ROLE_PERMISSIONS
from lae.auth.dev import DevUser, parse_dev_user
from lae.auth.setup import AuthConfigError
from lae.cli import EXIT_CONFIG_ERROR, main
from lae.db.models import User
from lae.settings import get_settings
from tests.auth.conftest import bearer, create_token, create_user, make_settings, serve_app

UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://lae@127.0.0.1:1/lae_unreachable"


def dev_email() -> str:
    return f"dev-{uuid7()}@example.org"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("editor@example.org:Editor", DevUser("editor@example.org", "Editor")),
        (" admin@example.org : System Admin ", DevUser("admin@example.org", "System Admin")),
        ("bot@example.org:Tier 1 Agent", DevUser("bot@example.org", "Tier 1 Agent")),
    ],
)
def test_parse_dev_user(value: str, expected: DevUser) -> None:
    assert parse_dev_user(value) == expected
    assert expected.subject == "dev:" + expected.email


@pytest.mark.parametrize(
    "value", ["editor@example.org", ":Editor", "editor@example.org:", "x@example.org:Superuser"]
)
def test_parse_dev_user_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ValueError, match="LAE_DEV_USER"):
        parse_dev_user(value)


async def test_every_request_runs_as_the_dev_user(db_url: str, db_engine: AsyncEngine) -> None:
    email = dev_email()
    async with serve_app(db_url, dev_user=f"{email}:Editor") as served:
        me = await served.client.get("/auth/me")
        again = await served.client.get("/auth/me")
        whoami = await served.client.get("/test/whoami")
        publish = await served.client.post("/test/publish")
        merge_free = await served.client.post("/test/both")
    assert me.status_code == 200
    assert me.json()["display_name"] == email
    assert me.json()["roles"] == ["Editor"]
    assert me.json()["permissions"] == sorted(ROLE_PERMISSIONS["Editor"])
    assert again.json() == me.json()
    assert whoami.json()["via"] == "dev"
    assert publish.status_code == 200
    assert merge_free.status_code == 200
    async with AsyncSession(db_engine) as session:
        users = (await session.scalars(select(User).where(User.subject == f"dev:{email}"))).all()
    assert [str(user.id) for user in users] == [me.json()["id"]]


async def test_a_changed_dev_role_replaces_the_previous_one(db_url: str) -> None:
    email = dev_email()
    async with serve_app(db_url, dev_user=f"{email}:Editor") as served:
        editor = (await served.client.get("/auth/me")).json()
    async with serve_app(db_url, dev_user=f"{email}:Viewer") as served:
        viewer = (await served.client.get("/auth/me")).json()
        drafts = await served.client.post("/test/drafts")
    assert viewer["id"] == editor["id"]
    assert viewer["roles"] == ["Viewer"]
    assert viewer["permissions"] == []
    assert drafts.status_code == 403


async def test_a_bearer_token_takes_precedence_over_the_dev_user(
    db_url: str, db_engine: AsyncEngine
) -> None:
    user_id = await create_user(db_engine, "Collaborator")
    token, _ = await create_token(db_engine, user_id, "edit_draft")
    async with serve_app(db_url, dev_user=f"{dev_email()}:System Admin") as served:
        as_token = await served.client.get("/auth/me", headers=bearer(token))
        bad_token = await served.client.get("/auth/me", headers=bearer("lae_pat_nope"))
    assert as_token.json()["id"] == str(user_id)
    assert as_token.json()["roles"] == ["Collaborator"]
    assert bad_token.status_code == 401
    assert bad_token.json()["code"] == "LAE-AUTH-003"


async def test_the_dev_user_may_create_tokens(db_url: str) -> None:
    async with serve_app(db_url, dev_user=f"{dev_email()}:Collaborator") as served:
        created = await served.client.post(
            "/api/tokens", json={"name": "dev", "scopes": ["edit_draft"]}
        )
    assert created.status_code == 201


def test_production_refuses_the_dev_user() -> None:
    settings = make_settings(
        UNREACHABLE_DATABASE_URL, env="production", dev_user="editor@example.org:Editor"
    )
    with pytest.raises(AuthConfigError, match="LAE_DEV_USER.*production"):
        create_app(settings)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"dev_user": "editor@example.org"}, "LAE_DEV_USER"),
        ({"dev_user": "editor@example.org:Root"}, "LAE_DEV_USER role 'Root'"),
        ({"oidc_client_id": None}, "LAE_OIDC_CLIENT_ID"),
        ({"env": "staging", "session_secret": None}, "LAE_SESSION_SECRET"),
        ({"env": "production", "session_secret": None}, "LAE_SESSION_SECRET"),
    ],
)
def test_unsafe_settings_refuse_to_start(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(AuthConfigError, match=message):
        create_app(make_settings(UNREACHABLE_DATABASE_URL, **overrides))


@pytest.mark.parametrize("env", ["development", "test"])
def test_a_missing_session_secret_is_allowed_outside_staging_and_production(env: str) -> None:
    create_app(make_settings(UNREACHABLE_DATABASE_URL, env=env, session_secret=None))


def test_production_with_its_settings_starts() -> None:
    create_app(make_settings(UNREACHABLE_DATABASE_URL, env="production"))


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Clear the cached settings around a test that sets ``LAE_*`` variables."""
    get_settings.cache_clear()
    monkeypatch.setattr(lae.cli, "configure_logging", lambda level: None)
    yield monkeypatch
    get_settings.cache_clear()


def test_lae_api_refuses_to_start_with_a_dev_user_in_production(
    settings_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[Any] = []
    settings_env.setattr(uvicorn, "run", lambda app, **kwargs: started.append(app))
    settings_env.setenv("LAE_ENV", "production")
    settings_env.setenv("LAE_DEV_USER", "editor@example.org:Editor")
    settings_env.setenv("LAE_SESSION_SECRET", "s3cret")
    assert main(["api"]) == EXIT_CONFIG_ERROR
    assert started == []
    assert "refusing to start" in capsys.readouterr().err
