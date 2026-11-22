"""Seed the roles of spec §10.4 with their permissions (spec §8.2).

Phase 1, Step 1.7. All seven roles are seeded now so that later phases do not reseed:
``Viewer``, ``Collaborator``, ``Editor``, ``Vocabulary Admin``, ``System Admin``,
``Tier 1 Agent``, ``Tier 2 Agent``. The values are a frozen copy of
``lae.auth.permissions.ROLE_PERMISSIONS`` (a test keeps the two equal); a later change to a
role's permissions needs a new migration.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
    "manage_vocabularies",
    "manage_roles",
    "manage_users",
    "manage_backups",
    "manage_system",
    "view_analytics",
]

ROLES: dict[str, list[str]] = {
    "Viewer": [],
    "Collaborator": ["edit_draft"],
    "Editor": ["edit_draft", "publish", "delete", "merge", "unmerge", "protect"],
    "Vocabulary Admin": ["manage_vocabularies"],
    "System Admin": ALL_PERMISSIONS,
    "Tier 1 Agent": ["edit_draft", "propose_merge"],
    "Tier 2 Agent": ["review_proposal", "approve_merge_scoped"],
}

roles = sa.table(
    "roles",
    sa.column("name", sa.Text()),
    sa.column("permissions", postgresql.ARRAY(sa.Text())),
)


def upgrade() -> None:
    """Insert the seven roles."""
    op.bulk_insert(roles, [{"name": name, "permissions": perms} for name, perms in ROLES.items()])


def downgrade() -> None:
    """Delete the seeded roles (their ``user_roles`` rows cascade)."""
    op.execute(roles.delete().where(roles.c.name.in_(list(ROLES))))
