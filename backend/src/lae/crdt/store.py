"""SQL for draft persistence: ``crdt_drafts``, ``crdt_updates`` and ``records.draft_epoch``.

Every function takes the caller's ``AsyncSession`` and neither commits nor rolls back, so the
caller decides what is atomic. ``lae.crdt.rooms`` is the only writer of live drafts; the
functions are separate so that the rules below can be read (and tested) in one place.

Rules (decisions B5, B6; ADR-0027):

- One ``crdt_drafts`` row per record is the draft. Its ``draft_epoch`` equals
  ``records.draft_epoch`` for as long as the draft is current.
- ``append_update`` and ``compact`` only touch a draft whose epoch still matches and report
  ``False`` / ``None`` otherwise. A room that gets that answer has been superseded (its draft
  was published or discarded by another transaction) and must close without writing anything.
  Both lock the ``crdt_drafts`` row, so they queue behind a publish that is deleting it.
- **Deleting a draft ends its epoch.** ``delete_draft`` removes the rows and makes sure
  ``records.draft_epoch`` is greater than the deleted draft's epoch. The publish transaction
  (Step 1.10), the reaper (Step 1.15) and discard must delete drafts through it (or through
  ``RoomManager.close(discard=True)``, which calls it). A fresh hydration at an epoch that some
  client still holds an older document for would be merged with that document on reconnect;
  two hydrations of one record must never be merged (``lae.projection.hydrate``).
- **Lock order: the ``records`` row first, then the ``crdt_drafts`` row.** ``load_record`` (for
  a room being loaded), ``append_update`` and ``delete_draft`` all take a lock on the record's
  row before they touch its draft, and the publish transaction (Step 1.10) locks ``records``
  ``FOR UPDATE`` first as well. With one order there is no deadlock between a write, a load and
  a publish; a write that arrives during a publish waits for it and then finds its draft gone.
- **``crdt_drafts.last_update_id`` is the draft's version.** ``append_update`` sets it to the id
  of the row it inserts, in the same statement; compaction leaves it alone. Unlike
  ``max(crdt_updates.id)`` it survives compaction, so "has the draft changed since this
  snapshot" is ``draft_unchanged`` (publish asks that inside its transaction).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Interval,
    LargeBinary,
    any_,
    case,
    delete,
    func,
    insert,
    literal,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Integer, Uuid

from lae.db.models import CrdtDraft, CrdtUpdate, Record, RecordStatus


@dataclass(frozen=True, slots=True)
class RecordRow:
    """The columns of ``records`` a room needs."""

    id: UUID
    uri: str
    data: dict[str, Any]
    version: int
    draft_epoch: int
    status: RecordStatus


@dataclass(frozen=True, slots=True)
class DraftRow:
    """A ``crdt_drafts`` row."""

    record_id: UUID
    draft_epoch: int
    base_version: int
    state: bytes
    contributors: tuple[UUID, ...]
    last_update_id: int
    """Id of the last update appended to the draft (0 if none); not reset by compaction."""


async def load_record(
    session: AsyncSession, record_id: UUID, *, for_share: bool = False
) -> RecordRow | None:
    """Return the record's row, or ``None`` if there is no such record.

    ``for_share`` locks the row ``FOR SHARE`` until the transaction ends: a publish, which
    updates the row, cannot commit in between, so what the caller then reads from
    ``crdt_drafts`` belongs to the ``version``, ``data`` and ``draft_epoch`` returned here.
    """
    statement = select(
        Record.id,
        Record.uri,
        Record.data,
        Record.version,
        Record.draft_epoch,
        Record.status,
    ).where(Record.id == record_id)
    if for_share:
        statement = statement.with_for_update(read=True)
    row = (await session.execute(statement)).one_or_none()
    return None if row is None else RecordRow(*row)


async def load_draft(
    session: AsyncSession, record_id: UUID, *, for_update: bool = False
) -> DraftRow | None:
    """Return the record's draft row (optionally locked), or ``None`` if it has no draft."""
    statement = select(
        CrdtDraft.record_id,
        CrdtDraft.draft_epoch,
        CrdtDraft.base_version,
        CrdtDraft.state,
        CrdtDraft.contributors,
        CrdtDraft.last_update_id,
    ).where(CrdtDraft.record_id == record_id)
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None
    return DraftRow(row[0], row[1], row[2], bytes(row[3]), tuple(row[4]), row[5])


