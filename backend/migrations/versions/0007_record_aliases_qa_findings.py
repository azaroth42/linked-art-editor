"""Add ``record_aliases`` and ``qa_findings`` (ADR-0030: bulk loads re-mint and flag).

``record_aliases`` keeps every URI a re-minted record was known by, so that later loads, lookups
and redirects resolve it (``lae.aliases``). ``qa_findings`` has the columns of Phase 3 Step 3.10;
it is created now because ``lae bootstrap load`` writes the JSON Schema failures of the records
it loads anyway. Step 3.10 adds ``curation_campaigns`` and the sweeps, not this table.

A finding is keyed by ``(code, record_id, node_id)``; the unique index treats a missing
``node_id`` as a value (``NULLS NOT DISTINCT``, PostgreSQL 15+), so the key also holds for
findings about a whole record.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS: dict[str, tuple[str, ...]] = {
    "qa_findings_severity": ("info", "warning", "error"),
    "qa_findings_status": ("open", "claimed", "resolved", "dismissed"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def _timestamp(name: str) -> sa.Column[sa.DateTime]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind)

    op.create_table(
        "record_aliases",
        sa.Column("alias_uri", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["records.id"],
            name=op.f("fk_record_aliases_record_id_records"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("alias_uri", name=op.f("pk_record_aliases")),
    )
    op.create_index(
        op.f("ix_record_aliases_record_id"), "record_aliases", ["record_id"], unique=False
    )

    op.create_table(
        "qa_findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("severity", _enum("qa_findings_severity"), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=True),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", _enum("qa_findings_status"), server_default="open", nullable=False),
        sa.Column("claimed_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("sweep_run_id", sa.Uuid(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["records.id"],
            name=op.f("fk_qa_findings_record_id_records"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["claimed_by"],
            ["users.id"],
            name=op.f("fk_qa_findings_claimed_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name=op.f("fk_qa_findings_resolved_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qa_findings")),
    )
    op.create_index(op.f("ix_qa_findings_record_id"), "qa_findings", ["record_id"], unique=False)
    op.create_index("ix_qa_findings_status_code", "qa_findings", ["status", "code"], unique=False)
    op.create_index(
        "uq_qa_findings_code_record_id_node_id",
        "qa_findings",
        ["code", "record_id", "node_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_table("qa_findings")
    op.drop_table("record_aliases")
    bind = op.get_bind()
    for name in ENUMS:
        postgresql.ENUM(name=name).drop(bind)
