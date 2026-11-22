"""Permission names, seeded roles, ``require``, 401/403 bodies, and per-request caching."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.auth import ALL_PERMISSIONS, ROLE_PERMISSIONS, require
from lae.auth.errors import AUTH_CODES
from lae.auth.permissions import load_roles
from lae.db.models import Role
from tests.auth.conftest import (
    FakeOIDCProvider,
    bearer,
    create_token,
    create_user,
    login,
    serve_app,
    set_roles,
    unique_subject,
)

ERRORS_JSON = Path(__file__).resolve().parents[3] / "frontend/public/locales/en/errors.json"

SPEC_PERMISSIONS = [
    "edit_draft",
    "publish",
    "delete",
    "merge",
    "unmerge",
    "protect",
    "propose_merge",
    "review_proposal",
    "approve_merge_scoped",
    "manage_vocabularies",
    "manage_roles",
    "manage_users",
    "manage_backups",
    "manage_system",
    "view_analytics",
]
"""Conventions §2 / spec §8.2, copied so that a rename shows up as a test failure."""


def test_permission_names_are_exactly_those_of_spec_8_2() -> None:
    assert list(ALL_PERMISSIONS) == SPEC_PERMISSIONS


def test_roles_are_those_of_spec_10_4_with_the_specified_permissions() -> None:
    assert set(ROLE_PERMISSIONS) == {
        "Viewer",
        "Collaborator",
        "Editor",
        "Vocabulary Admin",
        "System Admin",
        "Tier 1 Agent",
        "Tier 2 Agent",
    }
    assert ROLE_PERMISSIONS["Viewer"] == set()
    assert ROLE_PERMISSIONS["Collaborator"] == {"edit_draft"}
    assert ROLE_PERMISSIONS["Editor"] == {
        "edit_draft",
        "publish",
        "delete",
        "merge",
        "unmerge",
        "protect",
    }
    assert ROLE_PERMISSIONS["System Admin"] == set(SPEC_PERMISSIONS)
    assert ROLE_PERMISSIONS["Tier 1 Agent"] == {"edit_draft", "propose_merge"}
    assert ROLE_PERMISSIONS["Tier 2 Agent"] == {"review_proposal", "approve_merge_scoped"}
    assert ROLE_PERMISSIONS["Vocabulary Admin"] == {"manage_vocabularies"}


async def test_migration_seeds_exactly_the_role_constants(db_session: AsyncSession) -> None:
    rows = (await db_session.execute(select(Role.name, Role.permissions))).all()
    seeded = {name: frozenset(permissions) for name, permissions in rows}
    assert seeded == ROLE_PERMISSIONS


async def test_load_roles_unions_the_permissions_of_every_role(db_engine: AsyncEngine) -> None:
    user_id = await create_user(db_engine, "Collaborator", "Vocabulary Admin", "Tier 1 Agent")
    async with AsyncSession(db_engine) as session:
        roles, permissions = await load_roles(session, user_id)
    assert roles == {"Collaborator", "Vocabulary Admin", "Tier 1 Agent"}
    assert permissions == {"edit_draft", "manage_vocabularies", "propose_merge"}


def test_require_rejects_unknown_permission_names_at_definition() -> None:
    with pytest.raises(ValueError, match="publsh"):
        require("edit_draft", "publsh")


async def test_anonymous_requests_get_401_with_a_structured_body(db_url: str) -> None:
    async with serve_app(db_url) as served:
        responses = {
            "/test/whoami": await served.client.get("/test/whoami"),
            "/test/drafts": await served.client.post("/test/drafts"),
            "/auth/me": await served.client.get("/auth/me"),
            "/api/tokens": await served.client.get("/api/tokens"),
        }
    for path, response in responses.items():
        assert response.status_code == 401, path
        assert response.json() == {"code": "LAE-AUTH-002", "path": path, "params": {}}
        assert response.headers["www-authenticate"] == 'Bearer realm="lae"'


async def test_viewer_gets_403_on_an_edit_draft_endpoint(db_url: str) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        await login(served.client, idp, unique_subject())  # a new user is a Viewer
        whoami = await served.client.get("/test/whoami")
        response = await served.client.post("/test/drafts")
    assert whoami.status_code == 200
    assert whoami.json()["via"] == "session"
    assert response.status_code == 403
    assert response.json() == {
        "code": "LAE-AUTH-001",
        "path": "/test/drafts",
        "params": {"required": ["edit_draft"], "missing": ["edit_draft"]},
    }


async def test_role_changes_apply_from_the_next_request(
    db_url: str, db_engine: AsyncEngine
) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        await login(served.client, idp, unique_subject())
        user_id = (await served.client.get("/auth/me")).json()["id"]

        await set_roles(db_engine, user_id, "Collaborator")
        assert (await served.client.post("/test/drafts")).status_code == 200
        publish = await served.client.post("/test/publish")
        assert publish.status_code == 403
        assert publish.json()["params"]["missing"] == ["publish"]

        await set_roles(db_engine, user_id, "Editor")
        assert (await served.client.post("/test/publish")).status_code == 200
        assert (await served.client.post("/test/both")).status_code == 200

        await set_roles(db_engine, user_id)
        both = await served.client.post("/test/both")
    assert both.status_code == 403


async def test_system_admin_holds_every_permission(db_url: str, db_engine: AsyncEngine) -> None:
    user_id = await create_user(db_engine, "System Admin")
    token, _ = await create_token(db_engine, user_id, *SPEC_PERMISSIONS)
    async with serve_app(db_url) as served:
        me = await served.client.get("/auth/me", headers=bearer(token))
    assert me.json()["roles"] == ["System Admin"]
    assert me.json()["permissions"] == sorted(SPEC_PERMISSIONS)


async def test_permissions_are_read_once_per_request(db_url: str, db_engine: AsyncEngine) -> None:
    user_id = await create_user(db_engine, "Editor")
    token, _ = await create_token(db_engine, user_id, "edit_draft", "publish")
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    async with serve_app(db_url) as served:
        engine: AsyncEngine = served.app.state.engine
        await served.client.post("/test/both", headers=bearer(token))  # warm up last_used_at
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            # /test/both depends on require("edit_draft"), require("publish"), and current_user.
            response = await served.client.post("/test/both", headers=bearer(token))
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert response.status_code == 200
    role_queries = [s for s in statements if "FROM roles" in s]
    assert len(role_queries) == 1, statements


def test_every_auth_code_has_an_english_message() -> None:
    messages: dict[str, str] = json.loads(ERRORS_JSON.read_text())
    auth_keys = {key for key in messages if key.startswith("LAE-AUTH-")}
    assert auth_keys == set(AUTH_CODES)
    for code in AUTH_CODES:
        assert messages[code].strip(), code