async def draft_unchanged(
    session: AsyncSession, record_id: UUID, draft_epoch: int, last_update_id: int
) -> bool:
    """Lock the draft row and return whether it is still the draft a snapshot was taken of.

    True when the row exists with ``draft_epoch`` and no update has been appended since the one
    with ``last_update_id`` (``Snapshot.draft_epoch`` / ``Snapshot.last_update_id``). The row
    stays locked until the transaction ends, so no update can be appended before the caller
    commits: a publish that gets ``True`` and then deletes the draft has lost no edit. The
    caller must already hold its lock on the ``records`` row (lock order, see the module
    docstring).
    """
    draft = await load_draft(session, record_id, for_update=True)
    return (
        draft is not None
        and draft.draft_epoch == draft_epoch
        and draft.last_update_id == last_update_id
    )


async def draft_is_current(session: AsyncSession, record_id: UUID, draft_epoch: int) -> bool:
    """Return whether the record's draft row exists with exactly this epoch."""
    found = await session.scalar(
        select(CrdtDraft.record_id).where(
            CrdtDraft.record_id == record_id, CrdtDraft.draft_epoch == draft_epoch
        )
    )
    return found is not None


async def load_updates(
    session: AsyncSession, record_id: UUID, draft_epoch: int
) -> list[tuple[int, bytes]]:
    """Return the draft's not yet compacted updates as ``(id, update)``, oldest first."""
    rows = await session.execute(
        select(CrdtUpdate.id, CrdtUpdate.update)
        .where(CrdtUpdate.record_id == record_id, CrdtUpdate.draft_epoch == draft_epoch)
        .order_by(CrdtUpdate.id)
    )
    return [(row[0], bytes(row[1])) for row in rows]


async def insert_draft(
    session: AsyncSession,
    record_id: UUID,
    *,
    draft_epoch: int,
    base_version: int,
    state: bytes,
    created_by: UUID | None,
) -> None:
    """Insert the draft row of a fresh hydration and drop update rows of earlier drafts.

    Raises ``IntegrityError`` if the record already has a draft.
    """
    await session.execute(delete(CrdtUpdate).where(CrdtUpdate.record_id == record_id))
    await session.execute(
        insert(CrdtDraft).values(
            record_id=record_id,
            draft_epoch=draft_epoch,
            base_version=base_version,
            state=state,
            created_by=created_by,
        )
    )


async def append_update(
    session: AsyncSession,
    record_id: UUID,
    draft_epoch: int,
    update_bytes: bytes,
    user_id: UUID | None,
) -> int | None:
    """Append one update to the draft's log; return its id, or ``None`` if the draft is gone.

    Two statements. The first locks the ``records`` row ``FOR KEY SHARE``, the lock the foreign
    key of ``crdt_updates`` needs anyway, taken first to keep the lock order (a publish holding
    the row ``FOR UPDATE`` makes this wait here, holding nothing). The second locks the draft
    row, sets ``last_activity_at`` and ``last_update_id``, adds ``user_id`` to ``contributors``
    and inserts the ``crdt_updates`` row, all only if the draft row still has ``draft_epoch``.
    The caller commits **before** it applies or broadcasts the update (B5).
    """
    await session.execute(
        select(Record.id).where(Record.id == record_id).with_for_update(read=True, key_share=True)
    )
    values: dict[str, Any] = {
        "last_activity_at": func.now(),
        # The id the insert below uses, so that the draft row names its last update.
        "last_update_id": func.nextval(
            func.pg_get_serial_sequence(CrdtUpdate.__tablename__, "id"), type_=BigInteger
        ),
    }
    if user_id is not None:
        user = literal(user_id, Uuid)
        values["contributors"] = case(
            (user == any_(CrdtDraft.contributors), CrdtDraft.contributors),
            else_=func.array_append(CrdtDraft.contributors, user, type_=ARRAY(Uuid)),
        )
    touched = (
        update(CrdtDraft)
        .where(CrdtDraft.record_id == record_id, CrdtDraft.draft_epoch == draft_epoch)
        .values(**values)
        .returning(CrdtDraft.record_id, CrdtDraft.last_update_id)
        .cte("touched")
    )
    statement = (
        insert(CrdtUpdate)
        .from_select(
            ["id", "record_id", "draft_epoch", "update"],
            select(
                touched.c.last_update_id,
                touched.c.record_id,
                literal(draft_epoch, Integer),
                literal(update_bytes, LargeBinary),
            ),
        )
        .returning(CrdtUpdate.id)
    )
    update_id: int | None = await session.scalar(statement)
    return update_id


