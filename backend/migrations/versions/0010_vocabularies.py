"""Vocabulary registry: Value Sets, their terms, slot bindings, rule membership (Phase 2 Step 2.1).

Creates the tables of spec §7 that ``profiles/vocab/*.yaml`` loads into (``lae vocab import``):

- ``vocabularies``: one row per Value Set (``mode`` enumerated or rule, ``rule`` JSONB with
  ``metatype_uris``, ``broader_roots``, ``exclusions``, ``classes``, ``enforcement``).
- ``vocabulary_terms``: the ordered terms of a set (the pinned shortlist of a rule set). ``flags``
  holds the term flags of ``profiles/vocab/_schema.json`` (``{"unique": true}``); Step 2.10 reads
  it. It is created here so that import and export are lossless from the start.
- ``slot_vocabulary_bindings``: ``(entity_class, property_path, vocabulary_id)`` is unique, so
  several Value Sets can bind to one path (register C4; ``docs/questions.md``, Step 0.12).
  ``enforcement`` null means "inherit the set's". It reuses the ``vocabularies_enforcement`` type.
  A binding keeps its Value Set from being deleted (no cascade; Step 2.4 answers 409).
- ``vocabulary_membership``: the materialized membership of each set (``lae.vocab.membership``).

On ``entity_labels`` it adds ``usage_count`` (refreshed nightly from QLever,
``lae.vocab.usage``) and GIN indexes on ``vocab_ids`` and ``metatype_uris``.

``down_revision`` is the single head of ``main`` when this step merged (agent-workflow §4).

Revision ID: 0010
Revises: 0005
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODE = postgresql.ENUM("enumerated", "rule", name="vocabularies_mode", create_type=False)
ENFORCEMENT = postgresql.ENUM(
    "strict", "extensible", "advisory", name="vocabularies_enforcement", create_type=False
)
# The naming convention's name (uq_<table>_<columns>) would exceed PostgreSQL's 63 characters.
UNIQUE_BINDING = "uq_slot_vocabulary_bindings_class_path_vocabulary"


def _vocabulary_fk(table: str) -> sa.ForeignKeyConstraint:
    """``<table>.vocabulary_id`` → ``vocabularies.id``, removed with its Value Set."""
    return sa.ForeignKeyConstraint(
        ["vocabulary_id"],
        ["vocabularies.id"],
        name=op.f(f"fk_{table}_vocabulary_id_vocabularies"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    """Create the vocabulary tables and enums; extend ``entity_labels``."""
    bind = op.get_bind()
    MODE.create(bind)
    ENFORCEMENT.create(bind)

    op.create_table(
        "vocabularies",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scope_note", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("mode", MODE, nullable=False),
        sa.Column("rule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("enforcement", ENFORCEMENT, nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vocabularies")),
    )

    op.create_table(
        "vocabulary_terms",
        sa.Column("vocabulary_id", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deprecated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("scope_note", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("label_override", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _vocabulary_fk("vocabulary_terms"),
        sa.PrimaryKeyConstraint("vocabulary_id", "uri", name=op.f("pk_vocabulary_terms")),
    )

    op.create_table(
        "slot_vocabulary_bindings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("entity_class", sa.Text(), nullable=False),
        sa.Column("property_path", sa.Text(), nullable=False),
        sa.Column("vocabulary_id", sa.Text(), nullable=False),
        sa.Column("enforcement", ENFORCEMENT, nullable=True),
        sa.ForeignKeyConstraint(
            ["vocabulary_id"],
            ["vocabularies.id"],
            name=op.f("fk_slot_vocabulary_bindings_vocabulary_id_vocabularies"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slot_vocabulary_bindings")),
        sa.UniqueConstraint(
            "entity_class",
            "property_path",
            "vocabulary_id",
            name=op.f(UNIQUE_BINDING),
        ),
    )

    op.create_table(
        "vocabulary_membership",
        sa.Column("vocabulary_id", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _vocabulary_fk("vocabulary_membership"),
        sa.PrimaryKeyConstraint("vocabulary_id", "uri", name=op.f("pk_vocabulary_membership")),
    )

    op.add_column(
        "entity_labels",
        sa.Column("usage_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index(
        op.f("ix_entity_labels_vocab_ids"),
        "entity_labels",
        ["vocab_ids"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        op.f("ix_entity_labels_metatype_uris"),
        "entity_labels",
        ["metatype_uris"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    """Drop everything ``upgrade`` created."""
    op.drop_index(op.f("ix_entity_labels_metatype_uris"), table_name="entity_labels")
    op.drop_index(op.f("ix_entity_labels_vocab_ids"), table_name="entity_labels")
    op.drop_column("entity_labels", "usage_count")
    op.drop_table("vocabulary_membership")
    op.drop_table("slot_vocabulary_bindings")
    op.drop_table("vocabulary_terms")
    op.drop_table("vocabularies")
    bind = op.get_bind()
    ENFORCEMENT.drop(bind)
    MODE.drop(bind)
