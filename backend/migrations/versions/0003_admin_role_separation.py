"""Separate System Admin from editorial rights (spec §8.2).

Spec §8.2: "System Admin and Ops administrative permissions are separated cleanly from editorial
publish rights." Migration 0002 seeded System Admin with every permission (the Step 1.7 guide
said "System Admin → everything"); this migration restricts the role to the administrative
permissions. A person who also edits records holds Editor as well (roles are additive).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN_PERMISSIONS = [
    "manage_vocabularies",
    "manage_roles",
    "manage_users",
    "manage_backups",
    "manage_system",
    "view_analytics",
]

ALL_PERMISSIONS = [
    "edit_draft",
    "publish",
    "delete",
    "merge",
    "unmerge",
    "protect",
    "propose_merge",
    "review_proposal",
    "approve_merge_scoped",
    *ADMIN_PERMISSIONS,
]

roles = sa.table(
    "roles",
    sa.column("name", sa.Text()),
    sa.column("permissions", postgresql.ARRAY(sa.Text())),
)


def _set_admin(permissions: list[str]) -> None:
    op.execute(roles.update().where(roles.c.name == "System Admin").values(permissions=permissions))


def upgrade() -> None:
    _set_admin(ADMIN_PERMISSIONS)


def downgrade() -> None:
    _set_admin(ALL_PERMISSIONS)
