"""Allow ``record_revisions.published_by`` to be NULL (Phase 1 Step 1.10).

A bulk-loaded record has no revision row for the version it was loaded at (decision A9). Its
first publish inserts that row in the same transaction, so that ``/id/{uuid}?version=1`` keeps
resolving and the sync worker can read the previous JSON-LD and triples. Nobody published that
version through the editor, so the row has no publisher: NULL, which is what
``GET /api/records/{id}/revisions`` already answers for such a version.

The downgrade has to put a value into those rows before it can restore ``NOT NULL``; it uses
the nil UUID, which is no user's id.

Revision ID: 0008
Revises: 0006
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NIL_UUID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.alter_column("record_revisions", "published_by", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    op.execute(
        f"UPDATE record_revisions SET published_by = '{NIL_UUID}' WHERE published_by IS NULL"
    )
    op.alter_column("record_revisions", "published_by", existing_type=sa.Uuid(), nullable=False)
