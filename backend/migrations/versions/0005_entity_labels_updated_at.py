"""Index ``entity_labels.updated_at`` for the autocomplete overlay (Phase 1 Step 1.13).

Spike S1 found that QLever's prefix search never sees literals inserted by SPARQL Update, so
autocomplete adds the ``entity_labels`` rows changed since the live index was built (``updated_at``
after ``sync_state.qlever_index_watermark_time``) on every request. This index keeps that a range
scan over the recent rows instead of a scan of the whole table.

Revision ID: 0005
Revises: 0003
Create Date: 2026-11-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_entity_labels_updated_at"), "entity_labels", ["updated_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_entity_labels_updated_at"), table_name="entity_labels")
