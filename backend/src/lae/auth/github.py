"""GitHub login: an OAuth App, Authlib's OAuth 2.0 client, and the GitHub REST API (ADR-0026).

GitHub is OAuth 2.0, not OpenID Connect: there is no discovery document and no ID token. The
flow is ``/auth/login/github`` → ``https://github.com/login/oauth/authorize`` (``state``, PKCE
``S256``, scopes ``read:user user:email``, plus ``read:org`` when ``LAE_GITHUB_ALLOWED_ORGS`` is
set so that private memberships count) → ``/auth/callback/github``, which exchanges the code at
``https://github.com/login/oauth/access_token`` (``client_secret_post``) and identifies the user
with the access token:

- ``GET https://api.github.com/user``: ``users.subject = "github:<numeric id>"`` (never the
  login, which its owner can change and someone else can then take); ``display_name`` is
  ``name``, else ``login``.
- ``GET https://api.github.com/user/emails``: ``email`` is the primary address if GitHub has
  verified it, else ``NULL``. An unverified address is never stored.

The provider is enabled when both ``LAE_GITHUB_CLIENT_ID`` and ``LAE_GITHUB_CLIENT_SECRET`` are
set. ``LAE_GITHUB_ALLOWED_USERS`` (logins, compared case-insensitively) and
``LAE_GITHUB_ALLOWED_ORGS`` (organizations whose *active* members may sign in, checked with
``GET /user/memberships/orgs/{org}``) are checked at the callback, before the user is created;
an account on neither list gets 403 ``LAE-AUTH-012``. With neither list set any GitHub user may
sign in, as a ``Viewer`` like every new user. The access token is used only during the callback
and is not kept.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from typing import Any, Final
from urllib.parse import quote

import authlib.deprecate
import httpx
from fastapi import status

from lae.api.errors import ApiError
from lae.auth.errors import ACCOUNT_NOT_ALLOWED
from lae.auth.providers import (
    LoginIdentity,
    LoginProvider,
    idp_unavailable,
    login_failed,
    oauth_client_kwargs,
)
from lae.logging import get_logger
from lae.settings import Settings

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="The httpx module is deprecated",
        category=authlib.deprecate.AuthlibDeprecationWarning,
    )
    from authlib.integrations.starlette_client import OAuth, StarletteOAuth2App

GITHUB_PROVIDER_ID: Final = "github"
GITHUB_AUTHORIZE_URL: Final = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL: Final = "https://github.com/login/oauth/access_token"
GITHUB_API_URL: Final = "https://api.github.com/"
GITHUB_SCOPE: Final = "read:user user:email"
GITHUB_ORG_SCOPE: Final = "read:org"
"""Added to the scope when there is an organization allowlist (private memberships)."""

GITHUB_API_HEADERS: Final = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
SUBJECT_PREFIX: Final = "github:"
"""``users.subject`` of a GitHub user is this prefix and the numeric user id."""

INVALID_USER_ERROR: Final = "invalid_user"
"""``error`` of ``LAE-AUTH-007`` when ``/user`` has no numeric ``id`` or no ``login``."""

NOT_A_MEMBER_STATUSES: Final = frozenset({status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND})
"""``/user/memberships/orgs/{org}`` answers: no membership (or the org blocks this app)."""

logger = get_logger(__name__)


def github_enabled(settings: Settings) -> bool:
    """Whether both ``LAE_GITHUB_CLIENT_ID`` and ``LAE_GITHUB_CLIENT_SECRET`` are set."""
    secret = settings.github_client_secret
    return bool(settings.github_client_id) and bool(secret and secret.get_secret_value())


def github_scope(settings: Settings) -> str:
    """The scopes to request: ``read:user user:email`` (and ``read:org`` with an org allowlist)."""
    if settings.github_allowed_orgs:
        return f"{GITHUB_SCOPE} {GITHUB_ORG_SCOPE}"
    return GITHUB_SCOPE


def primary_verified_email(emails: object) -> str | None:
    """The primary address of a ``/user/emails`` answer if it is verified, else ``None``."""
    if not isinstance(emails, list):
        return None
    for entry in emails:
        if (
            isinstance(entry, dict)
            and entry.get("primary") is True
            and entry.get("verified") is True
            and isinstance(entry.get("email"), str)
            and entry["email"]
        ):
            return str(entry["email"])
    return None


class GitHubProvider(LoginProvider):
    """Sign in with a GitHub account, optionally restricted to listed users and organizations."""

    id = GITHUB_PROVIDER_ID
    display_name = "GitHub"

    def __init__(
        self,
        client: StarletteOAuth2App,
        allowed_users: Iterable[str] = (),
        allowed_orgs: Iterable[str] = (),
    ) -> None:
        super().__init__(client)
        self.allowed_users = frozenset(user.casefold() for user in allowed_users)
        self.allowed_orgs = tuple(allowed_orgs)

    async def identify(self, token: dict[str, Any]) -> LoginIdentity:
        """Read ``/user``, check the allowlists, and read the verified primary e-mail."""
        if not token.get("access_token"):
            raise login_failed(self.id, "invalid_token_response")
        user = await self._api_json(token, "user")
        user_id = user.get("id") if isinstance(user, dict) else None
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(user_id, int) or isinstance(user_id, bool) or not isinstance(login, str):
            raise login_failed(self.id, INVALID_USER_ERROR)
        if not login or not await self.is_allowed(token, login):
            logger.info("github_login_not_allowed", github_id=user_id, login=login)
            raise ApiError(status.HTTP_403_FORBIDDEN, ACCOUNT_NOT_ALLOWED, {"provider": self.id})
        name = user.get("name")
        display_name = name.strip() if isinstance(name, str) and name.strip() else login
        email = primary_verified_email(await self._api_json(token, "user/emails"))
        return LoginIdentity(
            subject=f"{SUBJECT_PREFIX}{user_id}", display_name=display_name, email=email
        )

    async def is_allowed(self, token: dict[str, Any], login: str) -> bool:
        """Whether ``login`` may sign in: no allowlist, a listed login, or an active member."""
        if not self.allowed_users and not self.allowed_orgs:
            return True
        if login.casefold() in self.allowed_users:
            return True
        for org in self.allowed_orgs:
            if await self.is_active_member(token, org):
                return True
        return False

    async def is_active_member(self, token: dict[str, Any], org: str) -> bool:
        """Whether the token's user is an active member of ``org`` (not just invited)."""
        response = await self._api(token, f"user/memberships/orgs/{quote(org, safe='')}")
        if response.status_code in NOT_A_MEMBER_STATUSES:
            return False
        if response.status_code != status.HTTP_200_OK:
            raise idp_unavailable(self.id, f"HTTP {response.status_code} from {response.url}")
        try:
            membership = response.json()
        except ValueError as exc:
            raise idp_unavailable(self.id, exc) from exc
        return isinstance(membership, dict) and membership.get("state") == "active"

    async def _api(self, token: dict[str, Any], path: str) -> httpx.Response:
        """``GET`` a REST API path with the user's access token; 502 if GitHub is unreachable."""
        try:
            response: httpx.Response = await self.client.get(
                path, token=token, headers=GITHUB_API_HEADERS
            )
        except httpx.HTTPError as exc:
            raise idp_unavailable(self.id, exc) from exc
        return response

    async def _api_json(self, token: dict[str, Any], path: str) -> Any:
        """The JSON body of a ``200`` answer to ``GET`` ``path``; 502 for anything else."""
        response = await self._api(token, path)
        if response.status_code != status.HTTP_200_OK:
            raise idp_unavailable(self.id, f"HTTP {response.status_code} from {response.url}")
        try:
            return response.json()
        except ValueError as exc:
            raise idp_unavailable(self.id, exc) from exc


def create_github_provider(
    settings: Settings, http_transport: httpx.AsyncBaseTransport | None = None
) -> GitHubProvider | None:
    """The GitHub provider, or ``None`` unless both its client id and secret are set."""
    if not github_enabled(settings):
        return None
    secret = settings.github_client_secret
    assert secret is not None
    oauth = OAuth()
    client: StarletteOAuth2App = oauth.register(
        GITHUB_PROVIDER_ID,
        client_id=settings.github_client_id,
        client_secret=secret.get_secret_value(),
        authorize_url=GITHUB_AUTHORIZE_URL,
        access_token_url=GITHUB_TOKEN_URL,
        api_base_url=GITHUB_API_URL,
        client_kwargs=oauth_client_kwargs(
            github_scope(settings),
            http_transport,
            token_endpoint_auth_method="client_secret_post",
        ),
    )
    return GitHubProvider(
        client,
        allowed_users=settings.github_allowed_users,
        allowed_orgs=settings.github_allowed_orgs,
    )
