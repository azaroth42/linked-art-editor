"""The draft reaper (decision B7, spec §4.1): hourly, discard drafts idle for ``draft_ttl_days``.

A draft is idle when ``crdt_drafts.last_activity_at`` (set by every persisted update and by
``POST /api/records/{id}/draft/extend``) is older than ``now() - draft_ttl_days``. For each:

1. The draft is read again: if it has another epoch or has had activity since the listing, it
   is left alone.
2. It is discarded through ``crdt-ws``: ``close(discard=True, draft_epoch=<its epoch>)`` closes
   its sockets with ``4409``, deletes the draft rows and ends the epoch (ADR-0027: nothing else
   may delete draft rows; the epoch limits the discard to the draft that was found idle). When
   ``crdt-ws`` cannot be reached, the draft is deleted with ``lae.crdt.store.delete_draft`` in
   one transaction that locks the ``records`` row, then the draft row (the lock order of
   ``crdt-ws``), and checks the epoch and the idleness again under those locks. A room that is
   still in memory in an unreachable ``crdt-ws`` finds its draft gone at its next write or
   compaction and closes (ADR-0027 item 3).
3. If the record was never published (``records.version = 0``), its ``records`` row is deleted
   too: in a transaction that locks the row ``FOR UPDATE`` and deletes it only if it is still
   at version 0 and nobody has opened a new draft on it meanwhile.

Then **abandoned creations**: ``version = 0`` records without any draft whose ``created_at`` is
older than the TTL (``POST /api/records`` cancelled between its commit and opening the draft,
see ``docs/questions.md`` Step 1.8 item 9) are deleted the same way as in step 3. The partial
index ``ix_records_unpublished_created_at`` (migration ``0007``) keeps this a small index scan.

Every action is logged (``draft_reaped``) with the record id, the action (``discarded``,
``record_deleted``), the draft's contributors, epoch and ``last_activity_at``. A draft that
fails (``crdt-ws`` answered with an error) is logged and counted, and the others go on.

**Residual race.** Between the re-read in step 1 and the discard in ``crdt-ws`` (one internal
HTTP call), an edit or an extend of a draft that has been idle for the whole TTL is lost with
it. The fallback path has no such window.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Interval, delete, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from lae.crdt import store
from lae.crdt.client import CrdtWsClient, CrdtWsUnavailableError
from lae.db.models import CrdtDraft, Record
from lae.logging import get_logger
from lae.scheduler.jobs import JobContext

JOB_NAME: Final = "reap_drafts"
"""The job's name; its ``sync_state`` record is ``job:reap_drafts``."""

INTERVAL: Final = timedelta(hours=1)
"""The reaper runs hourly (decision B7)."""

logger = get_logger(__name__)


class ReapAction(StrEnum):
    """What the reaper did to one record."""

    DISCARDED = "discarded"
    """The draft was discarded; the published record stays as it is."""
    RECORD_DELETED = "record_deleted"
    """The record was never published: its draft (if any) and the record row are gone."""


@dataclass(frozen=True, slots=True)
class IdleDraft:
    """A draft the listing found idle for longer than the TTL."""

    record_id: UUID
    draft_epoch: int
    version: int
    """``records.version`` when the draft was listed (0: never published)."""
    contributors: tuple[UUID, ...]
    last_activity_at: datetime


@dataclass(slots=True)
class ReapResult:
    """What one reaper pass did, by record id."""

    discarded: list[UUID] = field(default_factory=list)
    deleted: list[UUID] = field(default_factory=list)
    """Never-published records deleted (with their idle draft, or abandoned without one)."""
    skipped: list[UUID] = field(default_factory=list)
    """Listed as idle, but active, replaced or reopened by the time they were reached."""
    failed: list[UUID] = field(default_factory=list)
    interrupted: bool = False
    """The process began shutting down; the rest waits for the next run."""

    def summary(self) -> dict[str, Any]:
        """Counts for the job's ``sync_state`` record."""
        return {
            "discarded": len(self.discarded),
            "deleted": len(self.deleted),
            "skipped": len(self.skipped),
            "failed": len(self.failed),
            "interrupted": self.interrupted,
        }


def _never() -> bool:
    return False


def idle_before(ttl: timedelta) -> ColumnElement[datetime]:
    """SQL ``now() - ttl``: activity before this instant is idle (the database's clock)."""
    return func.now() - literal(ttl, Interval)


async def list_idle_drafts(
    sessions: async_sessionmaker[AsyncSession], ttl: timedelta
) -> list[IdleDraft]:
    """Return every draft idle for longer than ``ttl``, longest idle first."""
    statement = (
        select(
            CrdtDraft.record_id,
            CrdtDraft.draft_epoch,
            Record.version,
            CrdtDraft.contributors,
            CrdtDraft.last_activity_at,
        )
        .join(Record, Record.id == CrdtDraft.record_id)
        .where(CrdtDraft.last_activity_at < idle_before(ttl))
        .order_by(CrdtDraft.last_activity_at, CrdtDraft.record_id)
    )
    async with sessions() as session:
        rows = await session.execute(statement)
        return [
            IdleDraft(record_id, epoch, version, tuple(contributors), last_activity_at)
            for record_id, epoch, version, contributors, last_activity_at in rows
        ]


async def list_abandoned_records(
    sessions: async_sessionmaker[AsyncSession], ttl: timedelta
) -> list[UUID]:
    """Return never-published records without a draft created longer than ``ttl`` ago."""
    statement = (
        select(Record.id)
        .where(
            Record.version == 0,
            Record.created_at < idle_before(ttl),
            ~exists().where(CrdtDraft.record_id == Record.id),
        )
        .order_by(Record.created_at, Record.id)
    )
    async with sessions() as session:
        return list((await session.scalars(statement)).all())


