"""Publish and discard endpoints (Phase 1 Step 1.10; spec §4.1; decisions B4, B8).

- ``POST /api/records/{id}/publish`` (``publish``): body ``{summary, accepted_warnings:
  [{code, path, record_id?}]}``; answers ``{version, published: [ids], draft_epoch}``. The work
  is ``lae.publish.service.publish``: the draft and every never-published record it refers to
  become published versions in one transaction.
- ``POST /api/records/{id}/draft/discard`` (``publish``): throws the draft away; a record that
  was never published is deleted with it. Optional body ``{draft_epoch}``: the epoch of the
  draft the caller is looking at; 409 ``LAE-CRDT-007`` if the record has moved on from it
  (issue #178). Answers ``{id, deleted, draft_epoch}``.

Errors are structured (``lae.publish.errors``, ``LAE-PUB-NNN``). The two an editor meets in
normal use:

- 422 ``LAE-PUB-003``: violations. ``params.records`` maps a record id to its diagnostics.
- 409 ``LAE-PUB-002``: warnings that were not accepted. ``params.records`` maps a record id to
  that record's warnings; show them, and send them back as ``accepted_warnings`` (with
  ``record_id`` for a record other than the one in the URL) to publish anyway. A warning is
  identified by its exact ``path``, so an edit that moves it has to be accepted again.

After a publish or a discard the draft's epoch is over: the editor reconnects with the
``draft_epoch`` of the answer.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lae.auth import CurrentUser, require
from lae.auth.permissions import optional_user
from lae.crdt.client import CrdtWsClient, crdt_client
from lae.db.session import get_session
from lae.publish.service import (
    AcceptedWarning,
    DiscardResult,
    PublishResult,
    discard,
    publish,
)
from lae.settings import Settings

SUMMARY_MAX_LENGTH = 2000
"""Longest edit summary a publish accepts."""

router = APIRouter(prefix="/api/records", tags=["publish"])


class PublishRequest(BaseModel):
    """Body of ``POST /api/records/{id}/publish``."""

    summary: str = Field(default="", max_length=SUMMARY_MAX_LENGTH)
    """The edit summary stored with the revision of every record published by the request."""

    accepted_warnings: list[AcceptedWarning] = []
    """The warnings the editor was shown and accepts."""


class DiscardRequest(BaseModel):
    """Optional body of ``POST /api/records/{id}/draft/discard``."""

    draft_epoch: int | None = None
    """Epoch of the draft the caller means: the one its editor is connected with. The discard
    is refused (409 ``LAE-CRDT-007``) if the record is at another epoch by now."""


async def session_factory(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _caller: Annotated[CurrentUser | None, Depends(optional_user)],
) -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency: the application's session factory, for work in its own transactions.

    The request's session has been used to authenticate the caller (``_caller`` makes sure of
    the order); its connection goes back to the pool here, so that a request never holds one
    connection while it waits for another.
    """
    await session.rollback()
    factory: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return factory


@router.post(
    "/{record_id}/publish",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "LAE-REC-001"},
        status.HTTP_409_CONFLICT: {
            "description": "LAE-PUB-001, LAE-PUB-002, LAE-PUB-004, LAE-PUB-005, LAE-PUB-006"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "LAE-PUB-003"},
    },
)
async def publish_record(
    record_id: UUID,
    body: PublishRequest,
    user: Annotated[CurrentUser, Depends(require("publish"))],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
    crdt: Annotated[CrdtWsClient, Depends(crdt_client)],
    request: Request,
) -> PublishResult:
    """Publish the record's draft, with the never-published records it refers to."""
    settings: Settings = request.app.state.settings
    return await publish(
        record_id,
        user,
        body.summary,
        body.accepted_warnings,
        sessions=sessions,
        crdt=crdt,
        settings=settings,
    )


@router.post(
    "/{record_id}/draft/discard",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "LAE-REC-001"},
        status.HTTP_409_CONFLICT: {"description": "LAE-CRDT-007"},
    },
)
async def discard_draft(
    record_id: UUID,
    user: Annotated[CurrentUser, Depends(require("publish"))],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
    crdt: Annotated[CrdtWsClient, Depends(crdt_client)],
    body: DiscardRequest | None = None,
) -> DiscardResult:
    """Discard the record's draft; delete the record if it was never published.

    409 ``LAE-CRDT-007`` if the draft named by ``draft_epoch`` is no longer the record's, or
    the draft was published while the request was under way.
    """
    return await discard(
        record_id,
        user,
        sessions=sessions,
        crdt=crdt,
        draft_epoch=None if body is None else body.draft_epoch,
    )