async def compact(
    session: AsyncSession,
    record_id: UUID,
    draft_epoch: int,
    state: bytes,
    through_update_id: int,
) -> bool:
    """Store the compacted state and delete the updates it contains; ``False`` if superseded.

    ``state`` must contain every update with ``id <= through_update_id``. ``last_activity_at``
    is left alone: compaction is not an edit.
    """
    touched = await session.scalar(
        update(CrdtDraft)
        .where(CrdtDraft.record_id == record_id, CrdtDraft.draft_epoch == draft_epoch)
        .values(state=state)
        .returning(CrdtDraft.record_id)
    )
    if touched is None:
        return False
    await session.execute(
        delete(CrdtUpdate).where(
            CrdtUpdate.record_id == record_id, CrdtUpdate.id <= through_update_id
        )
    )
    return True


async def delete_draft(
    session: AsyncSession,
    record_id: UUID,
    *,
    only_epoch: int | None = None,
    idle_for: timedelta | None = None,
) -> int | None:
    """Delete the record's draft and updates and end the draft's epoch.

    Returns ``records.draft_epoch`` afterwards (``None`` if the record does not exist). If a
    draft row was deleted, ``records.draft_epoch`` is raised to one more than that draft's epoch
    unless it is already greater. Without a draft row nothing is incremented.

    With ``only_epoch`` the draft is deleted only if it has that epoch; a draft of another
    epoch (a newer one, opened since the caller looked) is left alone.

    With ``idle_for`` the draft is deleted only if its ``last_activity_at`` is longer ago than
    that on the database's clock (the draft reaper, decision B7). The statement that locks the
    draft row evaluates the condition, so it queues behind an ``append_update`` or an extend in
    flight and then sees the activity that one recorded. An edit therefore either commits first
    and keeps the draft, or comes after the delete and finds its draft gone
    (``append_update`` returns ``None``); none is deleted with the draft.

    The publish transaction (Step 1.10) increments ``records.draft_epoch`` itself and then calls
    this; in that order the epoch goes up by exactly one. ``records.updated_at`` is not touched.
    The ``records`` row is locked first (lock order, see the module docstring).
    """
    current: int | None = await session.scalar(
        select(Record.draft_epoch).where(Record.id == record_id).with_for_update(key_share=True)
    )
    if current is None:
        return None  # no record, so no draft rows either (they cascade)
    if only_epoch is not None or idle_for is not None:
        conditions = [CrdtDraft.record_id == record_id]
        if only_epoch is not None:
            conditions.append(CrdtDraft.draft_epoch == only_epoch)
        if idle_for is not None:
            conditions.append(CrdtDraft.last_activity_at < func.now() - literal(idle_for, Interval))
        # FOR UPDATE: having waited for a concurrent writer of the row, PostgreSQL evaluates the
        # conditions again on the row as that writer committed it (READ COMMITTED).
        meant = await session.scalar(
            select(CrdtDraft.record_id).where(*conditions).with_for_update()
        )
        if meant is None:
            return current
    epoch: int | None = await session.scalar(
        delete(CrdtDraft).where(CrdtDraft.record_id == record_id).returning(CrdtDraft.draft_epoch)
    )
    await session.execute(delete(CrdtUpdate).where(CrdtUpdate.record_id == record_id))
    if epoch is not None and current <= epoch:
        current = epoch + 1
        await session.execute(
            update(Record)
            .where(Record.id == record_id)
            # updated_at names itself so that its onupdate default does not fire.
            .values(draft_epoch=current, updated_at=Record.updated_at)
            .execution_options(synchronize_session=False)
        )
    return current
