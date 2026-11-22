"""Install authentication on an application: settings checks, session cookie, OIDC client.

``lae.app.create_app`` calls :func:`install_auth`. It raises :class:`AuthConfigError`, and so the
``api`` process refuses to start, when ``LAE_DEV_USER`` is set with ``LAE_ENV=production`` or is
malformed, when ``LAE_OIDC_ISSUER`` is set without ``LAE_OIDC_CLIENT_ID``, or when
``LAE_SESSION_SECRET`` is missing in ``staging`` or ``production``. In ``development`` and
``test`` a missing session secret is replaced by a random one per process (sessions then end
when the process restarts, and ``crdt-ws`` cannot read them; set the secret to share sessions).
"""

from __future__ import annotations

import secrets
from typing import Final

import httpx
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from lae.auth.dev import init_dev_state, parse_dev_user
from lae.auth.oidc import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS, create_oidc_client
from lae.logging import get_logger
from lae.settings import Settings

SECRET_REQUIRED_ENVS: Final = frozenset({"staging", "production"})
"""Environments in which ``LAE_SESSION_SECRET`` must be set."""

logger = get_logger(__name__)


class AuthConfigError(RuntimeError):
    """The authentication settings are unsafe or inconsistent; the process must not start."""


def check_auth_settings(settings: Settings) -> None:
    """Raise :class:`AuthConfigError` if the settings must not be served."""
    if settings.dev_user:
        if settings.env == "production":
            raise AuthConfigError(
                "LAE_DEV_USER is set while LAE_ENV=production "
                "(the development login bypass is never allowed in production)"
            )
        try:
            parse_dev_user(settings.dev_user)
        except ValueError as exc:
            raise AuthConfigError(str(exc)) from exc
    if settings.oidc_issuer and not settings.oidc_client_id:
        raise AuthConfigError("LAE_OIDC_ISSUER is set but LAE_OIDC_CLIENT_ID is not")
    if settings.session_secret is None and settings.env in SECRET_REQUIRED_ENVS:
        raise AuthConfigError(f"LAE_SESSION_SECRET is required when LAE_ENV={settings.env}")


def session_secret(settings: Settings) -> str:
    """The key that signs session cookies: ``LAE_SESSION_SECRET``, else a random per-process key."""
    if settings.session_secret is not None:
        return settings.session_secret.get_secret_value()
    logger.warning("session_secret_unset", detail="random per-process key; sessions end on restart")
    return secrets.token_urlsafe(32)


def install_auth(
    app: FastAPI, settings: Settings, http_transport: httpx.AsyncBaseTransport | None = None
) -> None:
    """Check the settings, add the session middleware, and create the OIDC client."""
    check_auth_settings(settings)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret(settings),
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=settings.env != "development",
    )
    app.state.oidc_client = create_oidc_client(settings, http_transport)
    init_dev_state(app.state)
    if settings.dev_user:
        logger.warning("dev_user_active", dev_user=settings.dev_user)
