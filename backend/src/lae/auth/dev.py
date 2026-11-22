"""Development login bypass: ``LAE_DEV_USER=<email>:<Role>`` (decision E2).

When set, every request without a bearer token runs as that user with exactly that role, so the
editor can be used without an identity provider. The first request upserts a ``users`` row with
``subject = "dev:<email>"`` and replaces its role memberships with the one role; permissions are
then read from ``roles`` like anyone else's. ``lae.auth.setup.check_auth_settings`` refuses to
start the ``api`` process when the value is malformed, names an unknown role, or ``LAE_ENV`` is
``production``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from fastapi import Request
from sqlalchemy import delete, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Uuid

from lae.auth.permissions import ROLE_PERMISSIONS, CurrentUser, user_principal
from lae.db.models import Role, User, UserRole

SUBJECT_PREFIX: Final = "dev:"
"""``users.subject`` of the dev user is this prefix plus the e-mail address."""

_USER_ID_STATE: Final = "lae_dev_user_id"
_LOCK_STATE: Final = "lae_dev_user_lock"


@dataclass(frozen=True, slots=True)
class DevUser:
    """A parsed ``LAE_DEV_USER`` value."""

    email: str
    role: str

    @property
    def subject(self) -> str:
        """The ``users.subject`` of this dev user."""
        return SUBJECT_PREFIX + self.email


def parse_dev_user(value: str) -> DevUser:
    """Parse ``<email>:<Role>``; raise ``ValueError`` if malformed or the role is unknown."""
    email, separator, role = value.partition(":")
    email, role = email.strip(), role.strip()
    if not separator or not email or not role:
        raise ValueError(
            f"LAE_DEV_USER={value!r} must be '<email>:<Role>', e.g. 'editor@example.org:Editor'"
        )
    if role not in ROLE_PERMISSIONS:
        raise ValueError(f"LAE_DEV_USER role {role!r} is not one of {sorted(ROLE_PERMISSIONS)}")
    return DevUser(email=email, role=role)


async def ensure_dev_user(session: AsyncSession, dev: DevUser) -> UUID:
    """Upsert the dev user, give it exactly ``dev.role``, commit, and return its id."""
    user_id: UUID | None = await session.scalar(
        insert(User)
        .values(subject=dev.subject, display_name=dev.email, email=dev.email, is_active=True)
        .on_conflict_do_update(
            index_elements=[User.subject],
            set_={"display_name": dev.email, "email": dev.email, "is_active": True},
        )
        .returning(User.id)
    )
    assert user_id is not None
    role_id = select(Role.id).where(Role.name == dev.role).scalar_subquery()
    await session.execute(
        delete(UserRole).where(
            UserRole.user_id == user_id, UserRole.role_id.is_distinct_from(role_id)
        )
    )
    await session.execute(
        insert(UserRole)
        .from_select(
            ["user_id", "role_id"],
            select(literal(user_id, Uuid), Role.id).where(Role.name == dev.role),
        )
        .on_conflict_do_nothing()
    )
    await session.commit()
    return user_id


def init_dev_state(app_state: object) -> None:
    """Prepare ``app.state`` for the dev user (called once by ``install_auth``)."""
    setattr(app_state, _USER_ID_STATE, None)
    setattr(app_state, _LOCK_STATE, asyncio.Lock())


async def dev_principal(request: Request, session: AsyncSession, value: str) -> CurrentUser:
    """Return the dev user as the caller, upserting it on the first request of the process."""
    state = request.app.state
    user = None
    user_id: UUID | None = getattr(state, _USER_ID_STATE, None)
    if user_id is not None:
        user = await session.get(User, user_id)
    if user is None:
        lock: asyncio.Lock = getattr(state, _LOCK_STATE)
        async with lock:
            user_id = await ensure_dev_user(session, parse_dev_user(value))
            setattr(state, _USER_ID_STATE, user_id)
        user = await session.get(User, user_id)
        assert user is not None
    return await user_principal(session, user, "dev")
