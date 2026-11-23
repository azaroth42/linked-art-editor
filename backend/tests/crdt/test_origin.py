"""The ``Origin`` allowlist of the WebSocket route (cross-site WebSocket hijacking)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from lae.crdt.server import allowed_origins, normalize_origin, origin_allowed
from tests.auth.conftest import create_user
from tests.crdt.helpers import (
    YClient,
    crdt_settings,
    create_record,
    draft_row,
    running_server,
    session_cookie,
)

DB = "postgresql+asyncpg://localhost/unused"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://editor.example.org", "https://editor.example.org"),
        ("HTTPS://Editor.Example.org:443", "https://editor.example.org"),
        ("http://editor.example.org:80/", "http://editor.example.org"),
        ("https://editor.example.org:8443", "https://editor.example.org:8443"),
        ("http://localhost:5173", "http://localhost:5173"),
        ("http://[::1]:5173", "http://[::1]:5173"),
        ("https://data.example.org/id/", "https://data.example.org"),
        ("null", None),
        ("", None),
        ("editor.example.org", None),
        ("file:///home/user/page.html", None),
        ("chrome-extension://abcdef", None),
        ("https://", None),
        ("https://editor.example.org:port", None),
    ],
)
def test_normalize_origin(value: str, expected: str | None) -> None:
    assert normalize_origin(value) == expected


def test_allowed_origins_are_the_base_uri_origin_and_the_configured_ones() -> None:
    settings = crdt_settings(
        DB,
        base_uri="https://data.example.org/id/",
        allowed_origins=["https://Editor.example.org", "http://intranet:8080/", "not an origin"],
    )
    assert allowed_origins(settings) == {
        "https://data.example.org",
        "https://editor.example.org",
        "http://intranet:8080",
    }
    assert origin_allowed(None, settings), "no Origin header: not a browser"
    assert origin_allowed("https://data.example.org", settings)
    assert origin_allowed("https://editor.example.org:443", settings)
    assert origin_allowed("http://intranet:8080", settings)
    for foreign in (
        "https://evil.example",
        "http://data.example.org",  # another scheme is another origin
        "https://data.example.org:8443",
        "https://data.example.org.evil.example",
        "https://sub.data.example.org",
        "http://localhost:5173",  # loopback is only trusted in development
        "null",
        "",
    ):
        assert not origin_allowed(foreign, settings), foreign


def test_loopback_origins_are_allowed_in_development_only() -> None:
    development = crdt_settings(DB, env="development", base_uri="https://data.example.org/id/")
    for local in ("http://localhost:5173", "http://127.0.0.1:9021", "http://[::1]:4173"):
        assert origin_allowed(local, development)
        for env in ("test", "staging", "production"):
            assert not origin_allowed(local, crdt_settings(DB, env=env, base_uri="https://x.org/"))
    assert not origin_allowed("https://evil.example", development)
    assert not origin_allowed("http://localhost.evil.example", development)


async def test_a_page_on_another_origin_cannot_open_a_room_with_the_users_cookie(
    db_url: str, db_engine: AsyncEngine
) -> None:
    """Cross-site WebSocket hijacking: the browser attaches the cookie, the server refuses."""
    record_id, _ = await create_record(db_engine)
    user_id = await create_user(db_engine, "Editor")
    cookie = f"lae_session={session_cookie(user_id)}"
    settings = crdt_settings(
        db_url,
        base_uri="https://data.example.org/id/",
        allowed_origins=["https://editor.example.org"],
    )
    async with running_server(settings) as server:
        url = server.ws_url(record_id, epoch=0)
        for foreign in ("https://evil.example", "null", "http://localhost:5173"):
            with pytest.raises(InvalidStatus) as refused:
                async with connect(url, additional_headers={"Cookie": cookie, "Origin": foreign}):
                    pytest.fail(f"a socket was opened for origin {foreign}")
            assert refused.value.response.status_code == 403
        assert await draft_row(db_engine, record_id) is None, "nothing was opened"
        assert await server.manager.list_rooms() == []

        # The editor's own origins, and a client that is not a browser, get in.
        for origin in ("https://editor.example.org", "https://data.example.org", None):
            headers = {"Cookie": cookie} | ({} if origin is None else {"Origin": origin})
            async with YClient(url, headers=headers) as client:
                await client.wait_synced()
                assert client.close_code is None


async def test_the_dev_user_bypass_is_not_open_to_other_sites(
    db_url: str, db_engine: AsyncEngine
) -> None:
    """With ``LAE_DEV_USER`` nobody needs a cookie, so the origin check is the only barrier."""
    record_id, _ = await create_record(db_engine)
    settings = crdt_settings(db_url, env="development", dev_user="dev-origin@example.org:Editor")
    async with running_server(settings) as server:
        url = server.ws_url(record_id, epoch=0)
        with pytest.raises(InvalidStatus) as refused:
            async with connect(url, additional_headers={"Origin": "https://evil.example"}):
                pytest.fail("a foreign page used the development login")
        assert refused.value.response.status_code == 403
        async with YClient(url, headers={"Origin": "http://localhost:5173"}) as vite:
            await vite.wait_synced()
