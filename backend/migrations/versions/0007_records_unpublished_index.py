"""Index never-published records by ``created_at`` for the draft reaper (Phase 1 Step 1.15).

The hourly reaper (``lae.scheduler.reaper``) deletes never-published records (``version = 0``)
that have no draft and were created longer than ``draft_ttl_days`` ago. They are a handful
among all records (bulk-loaded and published ones have ``version >= 1``), so a partial index on
``created_at`` where ``version = 0`` keeps that query a small index scan instead of an hourly
scan of the whole ``records`` table.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_records_unpublished_created_at",
        "records",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("version = 0"),
    )


def downgrade() -> None:
    op.drop_index("ix_records_unpublished_created_at", table_name="records")
