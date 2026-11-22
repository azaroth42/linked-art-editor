"""Insert and read rows through ``lae.db.models`` on the migrated test database."""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils.compat import uuid7

from lae.db.models import (
    ApiToken,
    CrdtDraft,
    CrdtUpdate,
    EntityLabel,
    EntityLabelStatus,
    OutboxEvent,
    OutboxEventKind,
    OutboxEventStatus,
    Record,
    RecordRevision,
    RecordStatus,
    Role,
    SyncState,
    User,
    UserKind,
    UserRole,
)

BASE_URI = "http://localhost:8000/id/"


def new_record(record_type: str = "HumanMadeObject") -> Record:
    """A record row with the skeleton JSON-LD of Phase 1 Step 1.6."""
    record_id = uuid7()
    uri = f"{BASE_URI}{record_id}"
    data = {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": uri,
        "type": record_type,
        "_label": "Night Watch",
    }
    return Record(id=record_id, uri=uri, type=record_type, etag="W/1", data=data, context_version=1)


async def test_insert_and_read_record(db_session: AsyncSession) -> None:
    record = new_record()
    db_session.add(record)
    await db_session.commit()
    db_session.expunge_all()

    loaded = await db_session.scalar(select(Record).where(Record.uri == record.uri))
    assert loaded is not None
    assert loaded.id == record.id
    assert loaded.type == "HumanMadeObject"
    assert loaded.data == record.data
    assert loaded.version == 0
    assert loaded.status is RecordStatus.ACTIVE
    assert loaded.context_version == 1
    assert loaded.merged_into is None
    assert isinstance(loaded.created_at, datetime)
    assert loaded.created_at.tzinfo is not None
    assert loaded.updated_at == loaded.created_at


async def test_record_ids_default_to_uuid7(db_session: AsyncSession) -> None:
    record = Record(
        uri=f"{BASE_URI}{uuid.uuid4()}", type="Place", etag="W/1", data={}, context_version=1
    )
    db_session.add(record)
    await db_session.flush()
    assert isinstance(record.id, uuid.UUID)
    assert record.id.version == 7


async def test_record_uri_is_unique(db_session: AsyncSession) -> None:
    first = new_record()
    duplicate = new_record()
    duplicate.uri = first.uri
    db_session.add(first)
    await db_session.flush()
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_record_status_and_merge_target(db_session: AsyncSession) -> None:
    target, merged = new_record("Person"), new_record("Person")
    merged.status = RecordStatus.MERGED
    merged.merged_into = target.id
    db_session.add_all([target, merged])
    await db_session.commit()
    db_session.expunge_all()

    loaded = await db_session.get(Record, merged.id)
    assert loaded is not None
    assert loaded.status is RecordStatus.MERGED
    assert loaded.merged_into == target.id


