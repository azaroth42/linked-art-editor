"""SQLAlchemy 2 declarative models for the core schema (migration ``0001_core``).

The migrations under ``backend/migrations/`` are the source of truth for the database; these
models mirror them exactly (``tests/db/test_migrations.py`` compares the two). Things the ORM
cannot express are only in the migration: TOAST ``lz4`` compression of ``records.data`` and the
``outbox_events`` ``NOTIFY`` trigger.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from uuid_utils.compat import uuid7

# Deterministic constraint and index names, so migrations can refer to them by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Server-side defaults shared by several columns.
NOW = func.now()
EMPTY_JSON_OBJECT = text("'{}'::jsonb")
EMPTY_JSON_ARRAY = text("'[]'::jsonb")
EMPTY_ARRAY = text("'{}'")


class Base(DeclarativeBase):
    """Declarative base whose metadata uses :data:`NAMING_CONVENTION`."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pg_enum(enum_class: type[StrEnum], name: str) -> Enum:
    """Postgres enum type ``name`` storing the members' values (not their names)."""
    return Enum(
        enum_class,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


class RecordStatus(StrEnum):
    """``records.status``: tombstones keep their row (spec §3.4)."""

    ACTIVE = "active"
    DELETED = "deleted"
    MERGED = "merged"


class EntityLabelStatus(StrEnum):
    """``entity_labels.status``."""

    PUBLISHED = "published"
    DRAFT = "draft"


class OutboxEventKind(StrEnum):
    """``outbox_events.kind``."""

    PUBLISH = "publish"
    DELETE = "delete"
    MERGE = "merge"
    SPLIT = "split"


class OutboxEventStatus(StrEnum):
    """``outbox_events.status``; ``failed`` is the dead letter state (Phase 1 Step 1.11)."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class UserKind(StrEnum):
    """``users.kind``: agents are service accounts (Phase 3 Step 3.4)."""

    HUMAN = "human"
    AGENT = "agent"


class Record(Base):
    """A published (or never-published, ``version = 0``) Linked Art record."""

    __tablename__ = "records"
    # Migration 0007: the draft reaper finds never-published records by age (Step 1.15).
    __table_args__ = (
        Index(
            "ix_records_unpublished_created_at",
            "created_at",
            postgresql_where=text("version = 0"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    uri: Mapped[str] = mapped_column(Text, unique=True)
    type: Mapped[str] = mapped_column(Text, index=True)
    version: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[RecordStatus] = mapped_column(
        _pg_enum(RecordStatus, "records_status"), server_default=RecordStatus.ACTIVE.value
    )
    etag: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    context_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=NOW, onupdate=NOW
    )
    merged_into: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Migration 0006: epoch of the current or next draft; bumped by publish and discard (B6).
    draft_epoch: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class RecordRevision(Base):
    """One published version of a record: zstd N-Triples and JSON-LD (append-only)."""

    __tablename__ = "record_revisions"
    __table_args__ = (UniqueConstraint("record_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("records.id"))
    version: Mapped[int] = mapped_column(Integer)
    ntriples_zstd: Mapped[bytes] = mapped_column(LargeBinary)
    jsonld_zstd: Mapped[bytes] = mapped_column(LargeBinary)
    context_version: Mapped[int] = mapped_column(Integer)
    authors: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), server_default=EMPTY_ARRAY)
    published_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    summary: Mapped[str] = mapped_column(Text, server_default="")
    accepted_warnings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=EMPTY_JSON_ARRAY
    )
    merge_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    split_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class EntityLabel(Base):
    """Label lookup row for a local record or a sidecar entity (spec §3.1, §3.2)."""

    __tablename__ = "entity_labels"
    # Migration 0005: the autocomplete overlay reads the rows changed since the index was built.
    __table_args__ = (Index("ix_entity_labels_updated_at", "updated_at"),)

    uri: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text)
    labels: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=EMPTY_JSON_OBJECT)
    default_label: Mapped[str] = mapped_column(Text)
    primary_classification: Mapped[str | None] = mapped_column(Text)
    metatype_uris: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARRAY)
    vocab_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARRAY)
    source: Mapped[str] = mapped_column(Text, server_default="local")
    status: Mapped[EntityLabelStatus] = mapped_column(
        _pg_enum(EntityLabelStatus, "entity_labels_status"),
        server_default=EntityLabelStatus.PUBLISHED.value,
    )
    # Not in the Step 0.5 column list: the Step 1.13 freshness fallback filters on it.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=NOW, onupdate=NOW
    )


class CrdtDraft(Base):
    """Compacted Yjs state of the one live draft of a record (decision B5)."""

    __tablename__ = "crdt_drafts"

    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("records.id", ondelete="CASCADE"), primary_key=True
    )
    draft_epoch: Mapped[int] = mapped_column(Integer)
    base_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[bytes] = mapped_column(LargeBinary)
    contributors: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), server_default=EMPTY_ARRAY)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    last_update_id: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    """Id of the last ``crdt_updates`` row appended to this draft; compaction keeps it."""


class CrdtUpdate(Base):
    """One Yjs update, persisted before broadcast and folded into ``crdt_drafts.state``."""

    __tablename__ = "crdt_updates"
    __table_args__ = (Index("ix_crdt_updates_record_id_id", "record_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("records.id", ondelete="CASCADE"))
    draft_epoch: Mapped[int] = mapped_column(Integer)
    update: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class OutboxEvent(Base):
    """Transactional outbox row; an insert trigger sends ``NOTIFY outbox, '<id>'``."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index(
            "ix_outbox_events_pending",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[OutboxEventKind] = mapped_column(_pg_enum(OutboxEventKind, "outbox_events_kind"))
    record_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    from_version: Mapped[int | None] = mapped_column(Integer)
    to_version: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSON_OBJECT)
    status: Mapped[OutboxEventStatus] = mapped_column(
        _pg_enum(OutboxEventStatus, "outbox_events_status"),
        server_default=OutboxEventStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncState(Base):
    """Key/value state of the sync pipeline, e.g. ``qlever_index_watermark``."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)


class User(Base):
    """A human (OIDC) or agent (service account) identity."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    subject: Mapped[str | None] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[UserKind] = mapped_column(
        _pg_enum(UserKind, "users_kind"), server_default=UserKind.HUMAN.value
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=NOW, onupdate=NOW
    )


class Role(Base):
    """A named set of permissions (spec §8.2); seeded by migration ``0002_auth_seed``."""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARRAY)


class UserRole(Base):
    """Membership of a user in a role."""

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class ApiToken(Base):
    """A Personal Access Token or service-account token, stored as its SHA-256 hex digest."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARRAY)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
