"""HTTP API (FastAPI routers).

``routers()`` is the registry ``lae.app.create_app`` includes. A step that adds a router module
imports it inside ``routers()`` (lazily, so submodules may import from ``lae.api``) and appends
its ``router``.
"""

from __future__ import annotations

from fastapi import APIRouter


def routers() -> list[APIRouter]:
    """Return every HTTP API router, in registration order."""
    return []
