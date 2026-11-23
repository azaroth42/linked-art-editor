"""Open drafts and their expiry (Phase 1, Step 1.15; decision B7).

- ``GET /api/drafts`` (``edit_draft``): the open drafts, the ones closest to expiry first. A
  caller who may ``publish`` (an Editor) sees every draft; anyone else (a Collaborator) sees the
  drafts they created or contributed to. Each entry has ``expires_at``, the moment the reaper
  (``lae.scheduler.reaper``) may discard the draft: ``last_activity_at + draft_ttl_days`` (the
  reaper runs hourly, so the discard happens within the hour after it). Phase 2 Step 2.9 adds
  the filters ``?contributor=&validation=&expires_within=`` and the Active Drafts Inbox.
- ``POST /api/records/{id}/draft/extend`` (``edit_draft``): set the draft's
  ``last_activity_at`` to now, which postpones its expiry by a full TTL; answers with the
  draft's entry. 404 ``LAE-REC-001`` without such a record, 404 ``LAE-CRDT-003`` if the record
  has no draft.

``label`` is the record's label for the caller's ``Accept-Language``: from its ``entity_labels``
row (published records), else extracted from ``records.data`` (a never-published record's
template). It is not read from the live draft, which would take a projection in ``crdt-ws``
per draft. ``contributors`` are the users whose edits the draft contains (``crdt_drafts``).

Extending touches only the ``crdt_drafts`` row, in one statement, so it never holds a lock while
waiting for another (``lae.crdt.store``'s lock order is not at stake); it does not delete or
replace the draft, so ``draft_epoch`` is unchanged (ADR-0027).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import Uuid, any_, case, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lae.api.deps import locale_chain
from lae.api.errors import ApiError
from lae.api.records import get_record_or_404
from lae.app import get_app_settings
from lae.auth import CurrentUser, require
from lae.crdt import errors as crdt_errors
from lae.db.models import CrdtDraft, EntityLabel, Record, User, UserKind
from lae.db.session import get_session
from lae.labels.extract import labels_from_record
from lae.labels.transclude import resolve_label
from lae.logging import bind_log_context, get_logger
from lae.settings import Settings

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["drafts"])


class DraftContributor(BaseModel):
    """A user whose edits a draft contains."""

    id: UUID
    display_name: str | None
    """``None`` if there is no such user any more."""
    kind: UserKind | None
    """``human`` or ``agent`` (``None`` if there is no such user any more)."""


class DraftSummary(BaseModel):
    """One open draft (an entry of ``GET /api/drafts``)."""

    record_id: UUID
    uri: str
    type: str
    label: str
    """The record's label for the caller's languages (see the module docstring)."""
    version: int
    """The record's published version; 0 if it was never published (the reaper deletes it)."""
    draft_epoch: int
    base_version: int
    created_by: UUID | None
    contributors: list[DraftContributor]
    last_activity_at: datetime
    expires_at: datetime
    """``last_activity_at + draft_ttl_days``: from then on the hourly reaper discards it."""


DRAFTS_QUERY: Final = (
    select(
        CrdtDraft.record_id,
        Record.uri,
        Record.type,
        Record.version,
        CrdtDraft.draft_epoch,
        CrdtDraft.base_version,
        CrdtDraft.created_by,
        CrdtDraft.contributors,
        CrdtDraft.last_activity_at,
        EntityLabel.labels,
        EntityLabel.default_label,
        # The record's JSON only where there is no label row to read the label from.
        case((EntityLabel.uri.is_(None), Record.data), else_=None).label("data"),
    )
    .join(Record, Record.id == CrdtDraft.record_id)
    .outerjoin(EntityLabel, EntityLabel.uri == Record.uri)
    .order_by(CrdtDraft.last_activity_at, CrdtDraft.record_id)
)
"""Every open draft with its record and label row, closest to expiry first."""


