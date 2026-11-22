"""Core schema: records, revisions, labels, CRDT drafts, outbox, sync state, auth.

Phase 0, Step 0.5. Generated with ``alembic revision --autogenerate`` from ``lae.db.models`` and
hand-checked: enum types are created and dropped explicitly, ``records.data`` uses TOAST ``lz4``
compression (decision A8), and inserts into ``outbox_events`` send ``NOTIFY outbox, '<id>'``
(decision A2). Enum values are frozen here; later changes need a new migration.

Revision ID: 0001
Revises:
Create Date: 2026-11-22
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS: dict[str, tuple[str, ...]] = {
    "records_status": ("active", "deleted", "merged"),
    "entity_labels_status": ("published", "draft"),
    "outbox_events_kind": ("publish", "delete", "merge", "split"),
    "outbox_events_status": ("pending", "done", "failed"),
    "users_kind": ("human", "agent"),
}


def _enum(name: str) -> postgresql.ENUM:
    """Column type for an enum created in :func:`upgrade` (not re-created per table)."""
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _uuid_array(name: str) -> sa.Column[Any]:
    return sa.Column(
        name, postgresql.ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False
    )


def _text_array(name: str) -> sa.Column[Any]:
    return sa.Column(
        name, postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
    )


def upgrade() -> None:
    """Create the core tables, enum types, lz4 compression, and the outbox trigger."""
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind)

    op.create_table(
        "records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", _enum("records_status"), server_default="active", nullable=False),
        sa.Column("etag", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context_version", sa.Integer(), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("merged_into", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_records")),
        sa.UniqueConstraint("uri", name=op.f("uq_records_uri")),
    )
    op.create_index(op.f("ix_records_type"), "records", ["type"], unique=False)
    # TOAST compression applies to values stored out of line (spec §3.1, decision A8).
    op.execute("ALTER TABLE records ALTER COLUMN data SET COMPRESSION lz4")

    op.create_table(
        "record_revisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("ntriples_zstd", sa.LargeBinary(), nullable=False),
        sa.Column("jsonld_zstd", sa.LargeBinary(), nullable=False),
        sa.Column("context_version", sa.Integer(), nullable=False),
        _uuid_array("authors"),
        sa.Column("published_by", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "accepted_warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("merge_batch_id", sa.Uuid(), nullable=True),
        sa.Column("split_batch_id", sa.Uuid(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["record_id"], ["records.id"], name=op.f("fk_record_revisions_record_id_records")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_record_revisions")),
        sa.UniqueConstraint(
            "record_id", "version", name=op.f("uq_record_revisions_record_id_version")
        ),
    )

    op.create_table(
        "entity_labels",
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("default_label", sa.Text(), nullable=False),
        sa.Column("primary_classification", sa.Text(), nullable=True),
        _text_array("metatype_uris"),
        _text_array("vocab_ids"),
        sa.Column("source", sa.Text(), server_default="local", nullable=False),
        sa.Column(
            "status", _enum("entity_labels_status"), server_default="published", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("uri", name=op.f("pk_entity_labels")),
    )

    op.create_table(
        "crdt_drafts",
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("draft_epoch", sa.Integer(), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.LargeBinary(), nullable=False),
        _uuid_array("contributors"),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["records.id"],
            name=op.f("fk_crdt_drafts_record_id_records"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("record_id", name=op.f("pk_crdt_drafts")),
    )

    op.create_table(
        "crdt_updates",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("draft_epoch", sa.Integer(), nullable=False),
        sa.Column("update", sa.LargeBinary(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["records.id"],
            name=op.f("fk_crdt_updates_record_id_records"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crdt_updates")),
    )
    op.create_index(
        "ix_crdt_updates_record_id_id", "crdt_updates", ["record_id", "id"], unique=False
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", _enum("outbox_events_kind"), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=True),
        sa.Column("to_version", sa.Integer(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status", _enum("outbox_events_status"), server_default="pending", nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
    )
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    # The sync worker LISTENs on `outbox` and also polls (Phase 1 Step 1.11).
    op.execute(
        """
        CREATE FUNCTION outbox_events_notify() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('outbox', NEW.id::text);
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER outbox_events_notify
        AFTER INSERT ON outbox_events
        FOR EACH ROW EXECUTE FUNCTION outbox_events_notify()
        """
    )

    op.create_table(
        "sync_state",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_sync_state")),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("kind", _enum("users_kind"), server_default="human", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("subject", name=op.f("uq_users_subject")),
    )

    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _text_array("permissions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("name", name=op.f("uq_roles_name")),
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_user_roles_role_id_roles"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_roles_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_roles")),
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        _text_array("scopes"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_api_tokens_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )
    op.create_index(op.f("ix_api_tokens_user_id"), "api_tokens", ["user_id"], unique=False)


def downgrade() -> None:
    """Drop everything :func:`upgrade` created, in reverse order."""
    op.drop_table("api_tokens")
    op.drop_table("user_roles")
    op.drop_table("roles")
    op.drop_table("users")
    op.drop_table("sync_state")
    op.execute("DROP TRIGGER outbox_events_notify ON outbox_events")
    op.execute("DROP FUNCTION outbox_events_notify()")
    op.drop_table("outbox_events")
    op.drop_table("crdt_updates")
    op.drop_table("crdt_drafts")
    op.drop_table("entity_labels")
    op.drop_table("record_revisions")
    op.drop_table("records")

    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
