"""Add ``records.draft_epoch`` (decision B6, Phase 1 Step 1.8).

The epoch of the record's current (or next) draft. A ``crdt_drafts`` row is created with this
value; publishing and discarding a draft increment it, so a client holding a document of an
earlier draft is refused with WebSocket close code 4409 (``lae.crdt``). A constant default makes
the ``ADD COLUMN`` a metadata-only change, whatever the size of ``records``.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "records",
        sa.Column("draft_epoch", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("records", "draft_epoch")
