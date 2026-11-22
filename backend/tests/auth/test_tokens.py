"""Personal Access Tokens: creation, bearer authentication, scopes, expiry, revocation."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import lae.auth.tokens
from lae.auth.tokens import TOKEN_PREFIX, generate_token, hash_token
from lae.db.models import ApiToken
from tests.auth.conftest import (
    FakeOIDCProvider,
    Served,
    bearer,
    create_token,
    create_user,
    login,
    serve_app,
    set_active,
    set_roles,
    unique_subject,
)


async def session_user(
    served: Served, idp: FakeOIDCProvider, engine: AsyncEngine, *roles: str
) -> str:
    """Log ``served.client`` in as a new user, give it ``roles``; return the user id."""
    await login(served.client, idp, unique_subject())
    user_id: str = (await served.client.get("/auth/me")).json()["id"]
    await set_roles(engine, user_id, *roles)
    return user_id


async def stored_token(engine: AsyncEngine, token_id: Any) -> ApiToken:
    async with AsyncSession(engine) as session:
        token = await session.get(ApiToken, token_id)
    assert token is not None
    return token


def test_tokens_are_random_prefixed_and_stored_as_sha256_hex() -> None:
    first, second = generate_token(), generate_token()
    assert first != second
    assert first.startswith(TOKEN_PREFIX)
    assert len(first) >= len(TOKEN_PREFIX) + 43
    assert hash_token(first) == hashlib.sha256(first.encode()).hexdigest()


async def test_pat_round_trip(db_url: str, db_engine: AsyncEngine) -> None:
    idp = FakeOIDCProvider()
    expires_at = datetime.now(UTC) + timedelta(days=30)
    async with serve_app(db_url, idp) as served:
        user_id = await session_user(served, idp, db_engine, "Collaborator")
        created = await served.client.post(
            "/api/tokens",
            json={"name": "etl", "scopes": ["edit_draft"], "expires_at": expires_at.isoformat()},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        plaintext = body["token"]
        listed = await served.client.get("/api/tokens")

        async with served.new_client(headers=bearer(plaintext)) as script:
            me = await script.get("/auth/me")
            whoami = await script.get("/test/whoami")
            drafts = await script.post("/test/drafts")
            publish = await script.post("/test/publish")

    assert plaintext.startswith(TOKEN_PREFIX)
    assert body["name"] == "etl"
    assert body["scopes"] == ["edit_draft"]
    assert datetime.fromisoformat(body["expires_at"]) == expires_at
    assert body["last_used_at"] is None
    # Shown once: the listing has no plaintext.
    assert listed.status_code == 200
    [entry] = listed.json()
    assert entry["id"] == body["id"]
    assert "token" not in entry
    assert plaintext not in listed.text
    # Stored as the SHA-256 digest only.
    stored = await stored_token(db_engine, body["id"])
    assert stored.token_hash == hashlib.sha256(plaintext.encode()).hexdigest()
    assert str(stored.user_id) == user_id
    # Bearer authentication resolves to the user, with the token's scopes.
    assert me.status_code == 200
    assert me.json() == {
        "id": user_id,
        "display_name": me.json()["display_name"],
        "roles": ["Collaborator"],
        "permissions": ["edit_draft"],
    }
    assert whoami.json() == {"id": user_id, "via": "token"}
    assert drafts.status_code == 200
    assert publish.status_code == 403
    assert publish.json()["code"] == "LAE-AUTH-001"


async def test_scopes_are_intersected_with_the_users_current_permissions(
    db_url: str, db_engine: AsyncEngine
) -> None:
    user_id = await create_user(db_engine, "Editor")
    token, _ = await create_token(db_engine, user_id, "edit_draft", "publish")
    narrow, _ = await create_token(db_engine, user_id, "edit_draft")
    async with serve_app(db_url) as served:
        before = (await served.client.get("/auth/me", headers=bearer(token))).json()
        narrow_me = (await served.client.get("/auth/me", headers=bearer(narrow))).json()
        await set_roles(db_engine, user_id, "Collaborator")
        after = (await served.client.get("/auth/me", headers=bearer(token))).json()
        publish = await served.client.post("/test/publish", headers=bearer(token))
    assert before["permissions"] == ["edit_draft", "publish"]
    assert narrow_me["permissions"] == ["edit_draft"]
    assert after["permissions"] == ["edit_draft"]
    assert publish.status_code == 403


async def test_a_token_cannot_be_given_scopes_the_caller_lacks(
    db_url: str, db_engine: AsyncEngine
) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        await session_user(served, idp, db_engine, "Collaborator")
        response = await served.client.post(
            "/api/tokens", json={"name": "x", "scopes": ["edit_draft", "publish", "merge"]}
        )
        listed = await served.client.get("/api/tokens")
    assert response.status_code == 403
    assert response.json() == {
        "code": "LAE-AUTH-004",
        "path": "/api/tokens",
        "params": {"scopes": ["merge", "publish"]},
    }
    assert listed.json() == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"name": "x", "scopes": ["publsh"]}, id="unknown-scope"),
        pytest.param({"name": "", "scopes": []}, id="empty-name"),
        pytest.param({"name": "x"}, id="no-scopes"),
        pytest.param(
            {"name": "x", "scopes": [], "expires_at": "2020-01-01T00:00:00Z"}, id="past-expiry"
        ),
        pytest.param(
            {"name": "x", "scopes": [], "expires_at": "2999-01-01T00:00:00"}, id="naive-expiry"
        ),
    ],
)
async def test_invalid_token_requests_are_422(
    db_url: str, db_engine: AsyncEngine, body: dict[str, Any]
) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        await session_user(served, idp, db_engine, "Editor")
        response = await served.client.post("/api/tokens", json=body)
    assert response.status_code == 422


async def test_a_token_with_no_scopes_only_authenticates(
    db_url: str, db_engine: AsyncEngine
) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        await session_user(served, idp, db_engine, "Editor")
        created = await served.client.post("/api/tokens", json={"name": "read", "scopes": []})
        token = created.json()["token"]
        async with served.new_client(headers=bearer(token)) as script:
            me = await script.get("/auth/me")
            drafts = await script.post("/test/drafts")
    assert created.status_code == 201
    assert me.json()["permissions"] == []
    assert drafts.status_code == 403


async def test_tokens_cannot_create_tokens(db_url: str, db_engine: AsyncEngine) -> None:
    user_id = await create_user(db_engine, "Editor")
    token, _ = await create_token(db_engine, user_id, "edit_draft")
    async with serve_app(db_url) as served:
        response = await served.client.post(
            "/api/tokens", json={"name": "child", "scopes": []}, headers=bearer(token)
        )
    assert response.status_code == 403
    assert response.json()["code"] == "LAE-AUTH-005"


async def test_anonymous_cannot_create_tokens(db_url: str) -> None:
    async with serve_app(db_url) as served:
        response = await served.client.post("/api/tokens", json={"name": "x", "scopes": []})
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-002"


def assert_invalid_token(response: Any) -> None:
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-003"
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


async def test_expired_token_is_rejected(db_url: str, db_engine: AsyncEngine) -> None:
    user_id = await create_user(db_engine, "Editor")
    expired, _ = await create_token(
        db_engine, user_id, "edit_draft", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    valid, _ = await create_token(
        db_engine, user_id, "edit_draft", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    async with serve_app(db_url) as served:
        rejected = await served.client.get("/auth/me", headers=bearer(expired))
        accepted = await served.client.get("/auth/me", headers=bearer(valid))
    assert_invalid_token(rejected)
    assert accepted.status_code == 200


@pytest.mark.parametrize("token", ["lae_pat_unknown", "", "x" * 300])
async def test_unknown_tokens_are_rejected(db_url: str, token: str) -> None:
    async with serve_app(db_url) as served:
        response = await served.client.get("/auth/me", headers=bearer(token))
    assert_invalid_token(response)


async def test_other_authorization_schemes_are_ignored(db_url: str) -> None:
    async with serve_app(db_url) as served:
        response = await served.client.get("/auth/me", headers={"Authorization": "Basic eDp5"})
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-002"


async def test_an_invalid_token_does_not_fall_back_to_the_session(
    db_url: str, db_engine: AsyncEngine
) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        await session_user(served, idp, db_engine, "Editor")
        with_session = await served.client.get("/auth/me")
        with_bad_token = await served.client.get("/auth/me", headers=bearer("lae_pat_nope"))
    assert with_session.status_code == 200
    assert_invalid_token(with_bad_token)


async def test_deleted_tokens_stop_working(db_url: str, db_engine: AsyncEngine) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        await session_user(served, idp, db_engine, "Editor")
        created = (await served.client.post("/api/tokens", json={"name": "x", "scopes": []})).json()
        assert (await served.client.get("/auth/me", headers=bearer(created["token"]))).is_success
        deleted = await served.client.delete(f"/api/tokens/{created['id']}")
        after = await served.client.get("/auth/me", headers=bearer(created["token"]))
        again = await served.client.delete(f"/api/tokens/{created['id']}")
    assert deleted.status_code == 204
    assert_invalid_token(after)
    assert again.status_code == 404
    assert again.json() == {
        "code": "LAE-AUTH-010",
        "path": f"/api/tokens/{created['id']}",
        "params": {"id": created["id"]},
    }


async def test_users_see_and_delete_only_their_own_tokens(
    db_url: str, db_engine: AsyncEngine
) -> None:
    other_user = await create_user(db_engine, "Editor")
    other_token, other_id = await create_token(db_engine, other_user, "edit_draft")
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        await session_user(served, idp, db_engine, "Editor")
        listed = await served.client.get("/api/tokens")
        deleted = await served.client.delete(f"/api/tokens/{other_id}")
        still_works = await served.client.get("/auth/me", headers=bearer(other_token))
    assert listed.json() == []
    assert deleted.status_code == 404
    assert still_works.status_code == 200


async def test_tokens_of_deactivated_users_are_rejected(
    db_url: str, db_engine: AsyncEngine
) -> None:
    user_id = await create_user(db_engine, "Editor")
    token, _ = await create_token(db_engine, user_id, "edit_draft")
    async with serve_app(db_url) as served:
        before = await served.client.get("/auth/me", headers=bearer(token))
        await set_active(db_engine, user_id, False)
        after = await served.client.get("/auth/me", headers=bearer(token))
    assert before.status_code == 200
    assert_invalid_token(after)


async def test_last_used_at_is_written_at_most_once_a_minute(
    db_url: str, db_engine: AsyncEngine
) -> None:
    user_id = await create_user(db_engine, "Editor")
    token, token_id = await create_token(db_engine, user_id, "edit_draft")
    async with serve_app(db_url) as served:
        assert (await stored_token(db_engine, token_id)).last_used_at is None
        await served.client.get("/auth/me", headers=bearer(token))
        first = (await stored_token(db_engine, token_id)).last_used_at
        await served.client.get("/auth/me", headers=bearer(token))
        second = (await stored_token(db_engine, token_id)).last_used_at

        two_minutes_ago = datetime.now(UTC) - timedelta(minutes=2)
        async with AsyncSession(db_engine) as session:
            await session.execute(
                update(ApiToken).where(ApiToken.id == token_id).values(last_used_at=two_minutes_ago)
            )
            await session.commit()
        await served.client.get("/auth/me", headers=bearer(token))
        third = (await stored_token(db_engine, token_id)).last_used_at
    assert first is not None
    assert second == first
    assert third is not None
    assert third > two_minutes_ago + timedelta(minutes=1)


async def test_token_digests_are_compared_in_constant_time(
    db_url: str, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    compare_digest = hmac.compare_digest

    def spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return compare_digest(a, b)

    monkeypatch.setattr(lae.auth.tokens.hmac, "compare_digest", spy)
    user_id = await create_user(db_engine, "Editor")
    token, _ = await create_token(db_engine, user_id)
    async with serve_app(db_url) as served:
        response = await served.client.get("/auth/me", headers=bearer(token))
    assert response.status_code == 200
    assert calls == [(hash_token(token), hash_token(token))]


async def test_token_rows_never_hold_the_plaintext(db_url: str, db_engine: AsyncEngine) -> None:
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp) as served:
        user_id = await session_user(served, idp, db_engine, "Editor")
        token = (await served.client.post("/api/tokens", json={"name": "x", "scopes": []})).json()
    async with AsyncSession(db_engine) as session:
        rows = (await session.scalars(select(ApiToken).where(ApiToken.user_id == user_id))).all()
    assert len(rows) == 1
    assert token["token"] not in repr(
        {column: getattr(rows[0], column) for column in ApiToken.__table__.columns.keys()}
    )
