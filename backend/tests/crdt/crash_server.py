"""A real ``crdt-ws`` process that can kill itself between persisting and broadcasting.

Run by ``test_crash.py`` as ``python crash_server.py <port> <die-at>``. Settings come from
``LAE_*`` like the real process. When the ``die-at``-th update has been committed to
``crdt_updates`` and before it is applied to the ``Doc`` (so before any broadcast), the process
sends itself ``SIGKILL``: no handler runs, nothing is flushed, no socket is closed properly.
``die-at`` 0 never dies, which is the plain ``lae crdt-ws``.
"""

from __future__ import annotations

import os
import signal
import sys

import uvicorn

from lae.crdt.rooms import InMemoryRoomManager, Room
from lae.crdt.server import create_crdt_app
from lae.settings import Settings

DIE_AT = int(sys.argv[2])
persisted = 0


class DyingRoom(Room):
    """Dies in the hook that runs after the commit and before the apply."""

    async def _persisted(self, update_id: int) -> None:
        global persisted
        persisted += 1
        if persisted == DIE_AT:
            os.kill(os.getpid(), signal.SIGKILL)


class DyingManager(InMemoryRoomManager):
    room_class = DyingRoom


if __name__ == "__main__":
    app = create_crdt_app(Settings(), manager_class=DyingManager)
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_config=None)