def _label(
    labels: dict[str, str] | None,
    default_label: str | None,
    data: dict[str, Any] | None,
    chain: Sequence[str],
    uri: str,
) -> str:
    if labels is None or default_label is None:
        if data is None:
            return uri
        labels, default_label = labels_from_record(data)
    return resolve_label(labels, default_label, chain)


async def _contributors(session: AsyncSession, ids: set[UUID]) -> dict[UUID, tuple[str, UserKind]]:
    if not ids:
        return {}
    rows = await session.execute(
        select(User.id, User.display_name, User.kind).where(User.id.in_(sorted(ids)))
    )
    return {user_id: (name, kind) for user_id, name, kind in rows}


async def load_drafts(
    session: AsyncSession,
    *,
    ttl_days: int,
    chain: Sequence[str],
    visible_to: UUID | None = None,
    record_id: UUID | None = None,
) -> list[DraftSummary]:
    """Return open drafts as ``DraftSummary``, closest to expiry first.

    ``visible_to`` limits them to the drafts that user created or contributed to; ``record_id``
    to one record's draft.
    """
    statement = DRAFTS_QUERY
    if visible_to is not None:
        statement = statement.where(
            or_(
                CrdtDraft.created_by == visible_to,
                literal(visible_to, Uuid) == any_(CrdtDraft.contributors),
            )
        )
    if record_id is not None:
        statement = statement.where(CrdtDraft.record_id == record_id)
    rows = (await session.execute(statement)).all()
    users = await _contributors(session, {user for row in rows for user in row.contributors})
    ttl = timedelta(days=ttl_days)
    drafts = []
    for row in rows:
        drafts.append(
            DraftSummary(
                record_id=row.record_id,
                uri=row.uri,
                type=row.type,
                label=_label(row.labels, row.default_label, row.data, chain, row.uri),
                version=row.version,
                draft_epoch=row.draft_epoch,
                base_version=row.base_version,
                created_by=row.created_by,
                contributors=[
                    DraftContributor(
                        id=user,
                        display_name=users[user][0] if user in users else None,
                        kind=users[user][1] if user in users else None,
                    )
                    for user in row.contributors
                ],
                last_activity_at=row.last_activity_at,
                expires_at=row.last_activity_at + ttl,
            )
        )
    return drafts


@router.get("/drafts")
async def list_drafts(
    user: Annotated[CurrentUser, Depends(require("edit_draft"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    chain: Annotated[list[str], Depends(locale_chain)],
) -> list[DraftSummary]:
    """List open drafts with their expiry: all for an Editor, one's own for a Collaborator."""
    bind_log_context(user_id=user.id)
    return await load_drafts(
        session,
        ttl_days=settings.draft_ttl_days,
        chain=chain,
        visible_to=None if user.has("publish") else user.id,
    )


@router.post(
    "/records/{record_id}/draft/extend",
    responses={status.HTTP_404_NOT_FOUND: {"description": "LAE-REC-001, LAE-CRDT-003"}},
)
async def extend_draft(
    record_id: UUID,
    user: Annotated[CurrentUser, Depends(require("edit_draft"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    chain: Annotated[list[str], Depends(locale_chain)],
) -> DraftSummary:
    """Reset the draft's ``last_activity_at`` to now, postponing its expiry by a full TTL."""
    bind_log_context(user_id=user.id, record_id=record_id)
    touched = await session.scalar(
        update(CrdtDraft)
        .where(CrdtDraft.record_id == record_id)
        .values(last_activity_at=func.now())
        .returning(CrdtDraft.record_id)
    )
    await session.commit()
    if touched is None:
        await get_record_or_404(session, record_id)
        raise ApiError(status.HTTP_404_NOT_FOUND, crdt_errors.DRAFT_NOT_FOUND)
    found = await load_drafts(
        session, ttl_days=settings.draft_ttl_days, chain=chain, record_id=record_id
    )
    if not found:  # published or discarded in between
        raise ApiError(status.HTTP_404_NOT_FOUND, crdt_errors.DRAFT_NOT_FOUND)
    logger.info("draft_extended", expires_at=found[0].expires_at.isoformat())
    return found[0]
