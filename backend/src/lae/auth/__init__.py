"""Authentication and permissions (Phase 1, Step 1.7; spec §8, §10.4; decisions E1, E2).

Modules:

- ``permissions``: ``ALL_PERMISSIONS`` (spec §8.2), ``ROLE_PERMISSIONS`` (the seven roles of spec
  §10.4, seeded by migration ``0002_auth_seed``), ``CurrentUser``, and the FastAPI dependencies
  ``optional_user``, ``current_user`` and ``require(*perms)``. Role permissions are read from the
  database once per request.
- ``oidc``: ``GET /auth/login``, ``GET /auth/callback``, ``POST /auth/logout``, ``GET /auth/me``
  with Authlib, and the signed session cookie ``lae_session``.
- ``tokens``: Personal Access Tokens, ``POST``/``GET /api/tokens`` and ``DELETE
  /api/tokens/{id}``, and ``Authorization: Bearer`` authentication.
- ``dev``: the ``LAE_DEV_USER=<email>:<Role>`` development bypass (refused in production).
- ``setup``: ``install_auth`` (called by ``lae.app.create_app``) and ``AuthConfigError``.
- ``errors``: the ``LAE-AUTH-NNN`` codes; 401/403 bodies are ``{code, path, params}``
  (``lae.api.errors``), with English text in ``frontend/public/locales/en/errors.json``.

Protecting an endpoint (Step 1.6 and every later router)
--------------------------------------------------------

Every endpoint under ``/api/`` takes one of these dependencies; an endpoint without one is a bug
(Phase 2 guide). Use the caller the dependency returns (``user.id`` for attribution) rather than
resolving it again::

    from typing import Annotated
    from fastapi import Depends
    from lae.auth import CurrentUser, current_user, require

    @router.post("")  # POST /api/records: needs edit_draft
    async def create_record(
        body: RecordCreate, user: Annotated[CurrentUser, Depends(require("edit_draft"))]
    ) -> RecordCreated: ...

    @router.get("/{id}")  # GET /api/records/{id}: any signed-in user (401 if anonymous)
    async def get_record(
        id: UUID, user: Annotated[CurrentUser, Depends(current_user)]
    ) -> dict[str, Any]: ...

Step 1.6 wires: ``POST /api/records`` → ``require("edit_draft")``; ``GET /api/records/{id}``
and ``GET /api/records/{id}/revisions[/{n}]`` → ``current_user``; the public ``/id/{uuid}``
routes take **no** auth dependency (conventions §9). Steps 1.9–1.10 wire
``POST /api/records/{id}/draft/validate`` → ``require("edit_draft")`` and
``POST /api/records/{id}/publish`` and ``.../draft/discard`` → ``require("publish")``. Each
endpoint's tests include the happy path and one permission failure (conventions §4): at least
401 ``LAE-AUTH-002`` for an anonymous ``GET /api/records/{id}`` and 403 ``LAE-AUTH-001`` for a
``Viewer`` calling ``POST /api/records``. ``tests/auth/conftest.py`` has helpers that create a
user with given roles and a bearer token for it.

Other processes: ``crdt-ws`` (Step 1.8) authenticates a WebSocket with ``?token=`` through
``tokens.authenticate_token(session, token)`` and with the session cookie by adding the same
``SessionMiddleware`` (same ``LAE_SESSION_SECRET``) and calling ``oidc.session_principal``;
the MCP server (Phase 3) reuses ``authenticate_token``.
"""

from lae.auth.permissions import (
    ALL_PERMISSIONS,
    DEFAULT_ROLE,
    ROLE_PERMISSIONS,
    CurrentUser,
    current_user,
    optional_user,
    require,
)

__all__ = [
    "ALL_PERMISSIONS",
    "DEFAULT_ROLE",
    "ROLE_PERMISSIONS",
    "CurrentUser",
    "current_user",
    "optional_user",
    "require",
]
