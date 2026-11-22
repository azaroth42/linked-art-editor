"""Diagnostic codes of ``lae.auth`` and the :class:`~lae.api.errors.ApiError` for each.

Every code has an English message under the same key in
``frontend/public/locales/en/errors.json`` (a test checks that ``AUTH_CODES`` and that file agree).
401 responses carry ``WWW-Authenticate: Bearer`` (RFC 6750 §3).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from fastapi import status

from lae.api.errors import ApiError

PERMISSION_DENIED: Final = "LAE-AUTH-001"
"""403: the caller lacks a permission; params ``required`` and ``missing`` (sorted names)."""

NOT_AUTHENTICATED: Final = "LAE-AUTH-002"
"""401: the endpoint needs a signed-in user or a bearer token, and the request has neither."""

INVALID_TOKEN: Final = "LAE-AUTH-003"
"""401: the bearer token is unknown, revoked, expired, or belongs to a deactivated user."""

SCOPES_NOT_HELD: Final = "LAE-AUTH-004"
"""403: a new token asks for scopes the caller does not hold; param ``scopes``."""

TOKEN_NEEDS_SESSION: Final = "LAE-AUTH-005"
"""403: tokens are created from a signed-in session (or the dev user), not with another token."""

LOGIN_NOT_CONFIGURED: Final = "LAE-AUTH-006"
"""503: ``LAE_OIDC_ISSUER`` is not set, so there is no login."""

LOGIN_FAILED: Final = "LAE-AUTH-007"
"""400: the login callback was rejected; param ``error`` (an OAuth 2.0 error code)."""

IDP_UNAVAILABLE: Final = "LAE-AUTH-008"
"""502: the identity provider could not be reached or answered with an HTTP error."""

ACCOUNT_INACTIVE: Final = "LAE-AUTH-009"
"""403: the account exists but has been deactivated."""

TOKEN_NOT_FOUND: Final = "LAE-AUTH-010"
"""404: no token with this id belongs to the caller."""

AUTH_CODES: Final = (
    PERMISSION_DENIED,
    NOT_AUTHENTICATED,
    INVALID_TOKEN,
    SCOPES_NOT_HELD,
    TOKEN_NEEDS_SESSION,
    LOGIN_NOT_CONFIGURED,
    LOGIN_FAILED,
    IDP_UNAVAILABLE,
    ACCOUNT_INACTIVE,
    TOKEN_NOT_FOUND,
)
"""Every code this package emits."""

BEARER_CHALLENGE: Final = 'Bearer realm="lae"'
"""``WWW-Authenticate`` value of a 401 without a token."""

INVALID_TOKEN_CHALLENGE: Final = 'Bearer realm="lae", error="invalid_token"'
"""``WWW-Authenticate`` value of a 401 for a rejected token (RFC 6750 §3.1)."""


def not_authenticated() -> ApiError:
    """401 ``LAE-AUTH-002``."""
    return ApiError(
        status.HTTP_401_UNAUTHORIZED,
        NOT_AUTHENTICATED,
        headers={"WWW-Authenticate": BEARER_CHALLENGE},
    )


def invalid_token() -> ApiError:
    """401 ``LAE-AUTH-003``."""
    return ApiError(
        status.HTTP_401_UNAUTHORIZED,
        INVALID_TOKEN,
        headers={"WWW-Authenticate": INVALID_TOKEN_CHALLENGE},
    )


def permission_denied(required: Iterable[str], missing: Iterable[str]) -> ApiError:
    """403 ``LAE-AUTH-001`` naming the required and the missing permissions."""
    return ApiError(
        status.HTTP_403_FORBIDDEN,
        PERMISSION_DENIED,
        {"required": sorted(required), "missing": sorted(missing)},
    )
