"""Rooms: the one live draft ``Doc`` per record, its persistence, and the ``RoomManager``.

``RoomManager`` is the narrow interface of decision B1: everything that reads or edits a live
draft goes through it, so a Yjs-wire-compatible sidecar could replace the implementation.
``InMemoryRoomManager`` implements it with ``pycrdt`` and runs only inside the single ``crdt-ws``
process (decision A4, ADR-0011); other processes call it through ``lae.crdt.client``.

Invariants (decisions B5 and B6, ADR-0027). A reviewer should be able to check each one against
the method named after it.

1. **One hydration per draft epoch** (``InMemoryRoomManager._load``). A draft is hydrated from
   ``records.data`` once, when its ``crdt_drafts`` row is inserted. Every later open of the
   same draft replays that row's ``state`` and the ``crdt_updates`` of the same epoch. The
   ``records`` row is locked ``FOR SHARE`` while that is decided, so a publish cannot commit
   between reading ``records.draft_epoch`` / ``data`` and reading (or inserting) the draft.
2. **Persist before apply, apply before broadcast** (``Room._write``). An update is validated,
   then committed to ``crdt_updates``, then applied to the ``Doc``; the broadcast is triggered
   by the ``Doc`` changing. Nothing a peer has seen can be missing from the database. The whole
   step holds the room lock and is shielded from cancellation, so compaction never runs between
   the commit and the apply. **Memory never falls behind the database either:** if the commit
   fails (its outcome is then unknown: the acknowledgement of a commit can be lost) or the
   apply fails, the room is dropped (``RoomFailedError``) and the next open rebuilds it from
   the rows. Otherwise a later compaction would delete a row the ``Doc`` does not contain.
3. **A superseded room writes nothing** (``lae.crdt.store.append_update`` / ``compact``). Both
   statements only match a ``crdt_drafts`` row with the room's epoch. When the row is gone or
   has another epoch (the draft was published or discarded while this room was in memory), the
   room is retired: every socket is closed with 4409 and the room is dropped without touching
   the database. ``open`` makes the same check before it returns a room that is in memory.
4. **Compaction loses nothing** (``Room._compact``). It runs under the room lock, so the
   ``Doc`` contains every committed update. The compacted state is ``Doc.get_update()`` merged
   with any update the ``Doc`` could not integrate yet (``lae.crdt.protocol.is_integrated``),
   because ``Doc.get_update()`` leaves those out.
5. **No room is dropped while it has sockets** (``InMemoryRoomManager._retire``; S2 finding 3).
   Sockets are closed with an explicit code first: 4409 ``stale-epoch`` when the draft is gone,
   1012 ``room-closed`` when the room only leaves memory (the client reconnects and the room
   rehydrates), 4403 ``ejected`` for ``eject``. Idle eviction only takes rooms with no socket.
6. **Deleting a draft ends its epoch** (``close(discard=True)`` → ``store.delete_draft``).
7. **One process** (decision A4; ``lae.crdt.instance``). Two managers on one database would each
   compact away rows the other one's ``Doc`` does not contain. ``crdt-ws`` holds a PostgreSQL
   advisory lock for as long as it runs and a second process does not start.

Presence (awareness). The server stamps ``user_id``, ``display_name``, ``kind`` (``"human"`` or
``"agent"``) and ``read_only`` on every state a socket publishes, so a client cannot pose as
someone else; states relayed for other clients are ignored. A joining socket receives every
current state at once, and the states a socket published are removed when it closes. The room's
own state is ``{"kind": "server", "draft_epoch", "base_version", "read_only": [user ids]}``:
a client whose user id is in ``read_only`` knows its edits are being dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from uuid import UUID

import pycrdt
from pycrdt import (
    Channel,
    Doc,
    YMessageType,
    YSyncMessageType,
    create_awareness_message,
    create_sync_message,
    handle_sync_message,
)
from pycrdt.websocket import YRoom
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lae.crdt import store
from lae.crdt.protocol import (
    EMPTY_UPDATE,
    AwarenessEntry,
    ProtocolError,
    check_update,
    decode_awareness,
    encode_awareness,
    frame_payload,
    is_integrated,
)
from lae.db.models import RecordStatus
from lae.logging import get_logger
from lae.projection import hydrate, project

COMPACT_EVERY: Final = 200
"""Updates appended to ``crdt_updates`` before they are folded into ``crdt_drafts.state`` (B5)."""

IDLE_SECONDS: Final = 600.0
"""Default time without a socket or a write after which a room leaves memory (B5)."""

SWEEP_SECONDS: Final = 30.0
"""Default interval of the idle-eviction sweep."""

DRAIN_SECONDS: Final = 5.0
"""How long a retiring room waits for its sockets to finish closing before it stops anyway."""

CLOSE_STALE_EPOCH: Final = 4409
"""WebSocket close code: the client's draft epoch is not the room's (decision B6)."""

REASON_STALE_EPOCH: Final = "stale-epoch"

CLOSE_ROOM_CLOSED: Final = 1012
"""WebSocket close code ("service restart"): the room left memory; reconnecting reopens it."""

REASON_ROOM_CLOSED: Final = "room-closed"

CLOSE_EJECTED: Final = 4403
"""WebSocket close code: the user was ejected (``RoomManager.eject``)."""

REASON_EJECTED: Final = "ejected"

CLOSE_PROTOCOL_ERROR: Final = 4400
"""WebSocket close code: the client sent something that is not y-sync or awareness.