async def _is_still_idle(session: AsyncSession, draft: IdleDraft, ttl: timedelta) -> bool:
    """Whether the record's draft still has the listed epoch and no activity since the TTL."""
    found = await session.scalar(
        select(CrdtDraft.record_id).where(
            CrdtDraft.record_id == draft.record_id,
            CrdtDraft.draft_epoch == draft.draft_epoch,
            CrdtDraft.last_activity_at < idle_before(ttl),
        )
    )
    return found is not None


async def discard_in_database(
    sessions: async_sessionmaker[AsyncSession], draft: IdleDraft, ttl: timedelta
) -> bool:
    """Delete an idle draft without ``crdt-ws``; ``False`` if it is no longer idle.

    One transaction: lock the ``records`` row (as ``store.delete_draft`` does), then the draft
    row, check epoch and idleness, and delete through ``store.delete_draft``.
    """
    async with sessions() as session, session.begin():
        await session.execute(
            select(Record.id).where(Record.id == draft.record_id).with_for_update(key_share=True)
        )
        locked = await store.load_draft(session, draft.record_id, for_update=True)
        if locked is None or not await _is_still_idle(session, draft, ttl):
            return False
        await store.delete_draft(session, draft.record_id, only_epoch=draft.draft_epoch)
        return True


async def delete_unpublished_record(
    sessions: async_sessionmaker[AsyncSession], record_id: UUID
) -> bool:
    """Delete a never-published record that has no draft; ``False`` if it no longer qualifies.

    The row is locked ``FOR UPDATE`` first, so a ``crdt-ws`` opening a draft on it (which locks
    it ``FOR SHARE``) either finishes first, and its draft is then seen here, or waits and then
    finds no record.
    """
    async with sessions() as session, session.begin():
        locked = await session.scalar(
            select(Record.id).where(Record.id == record_id, Record.version == 0).with_for_update()
        )
        if locked is None:
            return False
        if await session.scalar(select(exists().where(CrdtDraft.record_id == record_id))):
            return False
        await session.execute(delete(Record).where(Record.id == record_id))
        return True


async def _draft_with_epoch_exists(
    sessions: async_sessionmaker[AsyncSession], record_id: UUID, draft_epoch: int
) -> bool:
    async with sessions() as session:
        return await store.draft_is_current(session, record_id, draft_epoch)


async def reap_draft(
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
    draft: IdleDraft,
    ttl: timedelta,
) -> ReapAction | None:
    """Discard one idle draft (and a never-published record); ``None`` if it was left alone."""
    async with sessions() as session:
        if not await _is_still_idle(session, draft, ttl):
            return None
    via = "crdt-ws"
    try:
        await crdt.close(draft.record_id, discard=True, draft_epoch=draft.draft_epoch)
    except CrdtWsUnavailableError:
        via = "database"
        if not await discard_in_database(sessions, draft, ttl):
            return None
    else:
        if await _draft_with_epoch_exists(sessions, draft.record_id, draft.draft_epoch):
            raise RuntimeError("crdt-ws acknowledged the discard but the draft is still there")
    action = ReapAction.DISCARDED
    if draft.version == 0 and await delete_unpublished_record(sessions, draft.record_id):
        action = ReapAction.RECORD_DELETED
    logger.info(
        "draft_reaped",
        record_id=str(draft.record_id),
        action=action.value,
        contributors=[str(user) for user in draft.contributors],
        draft_epoch=draft.draft_epoch,
        last_activity_at=draft.last_activity_at.isoformat(),
        via=via,
    )
    return action


async def reap_drafts(
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
    ttl_days: int,
    *,
    should_stop: Callable[[], bool] = _never,
) -> ReapResult:
    """One reaper pass: idle drafts first, then abandoned never-published records."""
    if ttl_days < 1:
        raise ValueError(f"draft_ttl_days must be at least 1, not {ttl_days}")
    ttl = timedelta(days=ttl_days)
    result = ReapResult()
    for draft in await list_idle_drafts(sessions, ttl):
        if should_stop():
            result.interrupted = True
            return result
        try:
            action = await reap_draft(sessions, crdt, draft, ttl)
        except Exception:
            logger.exception(
                "draft_reap_failed",
                record_id=str(draft.record_id),
                contributors=[str(user) for user in draft.contributors],
            )
            result.failed.append(draft.record_id)
            continue
        if action is None:
            result.skipped.append(draft.record_id)
        elif action is ReapAction.RECORD_DELETED:
            result.deleted.append(draft.record_id)
        else:
            result.discarded.append(draft.record_id)
    for record_id in await list_abandoned_records(sessions, ttl):
        if should_stop():
            result.interrupted = True
            return result
        try:
            deleted = await delete_unpublished_record(sessions, record_id)
        except Exception:
            logger.exception("draft_reap_failed", record_id=str(record_id), contributors=[])
            result.failed.append(record_id)
            continue
        if deleted:
            result.deleted.append(record_id)
            logger.info(
                "draft_reaped",
                record_id=str(record_id),
                action=ReapAction.RECORD_DELETED.value,
                contributors=[],
                draft_epoch=None,
                last_activity_at=None,
                via="database",
            )
        else:
            result.skipped.append(record_id)
    return result


async def run(ctx: JobContext) -> Mapping[str, Any]:
    """The ``reap_drafts`` job: one pass with ``settings.draft_ttl_days``."""
    result = await reap_drafts(
        ctx.sessions, ctx.crdt(), ctx.settings.draft_ttl_days, should_stop=ctx.should_stop
    )
    return result.summary()
