"""The ``crdt-ws`` application under uvicorn, with real WebSocket clients and the RPC client."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from pycrdt import create_awareness_message, create_update_message
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from lae.auth.setup import AuthConfigError
from lae.crdt import errors
from lae.crdt.client import CrdtWsClient, CrdtWsError, CrdtWsUnavailableError
from lae.crdt.protocol import encode_awareness
from lae.crdt.server import (
    RedactTokens,
    create_crdt_app,
    main,
    redact_tokens_in_server_logs,
)
from lae.projection import ops, project
from tests.auth.conftest import create_token, create_user, set_active
from tests.crdt.helpers import (
    INTERNAL_TOKEN,
    YClient,
    change,
    crdt_settings,
    create_record,
    draft_row,
    eventually,
    free_port,
    record_epoch,
    replica_of,
    running_server,
    session_cookie,
    update_count,
)

ERRORS_JSON = Path(__file__).resolve().parents[3] / "frontend/public/locales/en/errors.json"


def add_name(content: str) -> Any:
    return lambda doc: ops.add_node(doc, "identified_by", {"type": "Name", "content": content})


def names(jsonld: dict[str, Any]) -> list[str]:
    return [name["content"] for name in jsonld["identified_by"]]


async def user_with_token(
    engine: AsyncEngine, role: str, name: str = "Test User"
) -> tuple[UUID, str]:
    user_id = await create_user(engine, role, display_name=name)
    token, _ = await create_token(engine, user_id, "edit_draft", "publish")
    return user_id, token


def rpc(server: Any, http: httpx.AsyncClient, token: str | None = INTERNAL_TOKEN) -> CrdtWsClient:
    return CrdtWsClient(http, server.http_url, token)


async def test_two_clients_converge_and_the_server_agrees(
    db_url: str, db_engine: AsyncEngine
) -> None:
    record_id, _ = await create_record(db_engine)
    alice_id, alice_token = await user_with_token(db_engine, "Editor", "Alice")
    bob_id, bob_token = await user_with_token(db_engine, "Collaborator", "Bob")
    async with running_server(crdt_settings(db_url)) as server:
        async with (
            YClient(server.ws_url(record_id, token=alice_token, epoch=0)) as alice,
            YClient(server.ws_url(record_id, token=bob_token, epoch=0)) as bob,
        ):
            await alice.wait_synced()
            await bob.wait_synced()
            assert names(alice.project()) == names(bob.project()) == ["First name"]

            # Concurrent edits: both add names and type into the same Text.
            for number in range(5):
                add_name(f"Alice {number}")(alice.doc)
                add_name(f"Bob {number}")(bob.doc)
                ops.splice_text(alice.doc, "referred_to_by.content", 0, 0, f"a{number}")
                ops.splice_text(bob.doc, "referred_to_by.content", 0, 0, f"b{number}")
                await asyncio.sleep(0.01)

            async def converged() -> bool:
                server_state = (await server.manager.snapshot(record_id)).jsonld
                return alice.project() == bob.project() == server_state

            await eventually(converged, what="alice == bob == server")
            final = alice.project()
            assert sorted(names(final)) == sorted(
                ["First name"] + [f"{who} {n}" for who in ("Alice", "Bob") for n in range(5)]
            )
            statement = final["referred_to_by"][0]["content"]
            assert statement.endswith("A statement") and len(statement) == len("A statement") + 20
            snapshot = await server.manager.snapshot(record_id)
            assert set(snapshot.contributors) == {alice_id, bob_id}
        # Everything a peer saw is in the database (persist before broadcast).
        assert await update_count(db_engine, record_id) == 20


async def test_stale_epoch_is_closed_with_4409(db_url: str, db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine, draft_epoch=3)
    _, token = await user_with_token(db_engine, "Editor")
    async with running_server(crdt_settings(db_url)) as server:
        for stale in (2, 4, 0):
            client = YClient(server.ws_url(record_id, token=token, epoch=stale))
            await client.connect()
            assert await client.wait_closed() == (4409, "stale-epoch")
            assert not client.synced.is_set()
            await client.close()
        # The header works as well as the query parameter.
        by_header = YClient(server.ws_url(record_id, token=token), headers={"X-Draft-Epoch": "2"})
        await by_header.connect()
        assert await by_header.wait_closed() == (4409, "stale-epoch")
        await by_header.close()
        async with YClient(
            server.ws_url(record_id, token=token), headers={"X-Draft-Epoch": "3"}
        ) as current:
            await current.wait_synced()
            assert current.close_code is None


async def test_offline_edits_against_a_published_draft_are_not_replayed(
    db_url: str, db_engine: AsyncEngine
) -> None:
    """Decision B6 end to end: edit offline, someone discards/publishes, reconnect → 4409."""
    record_id, _ = await create_record(db_engine)
    _, token = await user_with_token(db_engine, "Editor")
    async with running_server(crdt_settings(db_url)) as server, httpx.AsyncClient() as http:
        client = YClient(server.ws_url(record_id, token=token, epoch=0))
        await client.connect()
        await client.wait_synced()
        add_name("Online edit")(client.doc)
        await eventually(lambda: update_count(db_engine, record_id), what="edit persisted")
        await client.close()
        add_name("Offline edit")(client.doc)  # made while disconnected

        await rpc(server, http).close(record_id, discard=True)  # the draft ends; epoch 0 → 1
        assert await record_epoch(db_engine, record_id) == 1

        await client.connect()  # still says epoch 0
        assert await client.wait_closed() == (4409, "stale-epoch")
        await client.close()
        assert await draft_row(db_engine, record_id) is not None  # the new draft, hydrated fresh
        assert names((await server.manager.snapshot(record_id)).jsonld) == ["First name"]
        assert "Offline edit" in names(client.project())  # the client keeps its document

        async with YClient(server.ws_url(record_id, token=token, epoch=1)) as fresh:
            await fresh.wait_synced()
            assert names(fresh.project()) == ["First name"]


async def test_discard_while_connected_closes_sockets_with_4409(
    db_url: str, db_engine: AsyncEngine
) -> None:
    record_id, _ = await create_record(db_engine)
    _, token = await user_with_token(db_engine, "Editor")
    async with running_server(crdt_settings(db_url)) as server, httpx.AsyncClient() as http:
        async with (
            YClient(server.ws_url(record_id, token=token, epoch=0)) as one,
            YClient(server.ws_url(record_id, token=token, epoch=0)) as two,
        ):
            await one.wait_synced()
            await two.wait_synced()
            await rpc(server, http).close(record_id, discard=True)
            assert await one.wait_closed() == (4409, "stale-epoch")
            assert await two.wait_closed() == (4409, "stale-epoch")
        assert await draft_row(db_engine, record_id) is None
        assert await rpc(server, http).list_rooms() == []


async def test_connection_refusals(db_url: str, db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    user_id, token = await user_with_token(db_engine, "Editor")
    async with running_server(crdt_settings(db_url)) as server:

        async def refused(url: str, **kwargs: Any) -> tuple[int | None, str | None]:
            client = YClient(url, **kwargs)
            await client.connect()
            try:
                return await client.wait_closed()
            finally:
                await client.close()

        assert await refused(server.ws_url(record_id, epoch=0)) == (4401, "unauthenticated")
        assert await refused(server.ws_url(record_id, token="lae_pat_wrong", epoch=0)) == (
            4401,
            "unauthenticated",
        )
        # A bad token does not fall back to a valid cookie.
        cookie = {"Cookie": f"lae_session={session_cookie(user_id)}"}
        assert await refused(
            server.ws_url(record_id, token="lae_pat_wrong", epoch=0), headers=cookie
        ) == (4401, "unauthenticated")
        assert await refused(server.ws_url(record_id, token=token)) == (4400, "bad-request")
        assert await refused(server.ws_url(record_id, token=token, epoch="x")) == (
            4400,
            "bad-request",
        )
        assert await refused(server.ws_url("not-a-uuid", token=token, epoch=0)) == (
            4400,
            "bad-request",
        )
        assert await refused(server.ws_url(uuid7(), token=token, epoch=0)) == (4404, "not-found")
        await set_active(db_engine, user_id, False)
        assert await refused(server.ws_url(record_id, token=token, epoch=0)) == (
            4401,
            "unauthenticated",
        )
        assert await draft_row(db_engine, record_id) is None  # nobody got far enough to open it


async def test_session_cookie_and_bearer_header_authenticate(
    db_url: str, db_engine: AsyncEngine
) -> None:
    record_id, _ = await create_record(db_engine)
    user_id, token = await user_with_token(db_engine, "Editor", "Cookie User")
    # The cookie is Secure outside development, but a WebSocket client sends what it is given.
    async with running_server(crdt_settings(db_url)) as server:
        cookie = {"Cookie": f"lae_session={session_cookie(user_id)}"}
        async with YClient(server.ws_url(record_id, epoch=0), headers=cookie) as by_cookie:
            await by_cookie.wait_synced()
            add_name("By cookie")(by_cookie.doc)
            await eventually(lambda: update_count(db_engine, record_id), what="persisted")
        bearer = {"Authorization": f"Bearer {token}"}
        async with YClient(server.ws_url(record_id, epoch=0), headers=bearer) as by_header:
            await by_header.wait_synced()
            assert "By cookie" in names(by_header.project())
        forged = {"Cookie": f"lae_session={session_cookie(user_id, secret='other-secret')}"}
        client = YClient(server.ws_url(record_id, epoch=0), headers=forged)
        await client.connect()
        assert await client.wait_closed() == (4401, "unauthenticated")
        await client.close()
        assert (await server.manager.snapshot(record_id)).contributors == [user_id]


async def test_dev_user_authenticates_without_credentials(
    db_url: str, db_engine: AsyncEngine
) -> None:
    record_id, _ = await create_record(db_engine)
    settings = crdt_settings(db_url, env="development", dev_user="crdt-dev@example.org:Editor")
    async with running_server(settings) as server:
        async with YClient(server.ws_url(record_id, epoch=0)) as client:
            await client.wait_synced()
            assert names(client.project()) == ["First name"]


async def test_viewer_is_read_only(db_url: str, db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    no_draft_id, _ = await create_record(db_engine)
    _, editor_token = await user_with_token(db_engine, "Editor", "Edith")
    viewer_id, viewer_token = await user_with_token(db_engine, "Viewer", "Victor")
    # A token scoped without edit_draft is read-only too, whatever the user's role.
    editor_id = await create_user(db_engine, "Editor")
    unscoped_token, _ = await create_token(db_engine, editor_id, "publish")
    async with running_server(crdt_settings(db_url)) as server:
        # Without a draft there is nothing to watch, and a Viewer must not create one.
        lonely = YClient(server.ws_url(no_draft_id, token=viewer_token, epoch=0))
        await lonely.connect()
        assert await lonely.wait_closed() == (4404, "not-found")
        await lonely.close()
        assert await draft_row(db_engine, no_draft_id) is None

        async with (
            YClient(server.ws_url(record_id, token=editor_token, epoch=0)) as editor,
            YClient(server.ws_url(record_id, token=viewer_token, epoch=0)) as viewer,
            YClient(server.ws_url(record_id, token=unscoped_token, epoch=0)) as unscoped,
        ):
            await editor.wait_synced()
            await viewer.wait_synced()
            await unscoped.wait_synced()
            assert names(viewer.project()) == ["First name"]  # a Viewer reads the draft

            add_name("By the editor")(editor.doc)
            await eventually(
                lambda: "By the editor" in names(viewer.project()), what="viewer sees edit"
            )

            add_name("By the viewer")(viewer.doc)
            add_name("By the unscoped token")(unscoped.doc)
            add_name("Editor again")(editor.doc)  # a marker that arrives after the dropped ones
            await eventually(lambda: "Editor again" in names(viewer.project()), what="marker")
            server_names = names((await server.manager.snapshot(record_id)).jsonld)
            assert server_names == ["First name", "By the editor", "Editor again"]
            assert names(editor.project()) == server_names
            assert await update_count(db_engine, record_id) == 2
            assert (await server.manager.snapshot(record_id)).contributors != [viewer_id]

            # The awareness flag: the server's state lists the read-only users.
            def server_state(client: YClient) -> dict[str, Any]:
                states = [s for s in client.presence.values() if s.get("kind") == "server"]
                return states[-1] if states else {}

            await eventually(
                lambda: (
                    sorted(server_state(viewer).get("read_only", []))
                    == sorted([str(viewer_id), str(editor_id)])
                ),
                what="read_only flag at the viewer",
            )
            assert server_state(editor)["draft_epoch"] == 0
            # ... and a read-only user's own presence is stamped read_only for its peers.
            await viewer.set_presence({"cursor": 1})
            await eventually(
                lambda: any(s.get("read_only") is True for s in editor.presence.values()),
                what="viewer presence at the editor",
            )


async def test_presence_is_stamped_sent_on_join_and_removed_on_drop(
    db_url: str, db_engine: AsyncEngine
) -> None:
    record_id, _ = await create_record(db_engine)
    alice_id, alice_token = await user_with_token(db_engine, "Editor", "Alice")
    bob_id, bob_token = await user_with_token(db_engine, "Collaborator", "Bob")
    async with running_server(crdt_settings(db_url)) as server:
        alice = YClient(server.ws_url(record_id, token=alice_token, epoch=0))
        await alice.connect()
        await alice.wait_synced()
        # Alice claims to be Bob; the server overwrites the identity fields.
        await alice.set_presence(
            {"user_id": str(bob_id), "display_name": "Bob", "kind": "agent", "cursor": "name-1"}
        )
        await eventually(lambda: alice.doc.client_id in alice.presence, what="echo to the sender")

        async with YClient(server.ws_url(record_id, token=bob_token, epoch=0)) as bob:
            # S2 finding 4: a joiner sees idle peers at once, not at their 15 s renewal.
            await eventually(
                lambda: alice.doc.client_id in bob.presence, seconds=2, what="presence on join"
            )
            assert bob.presence[alice.doc.client_id] == {
                "user_id": str(alice_id),
                "display_name": "Alice",
                "kind": "human",
                "read_only": False,
                "cursor": "name-1",
            }
            await bob.set_presence({"cursor": "name-2"})
            await eventually(lambda: bob.doc.client_id in alice.presence, what="bob at alice")
            assert alice.presence[bob.doc.client_id]["user_id"] == str(bob_id)

            # Bob cannot overwrite or remove Alice's state (he relays it, as y-websocket does).
            forged = [
                (alice.doc.client_id, 99, {"cursor": "forged"}),
                (alice.doc.client_id, 100, None),
            ]
            for entry in forged:
                await bob.send_raw(create_awareness_message(encode_awareness([entry])))
            await bob.set_presence({"cursor": "name-3"})
            await eventually(
                lambda: alice.presence.get(bob.doc.client_id, {}).get("cursor") == "name-3",
                what="bob's next state",
            )
            assert alice.presence[alice.doc.client_id]["cursor"] == "name-1"
            assert alice.doc.client_id not in alice.removed

            # S2 finding 5: an abrupt drop removes the presence at once (not after 30 s).
            await alice.abort()
            await eventually(
                lambda: alice.doc.client_id in bob.removed, seconds=3, what="ghost removed"
            )
            assert alice.doc.client_id not in bob.presence


async def test_eject_closes_a_live_socket_with_4403(db_url: str, db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    user_id, token = await user_with_token(db_engine, "Editor")
    async with running_server(crdt_settings(db_url)) as server, httpx.AsyncClient() as http:
        async with YClient(server.ws_url(record_id, token=token, epoch=0)) as client:
            await client.wait_synced()
            assert await rpc(server, http).eject(uuid7()) == 0
            assert await rpc(server, http).eject(user_id) == 1
            assert await client.wait_closed() == (4403, "ejected")


async def test_garbage_from_a_client_closes_it_with_4400(
    db_url: str, db_engine: AsyncEngine
) -> None:
    record_id, _ = await create_record(db_engine)
    _, token = await user_with_token(db_engine, "Editor")
    async with running_server(crdt_settings(db_url)) as server:
        async with (
            YClient(server.ws_url(record_id, token=token, epoch=0)) as good,
            YClient(server.ws_url(record_id, token=token, epoch=0)) as bad,
        ):
            await good.wait_synced()
            await bad.wait_synced()
            await bad.send_raw(create_update_message(b"this is not a yjs update"))
            assert await bad.wait_closed() == (4400, "protocol-error")
            add_name("Still working")(good.doc)
            await eventually(
                lambda: update_count(db_engine, record_id), what="good client persists"
            )
            assert good.close_code is None
        assert await update_count(db_engine, record_id) == 1


async def test_server_restart_rehydrates_and_clients_resync(
    db_url: str, db_engine: AsyncEngine
) -> None:
    record_id, _ = await create_record(db_engine)
    _, token = await user_with_token(db_engine, "Editor")
    port = free_port()
    settings = crdt_settings(db_url)
    client = YClient(f"ws://127.0.0.1:{port}/ws/rooms/{record_id}?token={token}&epoch=0")
    async with running_server(settings, port=port):
        await client.connect()
        await client.wait_synced()
        add_name("Before restart")(client.doc)
        await eventually(lambda: update_count(db_engine, record_id), what="persisted")
        # uvicorn closes the socket itself on shutdown (1012 from the app, or 1001/1006).
    code, _ = await client.wait_closed(15)
    assert code in (1012, 1001, 1006, 1000)
    await client.close()
    add_name("While the server was down")(client.doc)

    async with running_server(settings, port=port) as server:
        assert await server.manager.list_rooms() == []
        await client.connect()  # same document, same epoch: an ordinary resync
        await client.wait_synced()
        expected = ["First name", "Before restart", "While the server was down"]
        await eventually(
            lambda: update_count(db_engine, record_id), what="offline edit persisted on reconnect"
        )

        async def server_has_all() -> bool:
            return names((await server.manager.snapshot(record_id)).jsonld) == expected

        await eventually(server_has_all, what="server has both edits")
        assert names(client.project()) == expected
        await client.close()


async def test_internal_rpc_through_the_client(db_url: str, db_engine: AsyncEngine) -> None:
    record_id, uri = await create_record(db_engine, version=2, draft_epoch=1)
    other_id, _ = await create_record(db_engine)
    user_id = await create_user(db_engine, "Editor")
    async with running_server(crdt_settings(db_url)) as server, httpx.AsyncClient() as http:
        crdt = rpc(server, http)
        assert await crdt.list_rooms() == []
        assert await crdt.snapshot(record_id) is None  # no draft; and none is created
        assert await draft_row(db_engine, record_id) is None

        opened = await crdt.open(record_id, created_by=user_id)
        assert (opened.record_id, opened.draft_epoch, opened.base_version) == (record_id, 1, 2)
        assert await crdt.open(record_id) == opened  # idempotent
        row = await draft_row(db_engine, record_id)
        assert row is not None and row.created_by == user_id

        snapshot = await crdt.snapshot(record_id)
        assert snapshot is not None
        assert snapshot.jsonld["id"] == uri and snapshot.jsonld["identified_by"][0]["id"]
        assert (snapshot.draft_epoch, snapshot.base_version, snapshot.contributors) == (1, 2, [])

        room = await server.manager.get(record_id)
        assert room is not None
        replica = replica_of(room.ydoc)
        await crdt.apply_update(record_id, change(replica, add_name("Via RPC")), user_id)
        snapshot = await crdt.snapshot(record_id)
        assert snapshot is not None
        assert names(snapshot.jsonld) == ["First name", "Via RPC"]
        assert snapshot.contributors == [user_id] and snapshot.last_update_id > 0
        assert snapshot.jsonld == project(replica)

        rooms = await crdt.list_rooms()
        assert [(r.record_id, r.draft_epoch, r.connections) for r in rooms] == [(record_id, 1, 0)]

        await crdt.close(record_id, discard=False)
        assert await crdt.list_rooms() == []
        snapshot = await crdt.snapshot(record_id)  # rehydrated from the draft row
        assert snapshot is not None and names(snapshot.jsonld) == ["First name", "Via RPC"]

        await crdt.close(record_id, discard=True)
        assert await crdt.snapshot(record_id) is None
        assert await record_epoch(db_engine, record_id) == 2

        # open with content (revert-to-revision), then the conflicts and failures.
        content = dict(snapshot.jsonld, _label="Reverted")
        opened = await crdt.open(other_id, content=content)
        reverted = await crdt.snapshot(other_id)
        assert reverted is not None and reverted.jsonld["_label"] == "Reverted"
        with pytest.raises(CrdtWsError) as conflict:
            await crdt.open(other_id, content=content)
        assert (conflict.value.status_code, conflict.value.code) == (409, errors.DRAFT_EXISTS)
        with pytest.raises(CrdtWsError) as missing:
            await crdt.open(uuid7())
        assert (missing.value.status_code, missing.value.code) == (404, errors.RECORD_NOT_FOUND)
        with pytest.raises(CrdtWsError) as missing:
            await crdt.snapshot(uuid7())
        assert missing.value.code == errors.RECORD_NOT_FOUND
        with pytest.raises(CrdtWsError) as invalid:
            await crdt.apply_update(other_id, b"not an update", None)
        assert (invalid.value.status_code, invalid.value.code) == (422, errors.INVALID_UPDATE)
        await crdt.close(record_id, discard=True)
        with pytest.raises(CrdtWsError) as bad_content:
            await crdt.open(record_id, content={"id": uri, "type": "Person", "_order": 1})
        assert (bad_content.value.status_code, bad_content.value.code) == (
            422,
            errors.INVALID_CONTENT,
        )
        assert bad_content.value.params["projection_code"] == "LAE-PROJ-001"
        response = await http.post(
            f"{server.http_url}/internal/rooms/{other_id}/apply",
            json={"update": "!!! not base64 !!!", "user_id": None},
            headers={"X-Internal-Token": INTERNAL_TOKEN},
        )
        assert response.status_code == 422
        assert response.json()["code"] == errors.INVALID_UPDATE


async def test_internal_rpc_requires_the_token(db_url: str, db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine)
    calls = [
        ("GET", "/internal/rooms", None),
        ("GET", f"/internal/rooms/{record_id}/snapshot", None),
        ("POST", f"/internal/rooms/{record_id}/open", {}),
        ("POST", f"/internal/rooms/{record_id}/close", {"discard": True}),
        ("POST", f"/internal/rooms/{record_id}/apply", {"update": "AAA=", "user_id": None}),
        ("POST", f"/internal/users/{record_id}/eject", None),
    ]
    async with running_server(crdt_settings(db_url)) as server, httpx.AsyncClient() as http:
        for headers in ({}, {"X-Internal-Token": "wrong"}, {"X-Internal-Token": ""}):
            for method, path, body in calls:
                response = await http.request(
                    method, server.http_url + path, json=body, headers=headers
                )
                assert response.status_code == 401, (method, path, headers)
                assert response.json() == {
                    "code": errors.INTERNAL_TOKEN_REQUIRED,
                    "path": path,
                    "params": {},
                }
        assert await draft_row(db_engine, record_id) is None
        # Through the client a refused token is a deployment fault: 502, not the user's 401.
        with pytest.raises(CrdtWsError) as refused:
            await rpc(server, http, "wrong").list_rooms()
        assert (refused.value.status_code, refused.value.code) == (
            502,
            errors.INTERNAL_TOKEN_REQUIRED,
        )
        assert (await http.get(server.http_url + "/health/live")).json() == {"status": "ok"}

    # A server without LAE_INTERNAL_TOKEN refuses every internal call, even one with no token.
    async with running_server(crdt_settings(db_url, internal_token=None)) as server:
        async with httpx.AsyncClient() as http:
            for token in (None, "", "anything"):
                with pytest.raises(CrdtWsError):
                    await rpc(server, http, token).list_rooms()


async def test_client_reports_an_unreachable_server() -> None:
    async with httpx.AsyncClient() as http:
        crdt = CrdtWsClient(http, f"http://127.0.0.1:{free_port()}", INTERNAL_TOKEN)
        with pytest.raises(CrdtWsUnavailableError) as unavailable:
            await crdt.snapshot(uuid7())
        assert unavailable.value.status_code == 503
        assert unavailable.value.code == errors.CRDT_WS_UNAVAILABLE


def test_client_from_settings_uses_the_configured_url_and_token() -> None:
    settings = crdt_settings("postgresql+asyncpg://localhost/unused", crdt_ws_url="http://crdt:9/")
    client = CrdtWsClient.from_settings(settings, httpx.AsyncClient())
    assert client._base_url == "http://crdt:9"
    assert client._headers == {"X-Internal-Token": INTERNAL_TOKEN}


def test_every_crdt_code_has_an_english_message() -> None:
    messages = json.loads(ERRORS_JSON.read_text())
    assert {code for code in messages if code.startswith("LAE-CRDT-")} == set(errors.CRDT_CODES)
    assert len(set(errors.CRDT_CODES)) == len(errors.CRDT_CODES)


def test_main_refuses_unsafe_settings(capsys: pytest.CaptureFixture[str]) -> None:
    url = "postgresql+asyncpg://localhost/unused"
    assert main(crdt_settings(url, env="production", internal_token=None)) == 2
    assert "LAE_INTERNAL_TOKEN is required" in capsys.readouterr().err
    assert main(crdt_settings(url, env="production", dev_user="a@example.org:Editor")) == 2
    assert "LAE_DEV_USER" in capsys.readouterr().err
    with pytest.raises(AuthConfigError):
        create_crdt_app(crdt_settings(url, env="staging", session_secret=None))


def test_tokens_are_redacted_from_server_logs() -> None:
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        '%s - "WebSocket %s" [accepted]',
        ("127.0.0.1:1", "/ws/rooms/abc?token=lae_pat_SECRET-x_y&epoch=3"),
        None,
    )
    assert RedactTokens().filter(record)
    assert record.getMessage() == (
        '127.0.0.1:1 - "WebSocket /ws/rooms/abc?token=[redacted]&epoch=3" [accepted]'
    )
    plain = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1, "no %s", ("secret",), None
    )
    assert RedactTokens().filter(plain) and plain.getMessage() == "no secret"
    redact_tokens_in_server_logs()
    redact_tokens_in_server_logs()
    filters = logging.getLogger("uvicorn.error").filters
    assert sum(isinstance(f, RedactTokens) for f in filters) == 1