async def test_revision_round_trip_and_unique_version(db_session: AsyncSession) -> None:
    record = new_record()
    editor, agent = uuid7(), uuid7()
    db_session.add(record)
    await db_session.flush()  # no ORM relationships, so insert the parent first
    revision = RecordRevision(
        record_id=record.id,
        version=1,
        ntriples_zstd=b"\x28\xb5\x2f\xfd triples",
        jsonld_zstd=b"\x28\xb5\x2f\xfd jsonld",
        context_version=1,
        authors=[editor, agent],
        published_by=editor,
        summary="First publish",
        accepted_warnings=[{"code": "LAE-SHACL-001", "path": "/identified_by/0"}],
    )
    db_session.add(revision)
    await db_session.commit()
    db_session.expunge_all()

    loaded = await db_session.scalar(
        select(RecordRevision).where(RecordRevision.record_id == record.id)
    )
    assert loaded is not None
    assert isinstance(loaded.id, int)
    assert loaded.ntriples_zstd == revision.ntriples_zstd
    assert loaded.authors == [editor, agent]
    assert loaded.accepted_warnings == [{"code": "LAE-SHACL-001", "path": "/identified_by/0"}]
    assert loaded.merge_batch_id is None
    assert loaded.split_batch_id is None

    db_session.add(
        RecordRevision(
            record_id=record.id,
            version=1,
            ntriples_zstd=b"",
            jsonld_zstd=b"",
            context_version=1,
            published_by=editor,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_revision_requires_existing_record(db_session: AsyncSession) -> None:
    db_session.add(
        RecordRevision(
            record_id=uuid7(),
            version=1,
            ntriples_zstd=b"",
            jsonld_zstd=b"",
            context_version=1,
            published_by=uuid7(),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_entity_label_defaults(db_session: AsyncSession) -> None:
    uri = "http://vocab.getty.edu/aat/300404670"
    db_session.add(EntityLabel(uri=uri, type="Type", default_label="primary name"))
    await db_session.commit()
    db_session.expunge_all()

    label = await db_session.get(EntityLabel, uri)
    assert label is not None
    assert label.labels == {}
    assert label.metatype_uris == []
    assert label.vocab_ids == []
    assert label.primary_classification is None
    assert label.source == "local"
    assert label.status is EntityLabelStatus.PUBLISHED


async def test_entity_label_requires_default_label(db_session: AsyncSession) -> None:
    db_session.add(EntityLabel(uri="urn:x", type="Type", default_label=None))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_crdt_rows_are_deleted_with_their_record(db_session: AsyncSession) -> None:
    record = new_record()
    user_id = uuid7()
    db_session.add(record)
    await db_session.flush()  # no ORM relationships, so insert the parent first
    db_session.add(
        CrdtDraft(
            record_id=record.id,
            draft_epoch=0,
            base_version=0,
            state=b"\x00\x00",
            contributors=[user_id],
            created_by=user_id,
        )
    )
    db_session.add_all(
        [CrdtUpdate(record_id=record.id, draft_epoch=0, update=bytes([n])) for n in range(3)]
    )
    await db_session.commit()
    db_session.expunge_all()

    draft = await db_session.get(CrdtDraft, record.id)
    assert draft is not None
    assert draft.contributors == [user_id]
    updates = (
        await db_session.scalars(
            select(CrdtUpdate.update)
            .where(CrdtUpdate.record_id == record.id)
            .order_by(CrdtUpdate.id)
        )
    ).all()
    assert updates == [b"\x00", b"\x01", b"\x02"]

    # The reaper deletes never-published records together with their drafts (Step 1.15).
    await db_session.execute(delete(Record).where(Record.id == record.id))
    assert await db_session.get(CrdtDraft, record.id, populate_existing=True) is None
    remaining = await db_session.scalar(
        select(func.count()).select_from(CrdtUpdate).where(CrdtUpdate.record_id == record.id)
    )
    assert remaining == 0


async def test_outbox_event_defaults(db_session: AsyncSession) -> None:
    event = OutboxEvent(
        kind=OutboxEventKind.PUBLISH,
        record_id=uuid7(),
        from_version=1,
        to_version=2,
        payload={"context_version": 1, "drift_check": False},
    )
    db_session.add(event)
    await db_session.commit()
    db_session.expunge_all()

    loaded = await db_session.get(OutboxEvent, event.id)
    assert loaded is not None
    assert loaded.status is OutboxEventStatus.PENDING
    assert loaded.attempts == 0
    assert loaded.last_error is None
    assert loaded.processed_at is None
    assert loaded.payload == {"context_version": 1, "drift_check": False}


async def test_sync_state_holds_json(db_session: AsyncSession) -> None:
    db_session.add(SyncState(key="qlever_index_watermark", value=42))
    await db_session.commit()
    db_session.expunge_all()

    state = await db_session.get(SyncState, "qlever_index_watermark")
    assert state is not None
    assert state.value == 42


async def test_users_roles_and_tokens(db_session: AsyncSession) -> None:
    user = User(subject="oidc|abc", display_name="Ada", email="ada@example.org")
    agent = User(display_name="tier1-sweeper", kind=UserKind.AGENT)
    # Not a seeded role name (migration 0002 seeds Viewer, Collaborator, Editor, ...).
    role = Role(name="Test Editor", permissions=["edit_draft", "publish"])
    db_session.add_all([user, agent, role])
    await db_session.flush()
    assert user.id.version == 7
    db_session.add(UserRole(user_id=user.id, role_id=role.id))
    token = ApiToken(user_id=user.id, name="etl", token_hash="0" * 64, scopes=["edit_draft"])
    db_session.add(token)
    await db_session.commit()
    db_session.expunge_all()

    loaded = await db_session.get(User, user.id)
    assert loaded is not None
    assert loaded.kind is UserKind.HUMAN
    assert loaded.is_active is True
    loaded_agent = await db_session.get(User, agent.id)
    assert loaded_agent is not None
    assert loaded_agent.subject is None
    role_names = (
        await db_session.scalars(
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
        )
    ).all()
    assert role_names == ["Test Editor"]
    loaded_token = await db_session.scalar(select(ApiToken).where(ApiToken.token_hash == "0" * 64))
    assert loaded_token is not None
    assert loaded_token.scopes == ["edit_draft"]
    assert loaded_token.expires_at is None
    assert loaded_token.last_used_at is None

    await db_session.execute(delete(User).where(User.id == user.id))
    assert await db_session.scalar(select(func.count()).select_from(ApiToken)) == 0
    assert await db_session.scalar(select(func.count()).select_from(UserRole)) == 0


async def test_user_subject_is_unique(db_session: AsyncSession) -> None:
    db_session.add(User(subject="oidc|dup", display_name="One"))
    await db_session.flush()
    db_session.add(User(subject="oidc|dup", display_name="Two"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
