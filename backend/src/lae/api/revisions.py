"""Revision diffs, the draft diff, and revert-to-revision (Phase 1 Step 1.14; spec §4.3).

Beside the revision list and the stored JSON-LD of one revision (``lae.api.records``, Step 1.6),
all under ``/api/records``:

- ``GET /{id}/revisions/{m}/diff/{n}`` (signed in): the typed changes that turn version ``m``
  into version ``n``, in either direction; ``m = n`` gives none.
- ``POST /{id}/draft/diff`` (``edit_draft``; owner decision 2026-11-23: the diff reveals nothing
  a Collaborator cannot already see in the draft and the published record): the changes that
  turn the published version into the live draft, for the Publish dialog. The draft is read
  with ``CrdtWsClient.snapshot``.
- ``POST /{id}/revisions/{n}/revert`` (``publish``): replace the record's draft with revision
  ``n``. Nothing is published: revisions are append-only (decision B9), and the editor publishes
  the reverted draft as usual, with a summary such as "Revert to revision n".

Diffs answer ``SemanticDiff``: ``{record_id, from_version, to_version, draft_epoch, changes}``.
``changes`` is the list ``lae.diff.semantic`` specifies; its display labels follow the caller's
``Accept-Language`` like transcluded labels do.

**Versions** resolve as everywhere else (``lae.api.records.load_version``, ADR-0023): the
``record_revisions`` row, else ``records.data`` for the current version. A bulk-loaded record has
no row for version 1 until its first publish writes one (Step 1.10); once it has moved on
without one, version 1 is 404 ``LAE-REC-004`` for a diff and for a revert alike.

**A never-published record** (``version = 0``) has nothing published to compare with, so its
draft is compared with the bare record ``{"@context", "id", "type"}``: everything else in the
draft is reported as added, and ``from_version`` is 0.

**Revert** (decision B9, ADR-0027). Only ``crdt-ws`` may delete a draft, so the endpoint calls
``close(discard=True)``, which also ends the draft's epoch, and then ``open(content=...)`` with
the revision's stored JSON-LD, internal node IRIs included: the nodes of the reverted draft are
the nodes of that revision, so a later diff against it is empty and comment anchors hold.
Between the two calls an editor's socket can reconnect and create a draft from ``records.data``;
``open`` then answers 409 ``LAE-CRDT-004``. That draft holds nothing but the published record,
so the endpoint discards it and tries again, ``REVERT_ATTEMPTS`` times in all, and passes the
409 on if it still loses. The answer carries the new ``draft_epoch``, which every client needs
before it can connect to the room again (the old one is closed with 4409).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from lae.api.deps import locale_chain
from lae.api.errors import ApiError
from lae.api.records import VERSION_NOT_FOUND, RecordVersion, get_record_or_404, load_version
from lae.auth import CurrentUser, current_user, require
from lae.crdt import errors as crdt_errors
from lae.crdt.client import CrdtWsClient, CrdtWsError, crdt_client
from lae.db.models import Record, RecordStatus
from lae.db.session import get_session
from lae.diff.semantic import Change, semantic_diff
from lae.labels.refs import reference_ids
from lae.labels.transclude import resolve_labels
from lae.logging import bind_log_context, get_logger
from lae.projection import ProjectionError, hydrate

REVERT_ATTEMPTS: Final = 3
"""How often revert discards and reopens before it gives up with 409 ``LAE-CRDT-004``."""

SKELETON_KEYS: Final = ("@context", "id", "type")
"""What a never-published record is taken to consist of when its draft is diffed."""

logger = get_logger(__name__)

router = APIRouter(prefix="/api/records", tags=["revisions"])


class SemanticDiff(BaseModel):
    """The typed changes between two states of a record."""

    record_id: UUID
    from_version: int
    """The version the changes start from; 0 for the draft of a never-published record."""
    to_version: int | None
    """The version the changes lead to; ``None`` when they lead to the live draft."""
    draft_epoch: int | None
    """Epoch of the draft that was compared; ``None`` for a diff of two versions."""
    changes: list[Change]


class Reverted(BaseModel):
    """Answer of ``POST /api/records/{id}/revisions/{n}/revert``."""

    id: UUID
    version: int
    """The revision the draft now holds."""
    draft_epoch: int
    """Epoch of the new draft; clients reconnect to the room with it."""
    base_version: int
    """``records.version`` the draft will be published on top of."""


async def labelled_diff(
    old: dict[str, Any], new: dict[str, Any], chain: Sequence[str], session: AsyncSession
) -> list[Change]:
    """Return ``semantic_diff(old, new)`` with display labels for the locale chain (one query)."""
    labels = await resolve_labels(reference_ids(old) | reference_ids(new), chain, session)
    return await asyncio.to_thread(semantic_diff, old, new, labels=labels)


async def _version_or_404(session: AsyncSession, record: Record, version: int) -> RecordVersion:
    found = await load_version(session, record, version)
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, VERSION_NOT_FOUND, {"version": version})
    return found


@router.get(
    "/{record_id}/revisions/{from_version}/diff/{to_version}",
    responses={status.HTTP_404_NOT_FOUND: {"description": "LAE-REC-001, LAE-REC-004"}},
)
async def diff_revisions(
    record_id: UUID,
    from_version: int,
    to_version: int,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    chain: Annotated[list[str], Depends(locale_chain)],
) -> SemanticDiff:
    """Return the changes that turn version ``from_version`` into version ``to_version``."""
    bind_log_context(user_id=user.id, record_id=record_id)
    record = await get_record_or_404(session, record_id)
    old = await _version_or_404(session, record, from_version)
    new = await _version_or_404(session, record, to_version)
    return SemanticDiff(
        record_id=record_id,
        from_version=from_version,
        to_version=to_version,
        draft_epoch=None,
        changes=await labelled_diff(old.data, new.data, chain, session),
    )


@router.post(
    "/{record_id}/draft/diff",
    responses={status.HTTP_404_NOT_FOUND: {"description": "LAE-REC-001, LAE-CRDT-003"}},
)
async def diff_draft(
    record_id: UUID,
    user: Annotated[CurrentUser, Depends(require("edit_draft"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    chain: Annotated[list[str], Depends(locale_chain)],
    crdt: Annotated[CrdtWsClient, Depends(crdt_client)],
) -> SemanticDiff:
    """Return the changes that turn the published version into the live draft."""
    bind_log_context(user_id=user.id, record_id=record_id)
    record = await get_record_or_404(session, record_id)
    snapshot = await crdt.snapshot(record_id)
    if snapshot is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            crdt_errors.DRAFT_NOT_FOUND,
            {"record_id": str(record_id)},
        )
    published: dict[str, Any] = record.data
    if record.version == 0:
        published = {key: published[key] for key in SKELETON_KEYS if key in published}
    return SemanticDiff(
        record_id=record_id,
        from_version=record.version,
        to_version=None,
        draft_epoch=snapshot.draft_epoch,
        changes=await labelled_diff(published, snapshot.jsonld, chain, session),
    )


@router.post(
    "/{record_id}/revisions/{version}/revert",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "LAE-REC-001, LAE-REC-004"},
        status.HTTP_409_CONFLICT: {"description": "LAE-CRDT-004, LAE-CRDT-005"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "LAE-CRDT-008"},
    },
)
async def revert_to_revision(
    record_id: UUID,
    version: int,
    user: Annotated[CurrentUser, Depends(require("publish"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    crdt: Annotated[CrdtWsClient, Depends(crdt_client)],
) -> Reverted:
    """Replace the record's draft with revision ``version``; return the new ``draft_epoch``.

    Whatever draft the record had is discarded, with every unpublished edit in it. 409
    ``LAE-CRDT-005`` for a deleted or merged record; 409 ``LAE-CRDT-004`` if other clients kept
    opening a draft between the discard and the open (try again).
    """
    bind_log_context(user_id=user.id, record_id=record_id)
    record = await get_record_or_404(session, record_id)
    if record.status != RecordStatus.ACTIVE:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            crdt_errors.RECORD_NOT_EDITABLE,
            {"record_id": str(record_id)},
        )
    revision = await _version_or_404(session, record, version)
    try:
        # Checked before the current draft is discarded, so that a revision the room cannot
        # hold (none should exist) does not cost the record its draft.
        await asyncio.to_thread(hydrate, revision.data, record.uri)
    except ProjectionError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            crdt_errors.INVALID_CONTENT,
            {"record_id": str(record_id), "projection_code": error.code},
        ) from error
    for attempt in range(1, REVERT_ATTEMPTS + 1):
        await crdt.close(record_id, discard=True)
        try:
            opened = await crdt.open(record_id, content=revision.data, created_by=user.id)
        except CrdtWsError as error:
            if error.code != crdt_errors.DRAFT_EXISTS or attempt == REVERT_ATTEMPTS:
                raise
            logger.info("revert_retried", version=version, attempt=attempt)
            continue
        logger.info("draft_reverted", version=version, draft_epoch=opened.draft_epoch)
        return Reverted(
            id=record_id,
            version=version,
            draft_epoch=opened.draft_epoch,
            base_version=opened.base_version,
        )
    raise AssertionError("unreachable")  # pragma: no cover
