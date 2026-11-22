"""HTTP API (FastAPI routers).

``routers()`` is the registry ``lae.app.create_app`` includes. A step that adds a router module
imports it inside ``routers()`` (lazily, so submodules may import from ``lae.api``) and appends
its ``router``.
"""

from __future__ import annotations

from fastapi import APIRouter


def routers() -> list[APIRouter]:
    """Return every HTTP API router, in registration order."""
    from lae.api import public, records, validation
    from lae.auth import oidc, tokens

    return [oidc.router, tokens.router, records.router, public.router, validation.router]
