"""Permissions, roles, and the ``current_user`` / ``require(*perms)`` dependencies.

Permission names are exactly those of spec §8.2 (conventions §2), listed in ``ALL_PERMISSIONS``.
A role is a named set of them, stored in ``roles.permissions``; a user's permissions are the union
over their ``user_roles``. ``ROLE_PERMISSIONS`` is the seed that migration ``0002_auth_seed``
writes (it hard-codes a copy; a test keeps the two equal) and the reference for tests. At request
time permissions are always read from the database, once per request: ``current_user`` stores the
resolved :class:`CurrentUser` on ``request.state``, and FastAPI's dependency cache shares it between
every ``require(...)`` of one request.

How a request is authenticated (``authenticate_request``):

1. ``Authorization: Bearer <token>``: a Personal Access Token (``lae.auth.tokens``). The user's
   permissions are intersected with the token's scopes. An invalid token is a 401 even when a
   session cookie or the dev user would otherwise apply: explicit credentials never fall back.
2. ``LAE_DEV_USER`` (development only, ``lae.auth.dev``): every other request runs as that user.
3. The signed session cookie set by the OIDC callback (``lae.auth.oidc``).

Anything else is anonymous: ``optional_user`` returns ``None``, ``current_user`` raises 401
``LAE-AUTH-002``, and ``require`` raises 403 ``LAE-AUTH-001`` when a permission is missing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lae.auth.errors import invalid_token, not_authenticated, permission_denied
from lae.db.models import Role, User, UserKind, UserRole
from lae.db.session import get_session
from lae.settings import Settings

ALL_PERMISSIONS: Final[tuple[str, ...]] = (
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
)
"""Every permission, in the order of conventions §2."""

ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    "Viewer": frozenset(),
    "Collaborator": frozenset({"edit_draft"}),
    "Editor": frozenset({"edit_draft", "publish", "delete", "merge", "unmerge", "protect"}),
    "Vocabulary Admin": frozenset({"manage_vocabularies"}),
    # Spec §8.2: administrative permissions are separate from editorial rights (migration 0003).
    "System Admin": frozenset(
        {
            "manage_vocabularies",
            "manage_roles",
            "manage_users",
            "manage_backups",
            "manage_system",
            "view_analytics",
        }
    ),
    "Tier 1 Agent": frozenset({"edit_draft", "propose_merge"}),
    "Tier 2 Agent": frozenset({"review_proposal", "approve_merge_scoped"}),
}
"""The roles of spec §10.4 with the permissions of spec §8.2 (seeded by ``0002``, System Admin
restricted to administrative permissions by ``0003``)."""

DEFAULT_ROLE: Final = "Viewer"
"""Role given to a user who signs in for the first time (and to any user left without a role)."""

AuthMethod = Literal["session", "token", "dev"]

_STATE_KEY: Final = "lae_current_user"
"""``request.state`` attribute caching the resolved user for the rest of the request."""


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated caller of one request, with the permissions in effect for it."""

    id: UUID
    display_name: str
    kind: UserKind
    roles: frozenset[str]
    permissions: frozenset[str]
    """Union over the roles; for a token, intersected with its scopes."""
    via: AuthMethod
    token_id: UUID | None = None
    """The ``api_tokens.id`` when ``via == "token"``."""

    def has(self, *permissions: str) -> bool:
        """Return whether every named permission is in effect."""
        return self.permissions.issuperset(permissions)


async def load_roles(session: AsyncSession, user_id: UUID) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(role names, union of their permissions)`` for a user, read from the database."""
    rows = await session.execute(
        select(Role.name, Role.permissions)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    roles: set[str] = set()
    permissions: set[str] = set()
    for name, role_permissions in rows:
        roles.add(name)
        permissions.update(role_permissions)
    return frozenset(roles), frozenset(permissions)


async def user_principal(
    session: AsyncSession,
    user: User,
    via: AuthMethod,
    *,
    scopes: frozenset[str] | None = None,
    token_id: UUID | None = None,
) -> CurrentUser:
    """Build the :class:`CurrentUser` of ``user``; a token's ``scopes`` narrow its permissions."""
    roles, permissions = await load_roles(session, user.id)
    if scopes is not None:
        permissions &= scopes
    return CurrentUser(
        id=user.id,
        display_name=user.display_name,
        kind=user.kind,
        roles=roles,
        permissions=permissions,
        via=via,
        token_id=token_id,
    )


async def authenticate_request(request: Request, session: AsyncSession) -> CurrentUser | None:
    """Resolve the caller (bearer token, else dev user, else session cookie); ``None`` if anonymous.

    Raises 401 ``LAE-AUTH-003`` for a bearer token that does not authenticate.
    """
    # Imported here: those modules' routers depend on this module's dependencies.
    from lae.auth import dev, oidc, tokens

    settings: Settings = request.app.state.settings
    raw_token = tokens.bearer_token(request)
    if raw_token is not None:
        user = await tokens.authenticate_token(session, raw_token)
        if user is None:
            raise invalid_token()
        return user
    if settings.dev_user:
        return await dev.dev_principal(request, session, settings.dev_user)
    return await oidc.session_principal(request, session)


async def optional_user(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> CurrentUser | None:
    """FastAPI dependency: the caller, or ``None`` when anonymous (cached for the request)."""
    if hasattr(request.state, _STATE_KEY):
        cached: CurrentUser | None = getattr(request.state, _STATE_KEY)
        return cached
    user = await authenticate_request(request, session)
    setattr(request.state, _STATE_KEY, user)
    return user


async def current_user(
    user: Annotated[CurrentUser | None, Depends(optional_user)],
) -> CurrentUser:
    """FastAPI dependency: the caller; 401 ``LAE-AUTH-002`` when anonymous."""
    if user is None:
        raise not_authenticated()
    return user


def require(*permissions: str) -> Callable[..., Awaitable[CurrentUser]]:
    """Return a dependency yielding the caller if they hold every permission, else 403.

    Unknown permission names raise ``ValueError`` when the route is defined, not per request.
    Anonymous callers get 401 ``LAE-AUTH-002``; callers lacking a permission get 403
    ``LAE-AUTH-001`` with ``params = {"required": [...], "missing": [...]}``.
    """
    unknown = sorted(set(permissions) - set(ALL_PERMISSIONS))
    if unknown:
        raise ValueError(f"unknown permission(s) {unknown}; see lae.auth.ALL_PERMISSIONS")
    required = frozenset(permissions)

    async def dependency(user: Annotated[CurrentUser, Depends(current_user)]) -> CurrentUser:
        missing = required - user.permissions
        if missing:
            raise permission_denied(required, missing)
        return user

    dependency.__name__ = "require_" + "_".join(sorted(required))
    dependency.__qualname__ = dependency.__name__
    return dependency