In y-websocket 3.1 a close code of 4400-4499 is final (no reconnect); every other code makes
the provider reconnect. A client that sent garbage would send it again, so this one is final.
"""

REASON_PROTOCOL_ERROR: Final = "protocol-error"

CLOSE_INTERNAL_ERROR: Final = 1011
"""WebSocket close code: an update could not be persisted; the client reconnects and resends."""

REASON_PERSIST_FAILED: Final = "persist-failed"

REASON_INTERNAL_ERROR: Final = "internal-error"

logger = get_logger(__name__)
_yroom_log = logging.getLogger("lae.crdt.yroom")


class RoomError(Exception):
    """Base class of the errors a ``RoomManager`` raises."""


class RecordNotFoundError(RoomError):
    """There is no ``records`` row with this id."""


class DraftNotFoundError(RoomError):
    """The record has no draft (no room in memory and no ``crdt_drafts`` row)."""


class DraftExistsError(RoomError):
    """A draft already exists, so it cannot be created from the given content."""


class RecordNotEditableError(RoomError):
    """The record is deleted or merged; no draft can be opened for it."""


class InvalidUpdateError(RoomError):
    """The bytes are not a Yjs update; nothing was persisted or applied."""


class DraftSupersededError(RoomError):
    """The draft this room holds was published or discarded; the room has been retired."""


class RoomClosedError(RoomError):
    """The room has left memory; open it again."""


class RoomFailedError(RoomError):
    """A write could not be completed and the room no longer vouches for its ``Doc``.

    Either the insert into ``crdt_updates`` failed (it may have been committed all the same) or
    a committed update could not be applied. Nothing was broadcast. The room is closed and is
    rebuilt from the database on the next open; clients reconnect and resend.
    """


class EpochMismatchError(RoomError):
    """The caller holds another draft epoch than the record's current one (decision B6)."""

    def __init__(self, record_id: UUID, current: int) -> None:
        super().__init__(f"{record_id}: the current draft epoch is {current}")
        self.current = current


class Snapshot(BaseModel):
    """The state of a draft at one moment, as ``RoomManager.snapshot`` returns it."""

    record_id: UUID
    jsonld: dict[str, Any]
    """The draft projected to JSON-LD, **with** internal node IRIs (``lae.projection.project``)."""
    contributors: list[UUID]
    """Users whose updates the draft contains, in no particular order."""
    draft_epoch: int
    base_version: int
    """``records.version`` when the draft was hydrated."""
    last_update_id: int
    """Id of the last update the snapshot contains (``crdt_drafts.last_update_id``; 0 if none).

    It is the draft's version: every write changes it, compaction and eviction do not. A publish
    that must not lose edits made after its snapshot calls, in its transaction,
    ``lae.crdt.store.draft_unchanged(session, record_id, draft_epoch, last_update_id)``.
    ``max(crdt_updates.id)`` is **not** a substitute: compaction deletes those rows.
    """


class RoomInfo(BaseModel):
    """One entry of ``RoomManager.list_rooms``."""

    record_id: UUID
    draft_epoch: int
    base_version: int
    connections: int
    contributors: list[UUID]
    idle_seconds: float
    """Seconds since the last write, connect or disconnect."""


class Actor(Protocol):
    """Who performs a server-side edit: a ``lae.auth.CurrentUser`` or anything shaped like one."""

    @property
    def id(self) -> UUID: ...

    @property
    def display_name(self) -> str: ...

    @property
    def kind(self) -> str: ...


@dataclass(eq=False)
class Connection:
    """One authenticated socket in a room."""

    channel: Channel
    user_id: UUID
    display_name: str
    kind: str
    """``"human"`` or ``"agent"`` (``users.kind``)."""
    read_only: bool
    """True without ``edit_draft``: document updates from this socket are dropped."""
    close_code: int | None = None
    close_reason: str = ""
    dropped_updates: int = 0
    """Document updates dropped because the connection is read-only."""
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    awareness_ids: set[int] = field(default_factory=set)

    def close(self, code: int, reason: str) -> None:
        """Ask the serving loop to stop; the transport then closes the socket with ``code``."""
        if self.close_code is None:
            self.close_code = code
            self.close_reason = reason
        self.closing.set()


def _yroom_exception_handler(exception: Exception, log: logging.Logger) -> bool:
    """Log an exception raised by a room's background tasks; the room keeps running."""
    logger.error("room_task_failed", error=repr(exception))
    return True


