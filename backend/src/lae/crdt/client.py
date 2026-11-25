"""``CrdtWsClient``: how the ``api`` process (and the scheduler) reaches live drafts.

Rooms live in the memory of the single ``crdt-ws`` process, so ``api`` never reads a room or the
``crdt_*`` tables itself; it calls the internal RPC of ``lae.crdt.server`` (decision A4,
ADR-0011) with the shared secret ``LAE_INTERNAL_TOKEN`` at ``LAE_CRDT_WS_URL``.

In an endpoint::

    from typing import Annotated
    from fastapi import Depends
    from lae.crdt.client import CrdtWsClient, crdt_client

    @router.post("/api/records/{id}/draft/validate")
    async def validate_draft(id: UUID, crdt: Annotated[CrdtWsClient, Depends(crdt_client)]):
        snapshot = await crdt.snapshot(id)      # None: the record has no draft
        ...

Stable contract (Steps 1.6, 1.9, 1.10, 1.14, 1.15 and Phase 3 call these; signatures only grow):

- ``snapshot(record_id) -> Snapshot | None``: the draft projected to JSON-LD **with internal
  node IRIs** plus ``contributors``, ``draft_epoch``, ``base_version`` and ``last_update_id``.
  ``None`` means the record has no draft, or does not exist (nothing is created). Validation
  strips the IRIs itself where a schema requires it.
- ``open(record_id, *, content=None, created_by=None) -> OpenedRoom``: make sure the record has
  a draft and return its ``draft_epoch`` (``POST /api/records`` returns that number to the
  editor). ``content`` hydrates a *new* draft from other JSON-LD than ``records.data``
  (revert-to-revision: ``close(discard=True)`` first, then ``open(content=...)``; 409
  ``LAE-CRDT-004`` if somebody opened a draft in between).
- ``close(record_id, *, discard, draft_epoch=None, idle_for=None)``: ``discard=True`` deletes
  the draft, closes its sockets with 4409 and makes sure ``records.draft_epoch`` is beyond the deleted draft's
  epoch. Use it *instead of* deleting draft rows anywhere else (ADR-0027).

**Publish (Step 1.10), in this order:** ``snapshot`` → validate → one transaction that locks
the ``records`` row ``FOR UPDATE``, then requires
``lae.crdt.store.draft_unchanged(session, id, snapshot.draft_epoch, snapshot.last_update_id)``
(``False``: the draft was edited or replaced since the snapshot; start again), writes the
record with ``draft_epoch + 1`` and calls ``lae.crdt.store.delete_draft(session, id)`` → commit
→ ``close(id, discard=True, draft_epoch=snapshot.draft_epoch)`` (best effort; the epoch limits
it to the published draft, so it cannot discard a draft opened since).

- ``apply_update(record_id, update, user_id)``: a Yjs update, persisted then broadcast.
- ``list_rooms()``, ``eject(user_id)``.

Errors: ``CrdtWsError`` carries the HTTP status and the ``LAE-CRDT-NNN`` code of the answer;
``CrdtWsUnavailableError`` (a subclass, code ``LAE-CRDT-009``) means the process could not be
reached. Both are ``lae.api.errors.ApiError``, so an endpoint that lets one propagate answers
with the structured body; 404 from ``crdt-ws`` becomes 404, anything unexpected becomes 502/503.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

import httpx
from fastapi import Request, status
from pydantic import TypeAdapter

from lae.api.errors import ApiError
from lae.crdt import errors
from lae.crdt.rooms import RoomInfo, Snapshot
from lae.crdt.server import INTERNAL_TOKEN_HEADER, Ejected, OpenedRoom
from lae.settings import Settings

RPC_TIMEOUT_SECONDS: Final = 10.0
"""Timeout of one internal RPC call."""

_ROOM_LIST: Final = TypeAdapter(list[RoomInfo])


class CrdtWsError(ApiError):
    """``crdt-ws`` answered an internal RPC call with an error."""


class CrdtWsUnavailableError(CrdtWsError):
    """``crdt-ws`` could not be reached, or answered with something that is not its RPC."""


class CrdtWsClient:
    """Client of the ``crdt-ws`` internal RPC; cheap to create, shares the caller's HTTP client."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, internal_token: str | None) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._headers = {INTERNAL_TOKEN_HEADER: internal_token or ""}

    @classmethod
    def from_settings(cls, settings: Settings, http: httpx.AsyncClient) -> CrdtWsClient:
        """Build the client for ``LAE_CRDT_WS_URL`` and ``LAE_INTERNAL_TOKEN``."""
        token = settings.internal_token
        return cls(http, settings.crdt_ws_url, None if token is None else token.get_secret_value())

    async def _call(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            response = await self._http.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                headers=self._headers,
                timeout=RPC_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise CrdtWsUnavailableError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                errors.CRDT_WS_UNAVAILABLE,
                {"error": type(exc).__name__},
            ) from exc
        if response.is_success:
            return response
        try:
            body = response.json()
            code, params = str(body["code"]), dict(body["params"])
        except (ValueError, KeyError, TypeError):
            raise CrdtWsUnavailableError(
                status.HTTP_502_BAD_GATEWAY,
                errors.CRDT_WS_UNAVAILABLE,
                {"error": f"HTTP {response.status_code}"},
            ) from None
        # A refused internal token is a deployment fault, not something the end user did.
        status_code = 502 if response.status_code == 401 else response.status_code
        raise CrdtWsError(status_code, code, params)

    async def list_rooms(self) -> list[RoomInfo]:
        """Return the rooms currently in memory."""
        response = await self._call("GET", "/internal/rooms")
        return _ROOM_LIST.validate_json(response.content)

    async def snapshot(self, record_id: UUID) -> Snapshot | None:
        """Return the record's draft projected to JSON-LD, or ``None`` if it has no draft.

        ``None`` also when there is no such record. Never creates a draft.
        """
        try:
            response = await self._call("GET", f"/internal/rooms/{record_id}/snapshot")
        except CrdtWsError as exc:
            if exc.code in (errors.DRAFT_NOT_FOUND, errors.RECORD_NOT_FOUND):
                return None
            raise
        return Snapshot.model_validate_json(response.content)

    async def open(
        self,
        record_id: UUID,
        *,
        content: dict[str, Any] | None = None,
        created_by: UUID | None = None,
    ) -> OpenedRoom:
        """Make sure the record has a draft (creating it if needed) and return its epoch."""
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if created_by is not None:
            body["created_by"] = str(created_by)
        response = await self._call("POST", f"/internal/rooms/{record_id}/open", json=body)
        return OpenedRoom.model_validate_json(response.content)

    async def close(
        self,
        record_id: UUID,
        *,
        discard: bool,
        draft_epoch: int | None = None,
        idle_for: timedelta | None = None,
    ) -> None:
        """Drop the room from memory; with ``discard`` delete the draft and end its epoch.

        ``draft_epoch`` limits a discard to the draft of that epoch (pass the published
        snapshot's ``draft_epoch`` after a publish). ``idle_for`` limits it to a draft without
        activity for that long (the draft reaper): a draft edited or extended meanwhile is
        kept, with its room and sockets. The answer is 204 either way; the caller reads
        ``crdt_drafts`` to learn which.
        """
        body: dict[str, Any] = {"discard": discard}
        if draft_epoch is not None:
            body["draft_epoch"] = draft_epoch
        if idle_for is not None:
            body["idle_seconds"] = idle_for.total_seconds()
        await self._call("POST", f"/internal/rooms/{record_id}/close", json=body)

    async def apply_update(self, record_id: UUID, update: bytes, user_id: UUID | None) -> None:
        """Persist, apply and broadcast a Yjs update on behalf of ``user_id``."""
        body = {
            "update": base64.b64encode(update).decode("ascii"),
            "user_id": None if user_id is None else str(user_id),
        }
        await self._call("POST", f"/internal/rooms/{record_id}/apply", json=body)

    async def eject(self, user_id: UUID) -> int:
        """Close every socket of a user; return how many were closed."""
        response = await self._call("POST", f"/internal/users/{user_id}/eject")
        return Ejected.model_validate_json(response.content).closed


def crdt_client(request: Request) -> CrdtWsClient:
    """FastAPI dependency for ``api`` endpoints: the client on the process-wide HTTP client."""
    settings: Settings = request.app.state.settings
    http: httpx.AsyncClient = request.app.state.http_client
    return CrdtWsClient.from_settings(settings, http)
