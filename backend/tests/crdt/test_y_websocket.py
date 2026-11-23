"""Wire compatibility with the browser's libraries: Yjs + ``y-websocket`` against ``crdt-ws``.

``scripts/crdt/y_websocket_check.mjs`` runs the real ``WebsocketProvider`` in Node; this test
plays the server and a ``pycrdt`` peer. It needs ``node`` and ``frontend/node_modules`` (``make
setup``) and is skipped without them; the CI ``backend`` job installs them for this test.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.projection.project import record_map
from tests.auth.conftest import create_token, create_user
from tests.crdt.helpers import (
    YClient,
    crdt_settings,
    create_record,
    eventually,
    running_server,
    update_count,
)

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/crdt/y_websocket_check.mjs"
NODE = shutil.which("node")
HAVE_LIBRARIES = (REPO / "frontend/node_modules/y-websocket").exists()

pytestmark = pytest.mark.skipif(
    NODE is None or not HAVE_LIBRARIES,
    reason="needs node and frontend/node_modules (corepack pnpm install in frontend/)",
)


async def test_yjs_and_y_websocket_interoperate(db_url: str, db_engine: AsyncEngine) -> None:
    record_id, _ = await create_record(db_engine, draft_epoch=2)
    editor_id = await create_user(db_engine, "Editor", display_name="JS Editor")
    editor_token, _ = await create_token(db_engine, editor_id, "edit_draft")
    viewer_id = await create_user(db_engine, "Viewer", display_name="JS Viewer")
    viewer_token, _ = await create_token(db_engine, viewer_id)
    python_id = await create_user(db_engine, "Collaborator", display_name="Python Peer")
    python_token, _ = await create_token(db_engine, python_id, "edit_draft")

    async with running_server(crdt_settings(db_url)) as server:
        assert NODE is not None
        process = await asyncio.create_subprocess_exec(
            NODE,
            str(SCRIPT),
            "--url",
            f"ws://127.0.0.1:{server.port}/ws/rooms",
            "--record",
            str(record_id),
            "--epoch",
            "2",
            "--token",
            editor_token,
            "--viewer-token",
            viewer_token,
            "--viewer-id",
            str(viewer_id),
            "--label",
            "Test object",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with YClient(server.ws_url(record_id, token=python_token, epoch=2)) as peer:
                await peer.wait_synced()
                root = record_map(peer.doc)
                # Yjs writes first; pycrdt answers once it has seen the write.
                await eventually(
                    lambda: root.get("js_marker") == "from-yjs", seconds=15, what="JS edit"
                )
                # Captured now: the JS client removes its presence when it finishes.
                await eventually(
                    lambda: any(s.get("cursor") == "js" for s in peer.presence.values()),
                    what="JS presence",
                )
                js_presence = [s for s in peer.presence.values() if s.get("cursor") == "js"]
                root["py_marker"] = "from-pycrdt"
                await peer.set_presence({"cursor": "py"})
                stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
                python_view = peer.project()
        finally:
            if process.returncode is None:
                process.kill()
        try:
            result: dict[str, Any] = json.loads(stdout)
        except ValueError:
            pytest.fail(f"y_websocket_check.mjs printed no JSON:\n{stdout!r}\n{stderr.decode()}")
        assert result["failures"] == [], result
        assert all(result["checks"].values()), result["checks"]
        assert set(result["checks"]) == {
            "hydrated",
            "peer_edit",
            "peer_presence",
            "server_state",
            "viewer_synced",
            "viewer_flagged",
            "own_presence_stamped",
            "viewer_dropped",
            "stale_closed",
            "stale_terminal",
        }
        assert result["details"]["stale_close"] == {"code": 4409, "reason": "stale-epoch"}
        assert process.returncode == 0

        # What Yjs wrote is what pycrdt and the server read, projected to JSON-LD.
        snapshot = await server.manager.snapshot(record_id)
        assert snapshot.jsonld == python_view
        assert snapshot.jsonld["js_marker"] == "from-yjs"
        assert snapshot.jsonld["js_marker_2"] == "after-viewer"
        assert snapshot.jsonld["py_marker"] == "from-pycrdt"
        assert snapshot.jsonld["referred_to_by"][0]["content"] == "JS:A statement"
        assert "viewer_marker" not in snapshot.jsonld
        assert set(snapshot.contributors) == {editor_id, python_id}
        assert await update_count(db_engine, record_id) == 4
        # The JS client's forged identity was overwritten by the server.
        assert js_presence and js_presence[0]["user_id"] == str(editor_id)
        assert js_presence[0]["display_name"] == "JS Editor" and js_presence[0]["kind"] == "human"