class Room(YRoom):
    """The live draft of one record: a ``pycrdt.websocket.YRoom`` that persists before it applies.

    Created and owned by ``InMemoryRoomManager``. ``YRoom`` contributes the broadcast of
    ``Doc`` changes to every socket and the server's awareness renewal; this class replaces its
    message loop (``attend`` instead of ``serve``) and adds persistence.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        record_id: UUID,
        record_uri: str,
        doc: Doc[Any],
        draft_epoch: int,
        base_version: int,
        contributors: set[UUID],
        last_update_id: int = 0,
        uncompacted: int = 0,
        unintegrated: list[bytes] | None = None,
        compact_every: int = COMPACT_EVERY,
    ) -> None:
        super().__init__(
            ready=True, ydoc=doc, log=_yroom_log, exception_handler=_yroom_exception_handler
        )
        self._sessions = sessions
        self.record_id = record_id
        self.record_uri = record_uri
        self.draft_epoch = draft_epoch
        self.base_version = base_version
        self.contributors = contributors
        self.last_update_id = last_update_id
        self.uncompacted = uncompacted
        """Updates persisted since the last compaction."""
        self.unintegrated: list[bytes] = unintegrated or []
        """Updates the ``Doc`` holds as pending; compaction keeps them (invariant 4)."""
        self.compact_every = compact_every
        self.connections: set[Connection] = set()
        self.lock = asyncio.Lock()
        """Serialises persist-then-apply and compaction."""
        self.closed = False
        self.superseded = False
        """True once a write or a compaction found the draft row gone (published, discarded)."""
        self.last_activity = time.monotonic()
        self._owners: dict[int, Connection] = {}
        self._empty = asyncio.Event()
        self._empty.set()
        self._runner: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------------------------

    async def run(self) -> None:
        """Start the room's background tasks (broadcast, awareness renewal)."""
        self._runner = asyncio.create_task(self.start(), name=f"room-{self.record_id}")
        await self.started.wait()
        # Until the Doc is observed a change would not be broadcast.
        await self.ydoc_observed.wait()
        self._publish_server_state()

    async def shutdown(self) -> None:
        """Stop the background tasks. The caller has closed the sockets first."""
        runner, self._runner = self._runner, None
        if runner is None:
            return
        with contextlib.suppress(RuntimeError):
            await self.stop()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await runner

    async def wait_empty(self, seconds: float) -> bool:
        """Wait until no socket is being served; ``False`` if that takes longer than ``seconds``."""
        try:
            async with asyncio.timeout(seconds):
                await self._empty.wait()
        except TimeoutError:
            return False
        return True

    def touch(self) -> None:
        """Record activity, postponing idle eviction."""
        self.last_activity = time.monotonic()

    # -- reading -----------------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Project the draft as it is now."""
        return Snapshot(
            record_id=self.record_id,
            jsonld=project(self.ydoc),
            contributors=sorted(self.contributors),
            draft_epoch=self.draft_epoch,
            base_version=self.base_version,
            last_update_id=self.last_update_id,
        )

    def info(self) -> RoomInfo:
        """Describe the room for ``list_rooms``."""
        return RoomInfo(
            record_id=self.record_id,
            draft_epoch=self.draft_epoch,
            base_version=self.base_version,
            connections=len(self.connections),
            contributors=sorted(self.contributors),
            idle_seconds=round(time.monotonic() - self.last_activity, 3),
        )

    # -- writing (invariants 2, 3, 4) --------------------------------------------------------

    async def write(self, update: bytes, user_id: UUID | None) -> int:
        """Persist ``update``, then apply it to the ``Doc`` (which broadcasts it).

        Returns ``last_update_id``. Raises ``InvalidUpdateError`` (nothing happened),
        ``DraftSupersededError`` or ``RoomClosedError`` (nothing happened, the room is gone), or
        ``RoomFailedError`` (nothing was applied or broadcast; the update may or may not be in
        the database, and the room is closed so that it is rebuilt from there). If the caller is
        cancelled the write still runs to completion.
        """
        try:
            check_update(update)
        except ProtocolError as exc:
            raise InvalidUpdateError(str(exc)) from exc
        if update == EMPTY_UPDATE:
            return self.last_update_id
        return await _shielded(self._write(update, user_id))

    async def _write(self, update: bytes, user_id: UUID | None) -> int:
        async with self.lock:
            if self.closed:
                raise RoomClosedError(str(self.record_id))
            try:
                async with self._sessions() as session:
                    async with session.begin():
                        update_id = await store.append_update(
                            session, self.record_id, self.draft_epoch, update, user_id
                        )
            except Exception as exc:
                # Not known to be persisted, so not applied and not broadcast. Not known to be
                # absent either: the row is there if only the commit's answer was lost. The Doc
                # can no longer be trusted to contain every row, so the room goes.
                self.closed = True
                logger.error("update_not_persisted", record_id=str(self.record_id), error=repr(exc))
                raise RoomFailedError(str(self.record_id)) from exc
            # Committed. From here on the update survives a crash (replayed on the next open).
            if update_id is None:
                self.closed = self.superseded = True
                raise DraftSupersededError(str(self.record_id))
            await self._persisted(update_id)
            try:
                self.ydoc.apply_update(update)  # observers queue the broadcast to every socket
            except Exception as exc:  # cannot happen: check_update accepted the bytes
                self.closed = True
                logger.error(
                    "update_not_applied",
                    record_id=str(self.record_id),
                    update_id=update_id,
                    error=repr(exc),
                )
                raise RoomFailedError(str(self.record_id)) from exc
            if not is_integrated(self.ydoc, update):
                self.unintegrated.append(update)
            self.last_update_id = update_id
            self.uncompacted += 1
            if user_id is not None:
                self.contributors.add(user_id)
            self.touch()
            if self.uncompacted >= self.compact_every:
                try:
                    await self._compact()
                except DraftSupersededError:
                    raise
                except Exception as exc:  # the update itself is safe in crdt_updates
                    logger.error(
                        "compaction_failed", record_id=str(self.record_id), error=repr(exc)
                    )
            return update_id

    async def _persisted(self, update_id: int) -> None:
        """Hook between the commit and the apply. Does nothing; the crash test overrides it."""

    async def compact(self) -> None:
        """Fold the persisted updates into ``crdt_drafts.state`` (shielded like ``write``)."""
        await _shielded(self._compact_with_lock())

    async def _compact_with_lock(self) -> None:
        async with self.lock:
            if not self.closed:
                await self._compact()

    async def _compact(self) -> None:
        """Write the compacted state and delete the rows it replaces. The caller holds the lock."""
        if self.uncompacted == 0:
            return
        self.unintegrated = [u for u in self.unintegrated if not is_integrated(self.ydoc, u)]
        state = self.ydoc.get_update()
        if self.unintegrated:
            # Doc.get_update() omits pending structs and deletes; keep them at the update level.
            state = pycrdt.merge_updates(state, *self.unintegrated)
            logger.warning(
                "compaction_keeps_pending_updates",
                record_id=str(self.record_id),
                pending=len(self.unintegrated),
            )
        async with self._sessions() as session:
            async with session.begin():
                current = await store.compact(
                    session, self.record_id, self.draft_epoch, state, self.last_update_id
                )
        if not current:
            self.closed = self.superseded = True
            raise DraftSupersededError(str(self.record_id))
        if self.unintegrated:
            self.unintegrated = [state]
        self.uncompacted = 0

    # -- sockets -----------------------------------------------------------------------------

    async def serve(self, channel: Channel) -> None:
        """Not available: a socket needs an authenticated ``Connection`` (use ``attend``)."""
        raise NotImplementedError("use InMemoryRoomManager.serve(room, connection)")

    async def attend(self, connection: Connection) -> None:
        """Serve one socket until it disconnects or ``connection.close`` is called.

        Returns normally in both cases; the transport closes the socket with
        ``connection.close_code`` if one was set.
        """
        if self.closed:
            connection.close(CLOSE_ROOM_CLOSED, REASON_ROOM_CLOSED)
            return
        channel = connection.channel
        self.clients.add(channel)
        self.connections.add(connection)
        self._empty.clear()
        self.touch()
        self._publish_server_state()
        closing = asyncio.ensure_future(connection.closing.wait())
        try:
            await channel.send(create_sync_message(self.ydoc))
            # y-websocket servers send the known presence on connect; YRoom does not (S2 finding 4).
            states = self.awareness.encode_awareness_update(list(self.awareness.states))
            await channel.send(create_awareness_message(states))
            while connection.close_code is None:
                receiving = asyncio.ensure_future(channel.__anext__())
                await asyncio.wait({receiving, closing}, return_when=asyncio.FIRST_COMPLETED)
                if not receiving.done():
                    receiving.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                        await receiving
                    break
                try:
                    message = receiving.result()
                except StopAsyncIteration:
                    break
                if message:
                    await self._handle(connection, message)
        finally:
            closing.cancel()
            self.clients.discard(channel)
            self.connections.discard(connection)
            self._forget_presence(connection)
            self._publish_server_state()
            self.touch()
            if not self.connections:
                self._empty.set()

    async def _handle(self, connection: Connection, message: bytes) -> None:
        """Process one frame; a failure closes this socket only."""
        try:
            kind = message[0]
            if kind == YMessageType.SYNC:
                await self._handle_sync(connection, message)
            elif kind == YMessageType.AWARENESS:
                self._handle_awareness(connection, frame_payload(message[1:]))
            # Other message types (y-websocket auth, query-awareness) have no meaning here.
        except (ProtocolError, InvalidUpdateError, IndexError) as exc:
            logger.warning("protocol_error", record_id=str(self.record_id), error=str(exc))
            connection.close(CLOSE_PROTOCOL_ERROR, REASON_PROTOCOL_ERROR)
        except DraftSupersededError:
            connection.close(CLOSE_STALE_EPOCH, REASON_STALE_EPOCH)
            raise
        except RoomClosedError:
            # The room left memory while this update waited; the client reconnects and resends
            # (and meets the epoch guard if the draft is gone).
            connection.close(CLOSE_ROOM_CLOSED, REASON_ROOM_CLOSED)
            raise
        except RoomFailedError:
            # Not applied and not broadcast. The client resends on reconnect.
            connection.close(CLOSE_INTERNAL_ERROR, REASON_PERSIST_FAILED)
            raise
        except Exception as exc:
            logger.error("message_failed", record_id=str(self.record_id), error=repr(exc))
            connection.close(CLOSE_INTERNAL_ERROR, REASON_INTERNAL_ERROR)

    async def _handle_sync(self, connection: Connection, message: bytes) -> None:
        step = message[1]
        if step == YSyncMessageType.SYNC_STEP1:
            frame_payload(message[2:])  # reject a truncated state vector before pycrdt asserts
            try:
                reply = handle_sync_message(message[1:], self.ydoc)
            except Exception as exc:
                raise ProtocolError(f"bad state vector: {exc}") from exc
            if reply is not None:
                await connection.channel.send(reply)
        elif step in (YSyncMessageType.SYNC_STEP2, YSyncMessageType.SYNC_UPDATE):
            update = frame_payload(message[2:])
            if update == EMPTY_UPDATE:
                return
            if connection.read_only:
                connection.dropped_updates += 1
                return
            await self.write(update, connection.user_id)
        else:
            raise ProtocolError(f"unknown sync message type {step}")

    # -- presence ----------------------------------------------------------------------------

    def _server_state(self) -> dict[str, Any]:
        return {
            "kind": "server",
            "draft_epoch": self.draft_epoch,
            "base_version": self.base_version,
            "read_only": sorted({str(c.user_id) for c in self.connections if c.read_only}),
        }

    def _publish_server_state(self) -> None:
        """Set the room's own awareness state if it changed (broadcast by ``YRoom``)."""
        state = self._server_state()
        if self._task_group is not None and self.awareness.get_local_state() != state:
            self.awareness.set_local_state(state)

    def _known_clock(self, client_id: int) -> int:
        # An unknown client counts as clock 0, as in y-protocols and pycrdt: a first state must
        # carry a clock of at least 1 (every client's constructor has already used 0).
        meta = self.awareness.meta.get(client_id)
        return 0 if meta is None else int(meta["clock"])

    def _handle_awareness(self, connection: Connection, payload: bytes) -> None:
        """Stamp, apply and broadcast the presence states this socket publishes.

        Only entries newer than what the room knows are taken, which drops the echoes
        y-websocket sends for other clients' states. An entry for a client id that another
        user's socket published is dropped as well.
        """
        accepted: list[AwarenessEntry] = []
        for client_id, clock, state in decode_awareness(payload):
            if client_id == self.awareness.client_id:
                continue
            known = self._known_clock(client_id)
            present = client_id in self.awareness.states
            if not (clock > known or (clock == known and state is None and present)):
                continue
            owner = self._owners.get(client_id)
            if owner is not None and owner is not connection:
                if owner.user_id != connection.user_id:
                    continue
                owner.awareness_ids.discard(client_id)  # the same user moved to a new socket
            if state is None:
                self._owners.pop(client_id, None)
                connection.awareness_ids.discard(client_id)
            else:
                state = {
                    **state,
                    "user_id": str(connection.user_id),
                    "display_name": connection.display_name,
                    "kind": connection.kind,
                    "read_only": connection.read_only,
                }
                self._owners[client_id] = connection
                connection.awareness_ids.add(client_id)
            accepted.append((client_id, clock, state))
        self._spread_awareness(accepted, connection)

    def _forget_presence(self, connection: Connection) -> None:
        """Remove the states a closed socket published (S2 finding 5)."""
        gone: list[AwarenessEntry] = []
        for client_id in connection.awareness_ids:
            if self._owners.get(client_id) is connection:
                del self._owners[client_id]
                if client_id in self.awareness.states:
                    gone.append((client_id, self._known_clock(client_id) + 1, None))
        connection.awareness_ids.clear()
        self._spread_awareness(gone, connection)

    def _spread_awareness(self, entries: list[AwarenessEntry], origin: Connection) -> None:
        if not entries:
            return
        update = encode_awareness(entries)
        self.awareness.apply_awareness_update(update, origin)
        if self._task_group is None:
            return
        message = create_awareness_message(update)
        for client in list(self.clients):
            # The sender gets its own states back (same clock, so it ignores them): y-websocket
            # counts any message as a sign of life.
            self._task_group.start_soon(self._send_to_client, client, message)


