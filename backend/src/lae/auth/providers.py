"""Sign-in providers: what ``lae.auth.oidc``'s login routes need from each one (ADR-0026).

A provider is one way a person signs in through an OAuth 2.0 authorization code flow run by
Authlib (``state`` always, PKCE ``S256`` where the provider supports it). Each provider has an
``id`` (the ``{provider}`` of ``/auth/login/{provider}`` and ``/auth/callback/{provider}``), a
``display_name`` for ``GET /auth/providers``, and an :meth:`LoginProvider.identify` that turns
the token response into a :class:`LoginIdentity`, the fields ``users`` keeps. The providers are:

- ``oidc`` (``lae.auth.oidc.OIDCProvider``): any OpenID Connect issuer; the identity comes from
  the validated ID token.
- ``github`` (``lae.auth.github.GitHubProvider``): GitHub's OAuth 2.0 (not OIDC); the identity
  comes from ``https://api.github.com/user`` and ``/user/emails``.

``lae.auth.setup.install_auth`` creates the enabled providers, in that order, and keeps them on
``app.state.login_providers``; the first is the default one that ``/auth/login`` and
``/auth/callback`` (the routes of Step 1.7) use. Errors are the ``LAE-AUTH-NNN`` responses of
``lae.auth.errors``: a refused or failed exchange is 400 ``LAE-AUTH-007``, an unreachable or
failing provider 502 ``LAE-AUTH-008``.
"""

from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Final

import authlib.deprecate
import httpx
from fastapi import Request, Response, status
from pydantic import BaseModel

from lae.api.errors import ApiError
from lae.auth.errors import IDP_UNAVAILABLE, LOGIN_FAILED, LOGIN_NOT_CONFIGURED, UNKNOWN_PROVIDER
from lae.logging import get_logger

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="The httpx module is deprecated",
        category=authlib.deprecate.AuthlibDeprecationWarning,
    )
    from authlib.integrations.starlette_client import OAuthError, StarletteOAuth2App

IDP_TIMEOUT_SECONDS: Final = 10.0
"""Timeout of each request to an identity provider."""

PROVIDER_CALLBACK_ROUTE: Final = "auth_provider_callback"
"""Route name of ``GET /auth/callback/{provider}``."""

logger = get_logger(__name__)


@dataclass(frozen=True)
class LoginIdentity:
    """The person a provider vouches for, as ``users`` stores them."""

    subject: str
    """``users.subject``: stable, and unique across providers."""
    display_name: str
    email: str | None


class ProviderInfo(BaseModel):
    """One entry of ``GET /auth/providers``."""

    id: str
    display_name: str


def oauth_client_kwargs(
    scope: str, http_transport: httpx.AsyncBaseTransport | None, **extra: Any
) -> dict[str, Any]:
    """Authlib ``client_kwargs``: scope, PKCE ``S256``, timeout, and the test transport."""
    kwargs: dict[str, Any] = {
        "scope": scope,
        "code_challenge_method": "S256",
        "timeout": IDP_TIMEOUT_SECONDS,
        **extra,
    }
    if http_transport is not None:
        kwargs["transport"] = http_transport
    return kwargs


def idp_unavailable(provider: str, cause: Exception | str) -> ApiError:
    """502 ``LAE-AUTH-008``, logged with the cause."""
    logger.warning("login_provider_unavailable", provider=provider, error=repr(cause))
    return ApiError(status.HTTP_502_BAD_GATEWAY, IDP_UNAVAILABLE)


def login_failed(provider: str, error: str) -> ApiError:
    """400 ``LAE-AUTH-007`` with an OAuth 2.0 error code."""
    logger.info("login_failed", provider=provider, error=error)
    return ApiError(status.HTTP_400_BAD_REQUEST, LOGIN_FAILED, {"error": error})


class LoginProvider(ABC):
    """One way to sign in: an Authlib OAuth 2.0 client plus how to identify the user."""

    id: str
    """Path segment of the login routes and id in ``GET /auth/providers``."""
    display_name: str
    """Name in ``GET /auth/providers`` (the SPA translates the ids it knows)."""

    def __init__(self, client: StarletteOAuth2App) -> None:
        self.client = client

    def info(self) -> ProviderInfo:
        """This provider's entry in ``GET /auth/providers``."""
        return ProviderInfo(id=self.id, display_name=self.display_name)

    def redirect_uri(self, request: Request) -> str:
        """Where the provider sends the browser back: ``/auth/callback/<id>``."""
        return str(request.url_for(PROVIDER_CALLBACK_ROUTE, provider=self.id))

    async def authorize_redirect(self, request: Request) -> Response:
        """Redirect to the provider's authorization endpoint (state and PKCE in the session)."""
        try:
            response: Response = await self.client.authorize_redirect(
                request, self.redirect_uri(request)
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise idp_unavailable(self.id, exc) from exc
        return response

    async def fetch_token(self, request: Request) -> dict[str, Any]:
        """Check ``state`` and exchange the callback's code (with the PKCE verifier)."""
        try:
            token: dict[str, Any] = await self.client.authorize_access_token(request)
        except OAuthError as exc:
            raise login_failed(self.id, exc.error or "oauth_error") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise idp_unavailable(self.id, exc) from exc
        return token

    async def authenticate(self, request: Request) -> LoginIdentity:
        """Finish the provider's side of the callback: who signed in (or an ``ApiError``)."""
        return await self.identify(await self.fetch_token(request))

    @abstractmethod
    async def identify(self, token: dict[str, Any]) -> LoginIdentity:
        """Turn the token response into the signed-in person, or raise an ``ApiError``."""


def login_providers(request: Request) -> dict[str, LoginProvider]:
    """The application's enabled providers by id, the default one first."""
    providers: dict[str, LoginProvider] = request.app.state.login_providers
    return providers


def get_provider(request: Request, provider_id: str) -> LoginProvider:
    """The enabled provider ``provider_id``; 404 ``LAE-AUTH-011`` if there is none."""
    provider = login_providers(request).get(provider_id)
    if provider is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, UNKNOWN_PROVIDER, {"provider": provider_id})
    return provider


def default_provider(request: Request) -> LoginProvider:
    """The first enabled provider; 503 ``LAE-AUTH-006`` when sign-in is not configured."""
    for provider in login_providers(request).values():
        return provider
    raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, LOGIN_NOT_CONFIGURED)
