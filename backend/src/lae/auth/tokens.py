"""Personal Access Tokens (PATs): ``/api/tokens`` and bearer authentication.

A token is ``lae_pat_`` followed by 32 random bytes (URL-safe base64). The plaintext is returned
once, by ``POST /api/tokens``; ``api_tokens.token_hash`` stores its SHA-256 hex digest, and
authentication looks the digest up and compares it in constant time. A token authenticates as
its user while the user is active and the token is neither expired nor deleted; the permissions
in effect are the token's ``scopes`` intersected with the user's current role permissions, so a
token never outlives a role the user has lost. ``last_used_at`` is written at most once per
``LAST_USED_RESOLUTION``.

Tokens are created from a signed-in session or the dev user, never with another token (403
``LAE-AUTH-005``), so a leaked token cannot mint longer-lived ones. Scopes must be permission names
(``lae.auth.ALL_PERMISSIONS``, else 422) that the caller holds (else 403 ``LAE-AUTH-004``).
Service-account tokens for agents (Phase 3, ``lae agents create``) are rows of the same table.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import AwareDatetime, BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lae.api.errors import ApiError
from lae.auth.errors import SCOPES_NOT_HELD, TOKEN_NEEDS_SESSION, TOKEN_NOT_FOUND
from lae.auth.permissions import ALL_PERMISSIONS, CurrentUser, current_user, user_principal
from lae.db.models import ApiToken, User
from lae.db.session import get_session
from lae.logging import get_logger

TOKEN_PREFIX: Final = "lae_pat_"
"""Prefix of every token, so secret scanners and people can recognise one."""

TOKEN_BYTES: Final = 32
"""Random bytes in a token."""

LAST_USED_RESOLUTION: Final = timedelta(minutes=1)
"""``last_used_at`` is updated only when it is older than this (or unset)."""

logger = get_logger(__name__)

router = APIRouter(prefix="/api/tokens", tags=["auth"])


def generate_token() -> str:
    """Return a new random plaintext token."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest stored in ``api_tokens.token_hash``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(request: Request) -> str | None:
    """Return the credentials of an ``Authorization: Bearer`` header, else ``None``."""
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credentials.strip()


async def _record_use(session: AsyncSession, token: ApiToken, now: datetime) -> None:
    """Set ``last_used_at`` to now unless it was set less than a minute ago; commit."""
    if token.last_used_at is not None and now - token.last_used_at < LAST_USED_RESOLUTION:
        return
    await session.execute(
        update(ApiToken)
        .where(
            ApiToken.id == token.id,
            or_(
                ApiToken.last_used_at.is_(None),
                ApiToken.last_used_at < func.now() - LAST_USED_RESOLUTION,
            ),
        )
        .values(last_used_at=func.now())
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def authenticate_token(session: AsyncSession, token: str) -> CurrentUser | None:
    """Return the caller a plaintext token authenticates as, or ``None`` if it does not.

    ``None`` covers unknown, deleted, and expired tokens and deactivated users alike.
    """
    if not token:
        return None
    digest = hash_token(token)
    row = (
        await session.execute(
            select(ApiToken, User)
            .join(User, User.id == ApiToken.user_id)
            .where(ApiToken.token_hash == digest)
        )
    ).one_or_none()
    if row is None:
        return None
    api_token, user = row
    if not hmac.compare_digest(api_token.token_hash, digest):
        return None
    now = datetime.now(UTC)
    if api_token.expires_at is not None and api_token.expires_at <= now:
        return None
    if not user.is_active:
        return None
    await _record_use(session, api_token, now)
    return await user_principal(
        session, user, "token", scopes=frozenset(api_token.scopes), token_id=api_token.id
    )


class TokenCreate(BaseModel):
    """Body of ``POST /api/tokens``."""

    name: str = Field(min_length=1, max_length=200)
    scopes: list[str]
    """Permission names; may be empty (the token then only authenticates)."""
    expires_at: AwareDatetime | None = None
    """Expiry with a time zone; ``None`` means the token does not expire."""

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, scopes: list[str]) -> list[str]:
        unknown = sorted(set(scopes) - set(ALL_PERMISSIONS))
        if unknown:
            raise ValueError(f"unknown permission(s): {', '.join(unknown)}")
        return sorted(set(scopes))

    @field_validator("expires_at")
    @classmethod
    def _in_the_future(cls, expires_at: datetime | None) -> datetime | None:
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return expires_at


class TokenInfo(BaseModel):
    """A token as listed by ``GET /api/tokens`` (never the plaintext or the hash)."""

    id: UUID
    name: str
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, token: ApiToken) -> TokenInfo:
        """Describe an ``api_tokens`` row."""
        return cls(
            id=token.id,
            name=token.name,
            scopes=list(token.scopes),
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
            created_at=token.created_at,
        )


class TokenCreated(TokenInfo):
    """Response of ``POST /api/tokens``: the only time the plaintext ``token`` is shown."""

    token: str


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_token(
    body: TokenCreate,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenCreated:
    """Create a token for the caller and return its plaintext once."""
    if user.via == "token":
        raise ApiError(status.HTTP_403_FORBIDDEN, TOKEN_NEEDS_SESSION)
    not_held = sorted(set(body.scopes) - user.permissions)
    if not_held:
        raise ApiError(status.HTTP_403_FORBIDDEN, SCOPES_NOT_HELD, {"scopes": not_held})
    plaintext = generate_token()
    token = ApiToken(
        user_id=user.id,
        name=body.name,
        token_hash=hash_token(plaintext),
        scopes=body.scopes,
        expires_at=body.expires_at,
    )
    session.add(token)
    await session.commit()
    await session.refresh(token)
    logger.info("token_created", user_id=str(user.id), token_id=str(token.id), scopes=body.scopes)
    return TokenCreated(**TokenInfo.of(token).model_dump(), token=plaintext)


@router.get("")
async def list_tokens(
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[TokenInfo]:
    """List the caller's tokens, oldest first."""
    tokens = await session.scalars(
        select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at)
    )
    return [TokenInfo.of(token) for token in tokens]


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_token(
    token_id: UUID,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Revoke one of the caller's tokens; 404 ``LAE-AUTH-010`` if it is not theirs."""
    deleted = await session.scalar(
        delete(ApiToken)
        .where(ApiToken.id == token_id, ApiToken.user_id == user.id)
        .returning(ApiToken.id)
    )
    if deleted is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, TOKEN_NOT_FOUND, {"id": str(token_id)})
    await session.commit()
    logger.info("token_deleted", user_id=str(user.id), token_id=str(token_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