async def _shielded[T](coroutine: Any) -> T:
    """Run ``coroutine`` to completion even if the awaiting task is cancelled meanwhile."""
    task: asyncio.Task[T] = asyncio.ensure_future(coroutine)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            task.add_done_callback(_log_abandoned)
        raise


def _log_abandoned(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("shielded_task_failed", error=repr(task.exception()))


class RoomManager(Protocol):
    """Everything that may be done to live drafts (decision B1). Later phases add, never reshape."""

    async def open(self, record_id: UUID) -> Room:
        """Return the record's room, loading or creating its draft if necessary."""
        ...

    async def get(self, record_id: UUID) -> Room | None:
        """Return the record's room if it is in memory, without loading anything."""
        ...

    async def is_open(self, record_id: UUID) -> bool:
        """Return whether the record's room is in memory."""
        ...

    async def snapshot(self, record_id: UUID) -> Snapshot:
        """Project the record's draft; raise ``DraftNotFoundError`` if it has none."""
        ...

    async def close(
        self, record_id: UUID, *, discard: bool, draft_epoch: int | None = None
    ) -> None:
        """Drop the room from memory; with ``discard`` also delete the draft and end its epoch.

        ``draft_epoch`` limits a discard to the draft of that epoch.
        """
        ...

    async def apply_update(self, record_id: UUID, update: bytes, user_id: UUID | None) -> None:
        """Persist, apply and broadcast a Yjs update on behalf of ``user_id``."""
        ...

    async def list_rooms(self) -> list[RoomInfo]:
        """Describe every room in memory."""
        ...

    async def eject(self, user_id: UUID) -> int:
        """Disconnect every socket of a user; return how many were closed."""
        ...

    async def transact(self, record_id: UUID, actor: Actor, fn: Callable[[Doc[Any]], None]) -> str:
        """Apply ``fn(doc)`` as one Yjs transaction tagged with ``actor``; return its id.

        Phase 3 Step 3.6 implements this.
        """
        raise NotImplementedError

    async def revert(self, record_id: UUID, transaction_ids: Sequence[str]) -> None:
        """Undo the given ``transact`` transactions (decision B11).

        Phase 3 Step 3.6 implements this.
        """
        raise NotImplementedError


class InMemoryRoomManager:
    """``RoomManager`` keeping ``Room`` objects in this process, persisted by ``lae.crdt.store``.

    Use as ``async with InMemoryRoomManager(sessionmaker) as manager`` or call ``start`` /
    ``stop``. Exactly one instance may serve a database (decision A4).
    """

    room_class: type[Room] = Room

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        idle_seconds: float = IDLE_SECONDS,
        compact_every: int = COMPACT_EVERY,
        sweep_seconds: float = SWEEP_SECONDS,
    ) -> None:
        self._sessions = sessions
        self.idle_seconds = idle_seconds
        self.compact_every = compact_every
        self.sweep_seconds = sweep_seconds
        self._rooms: dict[UUID, Room] = {}
        self._locks: dict[UUID, tuple[asyncio.Lock, int]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._sweeper: asyncio.Task[None] | None = None
        self._abandoned = False

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        """Start the idle-eviction sweep."""
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep(), name="room-sweeper")

    async def stop(self) -> None:
        """Compact and drop every room, closing its sockets with 1012 so that clients reconnect."""
        sweeper, self._sweeper = self._sweeper, None
        if sweeper is not None:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
        for room in list(self._rooms.values()):
            await self._retire(room, CLOSE_ROOM_CLOSED, REASON_ROOM_CLOSED, compact=True)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def abandon(self) -> None:
        """Drop every room **without writing anything** and refuse to open rooms from now on.

        For a process that may no longer be the only ``crdt-ws`` on its database
        (``lae.crdt.instance``): its ``Doc`` objects must not be compacted over what another
        process has written. Sockets are closed with 1012; nothing is lost, because every
        update a room accepted is in ``crdt_updates`` already.
        """
        self._abandoned = True
        for room in list(self._rooms.values()):
            await self._retire(room, CLOSE_ROOM_CLOSED, REASON_ROOM_CLOSED)

    async def __aenter__(self) -> InMemoryRoomManager:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    @contextlib.asynccontextmanager
    async def _record_lock(self, record_id: UUID) -> AsyncIterator[None]:
        """Serialise open, close and eviction of one record's room."""
        lock, users = self._locks.get(record_id, (asyncio.Lock(), 0))
        self._locks[record_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[record_id]
            if users <= 1:
                del self._locks[record_id]
            else:
                self._locks[record_id] = (lock, users - 1)

    # -- RoomManager -------------------------------------------------------------------------

    async def open(
        self,
        record_id: UUID,
        *,
        content: dict[str, Any] | None = None,
        created_by: UUID | None = None,
        create: bool = True,
        expected_epoch: int | None = None,
    ) -> Room:
        """Return the record's room, loading its draft or creating one from ``records.data``.

        ``content`` replaces ``records.data`` as the JSON-LD a **new** draft is hydrated from
        (revert to a revision, Step 1.14; templates); with an existing draft it raises
        ``DraftExistsError``. ``created_by`` is stored on a new draft. With ``create=False`` a
        record without a draft raises ``DraftNotFoundError`` instead of getting one.

        ``expected_epoch`` is the draft epoch the caller holds (a socket's ``?epoch=``). If it
        is not the record's current one, ``EpochMismatchError`` is raised and **no draft is
        created**: a stale browser tab must not leave a new draft behind on a record that was
        just published.

        Also raises ``RecordNotFoundError``, ``RecordNotEditableError`` (deleted or merged) and
        ``lae.projection.ProjectionError`` (content that cannot be hydrated).
        """
        async with self._record_lock(record_id):
            if self._abandoned:
                raise RoomFailedError("this crdt-ws process is shutting down")
            room = self._rooms.get(record_id)
            if room is not None and not room.closed:
                async with self._sessions() as session:
                    current = await store.draft_is_current(session, record_id, room.draft_epoch)
                if current:
                    if expected_epoch is not None and expected_epoch != room.draft_epoch:
                        raise EpochMismatchError(record_id, room.draft_epoch)
                    if content is not None:
                        raise DraftExistsError(str(record_id))
                    return room
                logger.warning("room_superseded", record_id=str(record_id), via="open")
                await self._retire(room, CLOSE_STALE_EPOCH, REASON_STALE_EPOCH)
            elif room is not None and room.superseded:
                await self._retire(room, CLOSE_STALE_EPOCH, REASON_STALE_EPOCH)
            elif room is not None:
                # Closed by a failed write and not yet retired; its sockets reconnect.
                await self._retire(room, CLOSE_ROOM_CLOSED, REASON_ROOM_CLOSED)
            room = await self._load(record_id, content, created_by, create, expected_epoch)
            self._rooms[record_id] = room
            await room.run()
            return room

    async def _load(
        self,
        record_id: UUID,
        content: dict[str, Any] | None,
        created_by: UUID | None,
        create: bool,
        expected_epoch: int | None = None,
    ) -> Room:
        """Build the room from the draft rows, or hydrate a new draft (invariant 1)."""
        async with self._sessions() as session:
            async with session.begin():
                # Locked against a concurrent publish until the draft is read or inserted.
                record = await store.load_record(session, record_id, for_share=True)
                if record is None:
                    raise RecordNotFoundError(str(record_id))
                epoch = record.draft_epoch
                draft = await store.load_draft(session, record_id, for_update=True)
                if draft is not None and draft.draft_epoch != epoch:
                    # Cannot happen while every deletion goes through store.delete_draft.
                    logger.error(
                        "stale_draft_deleted",
                        record_id=str(record_id),
                        draft_epoch=draft.draft_epoch,
                        record_epoch=epoch,
                    )
                    new_epoch = await store.delete_draft(session, record_id)
                    assert new_epoch is not None
                    epoch, draft = new_epoch, None
                if expected_epoch is not None and expected_epoch != epoch:
                    raise EpochMismatchError(record_id, epoch)
                if draft is None:
                    if not create:
                        raise DraftNotFoundError(str(record_id))
                    if record.status is not RecordStatus.ACTIVE:
                        raise RecordNotEditableError(str(record_id))
                    doc = hydrate(record.data if content is None else content, record.uri)
                    await store.insert_draft(
                        session,
                        record_id,
                        draft_epoch=epoch,
                        base_version=record.version,
                        state=doc.get_update(),
                        created_by=created_by,
                    )
                    return self.room_class(
                        self._sessions,
                        record_id=record_id,
                        record_uri=record.uri,
                        doc=doc,
                        draft_epoch=epoch,
                        base_version=record.version,
                        contributors=set(),
                        compact_every=self.compact_every,
                    )
                if content is not None:
                    raise DraftExistsError(str(record_id))
                updates = await store.load_updates(session, record_id, draft.draft_epoch)
        doc = Doc()
        doc.apply_update(draft.state)
        replayed = [draft.state]
        for update_id, update in updates:
            try:
                doc.apply_update(update)
            except Exception as exc:  # cannot happen: updates are validated before the insert
                logger.error(
                    "update_not_replayed",
                    record_id=str(record_id),
                    update_id=update_id,
                    error=repr(exc),
                )
            else:
                replayed.append(update)
        return self.room_class(
            self._sessions,
            record_id=record_id,
            record_uri=record.uri,
            doc=doc,
            draft_epoch=draft.draft_epoch,
            base_version=draft.base_version,
            contributors=set(draft.contributors),
            last_update_id=draft.last_update_id,
            uncompacted=len(updates),
            unintegrated=[u for u in replayed if not is_integrated(doc, u)],
            compact_every=self.compact_every,
        )

    async def get(self, record_id: UUID) -> Room | None:
        """Return the record's room if it is in memory, without loading anything."""
        room = self._rooms.get(record_id)
        return None if room is None or room.closed else room

    async def is_open(self, record_id: UUID) -> bool:
        """Return whether the record's room is in memory."""
        return await self.get(record_id) is not None

    async def snapshot(self, record_id: UUID) -> Snapshot:
        """Project the record's draft, loading the room if needed; never creates a draft."""
        room = await self.open(record_id, create=False)
        return room.snapshot()

    async def close(
        self, record_id: UUID, *, discard: bool, draft_epoch: int | None = None
    ) -> None:
        """Drop the room from memory, closing its sockets first.

        ``discard=False`` compacts the draft and closes sockets with 1012 (they reconnect and
        the room rehydrates). ``discard=True`` closes them with 4409, deletes the draft rows and
        ends the draft's epoch (``store.delete_draft``); it is safe to call after a publish has
        already deleted the rows, and for a record that has no draft or does not exist.

        ``draft_epoch`` (with ``discard``) names the draft that is meant: a room or a draft row
        of another epoch is left alone. Publish passes the epoch of the draft it published, so
        that its close, arriving late, cannot discard the draft somebody has opened since.
        """
        async with self._record_lock(record_id):
            room = self._rooms.get(record_id)
            if not discard:
                if room is not None:
                    await self._retire(room, CLOSE_ROOM_CLOSED, REASON_ROOM_CLOSED, compact=True)
                return
            if room is not None and draft_epoch in (None, room.draft_epoch):
                await self._retire(room, CLOSE_STALE_EPOCH, REASON_STALE_EPOCH)
            async with self._sessions() as session:
                async with session.begin():
                    await store.delete_draft(session, record_id, only_epoch=draft_epoch)

    async def apply_update(self, record_id: UUID, update: bytes, user_id: UUID | None) -> None:
        """Persist, apply and broadcast a Yjs update on behalf of ``user_id``.

        Opens the room (creating the draft) if needed. Raises ``InvalidUpdateError`` for bytes
        that are not an update, ``DraftSupersededError`` if the draft was published or
        discarded underneath the room, and ``RoomFailedError`` if the update could not be
        persisted.
        """
        for attempt in (1, 2):
            room = await self.open(record_id)
            try:
                await room.write(update, user_id)
            except DraftSupersededError:
                self._retire_soon(room, CLOSE_STALE_EPOCH, REASON_STALE_EPOCH)
                raise
            except RoomFailedError:
                self._retire_soon(room, CLOSE_ROOM_CLOSED, REASON_ROOM_CLOSED)
                raise
            except RoomClosedError:
                # The room left memory (eviction, a failed write) between open and write.
                if attempt == 2:
                    raise
            else:
                return

    async def list_rooms(self) -> list[RoomInfo]:
        """Describe every room in memory."""
        return [room.info() for room in self._rooms.values() if not room.closed]

    async def eject(self, user_id: UUID) -> int:
        """Disconnect every socket of a user with 4403; return how many were closed."""
        closed = 0
        for room in self._rooms.values():
            for connection in list(room.connections):
                if connection.user_id == user_id:
                    connection.close(CLOSE_EJECTED, REASON_EJECTED)
                    closed += 1
        return closed

    async def transact(self, record_id: UUID, actor: Actor, fn: Callable[[Doc[Any]], None]) -> str:
        """Not implemented before Phase 3 Step 3.6 (see ``revert``)."""
        raise NotImplementedError("RoomManager.transact arrives with Phase 3 Step 3.6")

    async def revert(self, record_id: UUID, transaction_ids: Sequence[str]) -> None:
        """Not implemented before Phase 3 Step 3.6.

        What that step has to respect: ``fn`` must not run in a transaction on the live ``Doc``,
        because the broadcast is triggered by the ``Doc`` changing and would precede the insert
        into ``crdt_updates`` (invariant 2). Run ``fn`` (and each undo) on a private replica
        that is synced from the room, take the resulting update, and pass it to ``Room.write``.
        An ``UndoManager`` on that replica gives ``revert``; its stack lives in memory only, so
        the step also has to decide what a transaction id means after eviction or a restart.
        Presence for an actor without a socket needs a server-side awareness state as well.
        """
        raise NotImplementedError("RoomManager.revert arrives with Phase 3 Step 3.6")

    # -- sockets -----------------------------------------------------------------------------

    async def serve(self, room: Room, connection: Connection) -> None:
        """Serve one authenticated socket in ``room`` until it closes (see ``Room.attend``)."""
        try:
            await room.attend(connection)
        except DraftSupersededError:
            self._retire_soon(room, CLOSE_STALE_EPOCH, REASON_STALE_EPOCH)
        except (RoomFailedError, RoomClosedError):
            self._retire_soon(room, CLOSE_ROOM_CLOSED, REASON_ROOM_CLOSED)

    # -- eviction and retirement (invariant 5) -----------------------------------------------

    async def evict_idle(self) -> list[UUID]:
        """Compact and drop every room without sockets that has been idle for ``idle_seconds``."""
        evicted: list[UUID] = []
        for record_id, room in list(self._rooms.items()):
            if not self._is_idle(room):
                continue
            async with self._record_lock(record_id):
                if self._rooms.get(record_id) is room and self._is_idle(room):
                    await self._retire(room, CLOSE_ROOM_CLOSED, REASON_ROOM_CLOSED, compact=True)
                    evicted.append(record_id)
        return evicted

    def _is_idle(self, room: Room) -> bool:
        idle = time.monotonic() - room.last_activity
        return room.closed or (not room.connections and idle >= self.idle_seconds)

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_seconds)
            try:
                evicted = await self.evict_idle()
            except Exception as exc:
                logger.error("idle_eviction_failed", error=repr(exc))
            else:
                if evicted:
                    logger.info("rooms_evicted", count=len(evicted))

    def _retire_soon(self, room: Room, code: int, reason: str) -> None:
        """Retire a superseded or failed room from inside one of its own sockets' tasks."""
        if self._rooms.get(room.record_id) is room:
            del self._rooms[room.record_id]
        task = asyncio.create_task(self._retire(room, code, reason))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _retire(self, room: Room, code: int, reason: str, *, compact: bool = False) -> None:
        """Take a room out of service: finish the write in flight, close sockets, stop it.

        With ``compact`` the persisted updates are folded into ``crdt_drafts.state`` first; a
        failure there loses nothing, because the updates stay in ``crdt_updates``.
        """
        async with room.lock:  # waits for a write between its commit and its apply
            if compact and not room.closed:
                try:
                    await _shielded(room._compact())
                except DraftSupersededError:
                    code, reason = CLOSE_STALE_EPOCH, REASON_STALE_EPOCH
                except Exception as exc:
                    logger.error(
                        "compaction_failed", record_id=str(room.record_id), error=repr(exc)
                    )
            room.closed = True
        if self._rooms.get(room.record_id) is room:
            del self._rooms[room.record_id]
        for connection in list(room.connections):
            connection.close(code, reason)
        if not await room.wait_empty(DRAIN_SECONDS):
            logger.warning("room_sockets_not_drained", record_id=str(room.record_id))
        await room.shutdown()
