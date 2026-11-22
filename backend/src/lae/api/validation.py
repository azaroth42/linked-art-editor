"""Validation endpoints (Phase 1 Step 1.9; spec §5).

- ``POST /api/validate`` body ``{jsonld, record_uri?}``: validate any Linked Art record, for the
  CLI, imports and agents. Any authenticated caller. ``record_uri`` defaults to the root ``id``.
- ``POST /api/records/{id}/draft/validate``: validate the live draft of a record. Requires
  ``edit_draft`` (drafts are not public).

Both answer 200 with ``Diagnostics`` (``{items: [{code, path, severity, params, message_en}],
ok}``) whatever the findings: an invalid document is a result, not an error.

The draft comes from a ``DraftSnapshotSource``: the object on ``app.state.draft_snapshot_source``
that returns the projected JSON-LD of a record's draft, with its internal node IRIs. That is the
shape of ``lae.crdt.client.CrdtWsClient.snapshot`` (Step 1.8, the internal RPC to ``crdt-ws``,
decision A4); the application factory sets it once that client exists. Until then the endpoint
answers 503 ``LAE-VALID-001``, and the tests install a fake source.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from lae.api.errors import ApiError
from lae.auth.permissions import CurrentUser, current_user, require
from lae.db.session import get_session
from lae.ids import record_uri
from lae.logging import bind_log_context
from lae.settings import Settings
from lae.validation import Diagnostics, validate

DRAFTS_UNAVAILABLE: Final = "LAE-VALID-001"
"""503: no draft snapshot source is configured, or ``crdt-ws`` cannot be reached."""

DRAFT_NOT_FOUND: Final = "LAE-VALID-002"
"""404: the record does not exist or has no draft to validate; param ``id``."""

VALIDATION_API_CODES: Final = (DRAFTS_UNAVAILABLE, DRAFT_NOT_FOUND)
"""Every error code these endpoints emit themselves (diagnostics are ``lae.validation``'s)."""

SNAPSHOT_SOURCE_STATE: Final = "draft_snapshot_source"
"""Name of the ``app.state`` attribute holding the ``DraftSnapshotSource``."""

router = APIRouter(prefix="/api", tags=["validation"])


class DraftSnapshot(Protocol):
    """What validation needs from a draft snapshot (Step 1.8's ``Snapshot`` has more)."""

    @property
    def jsonld(self) -> dict[str, Any]:
        """The draft projected to JSON-LD, internal node IRIs kept."""
        ...


class DraftSnapshotSource(Protocol):
    """Where the live draft of a record is read from."""

    async def snapshot(self, record_id: UUID) -> DraftSnapshot | None:
        """Return the draft of ``record_id``; ``None`` (or ``LookupError``) if there is none."""
        ...


class ValidateRequest(BaseModel):
    """Body of ``POST /api/validate``."""

    jsonld: dict[str, Any]
    record_uri: str | None = None


def get_draft_snapshot_source(request: Request) -> DraftSnapshotSource:
    """FastAPI dependency: the configured ``DraftSnapshotSource``; 503 if there is none."""
    source: DraftSnapshotSource | None = getattr(request.app.state, SNAPSHOT_SOURCE_STATE, None)
    if source is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, DRAFTS_UNAVAILABLE)
    return source


@router.post("/validate")
async def validate_jsonld(
    body: ValidateRequest,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    request: Request,
) -> Diagnostics:
    """Validate the JSON-LD in the body and return its diagnostics."""
    settings: Settings = request.app.state.settings
    return await validate(
        body.jsonld, body.record_uri, session, context_version=settings.context_version
    )


@router.post("/records/{record_id}/draft/validate")
async def validate_draft(
    record_id: UUID,
    user: Annotated[CurrentUser, Depends(require("edit_draft"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    source: Annotated[DraftSnapshotSource, Depends(get_draft_snapshot_source)],
    request: Request,
) -> Diagnostics:
    """Validate the live draft of a record and return its diagnostics."""
    bind_log_context(record_id=record_id)
    settings: Settings = request.app.state.settings
    try:
        snapshot = await source.snapshot(record_id)
    except LookupError:
        snapshot = None
    if snapshot is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, DRAFT_NOT_FOUND, {"id": str(record_id)})
    uri = record_uri(record_id, base_uri=settings.base_uri)
    return await validate(snapshot.jsonld, uri, session, context_version=settings.context_version)
