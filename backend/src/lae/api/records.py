"""Records API: create a record, read it for the editor, list and read its revisions.

Phase 1, Step 1.6 (spec §3.1, §3.4, §4.1). Routes, all under ``/api/records``:

- ``POST /api/records`` (``edit_draft``): create a never-published record (``version = 0``) and
  return ``{id, uri, draft_epoch}``.
- ``GET /api/records/{id}`` (signed in): the published JSON-LD **with** internal node IRIs (the
  editor needs them), ``_label`` transcluded for the caller's locale chain.
- ``GET /api/records/{id}/revisions`` and ``GET /api/records/{id}/revisions/{n}`` (signed in):
  the revision list and one revision's stored JSON-LD, in the shapes Step 1.14 specifies; Step
  1.14 adds the semantic diff and revert beside them.

This module also holds what ``lae.api.public`` (``/id/{uuid}``) shares with it: the
``LAE-REC-NNN`` codes (English text in ``frontend/public/locales/en/errors.json``, checked by a
test against ``RECORD_CODES``), ``record_etag``, and ``load_version``, which resolves "version N
of a record" to a stored JSON-LD snapshot:

- a ``record_revisions`` row for N: its zstd JSON-LD snapshot;
- otherwise, if N is the record's current version: ``records.data`` (a bulk-loaded record stays
  at version 1 with no revision row until its first edit, decision A9);
- otherwise there is no such version.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final
from uuid import UUID

import orjson
from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from lae.api.deps import locale_chain
from lae.api.errors import ApiError
from lae.app import get_app_settings
from lae.auth import CurrentUser, current_user, require
from lae.db.models import CrdtDraft, Record, RecordRevision, RecordStatus
from lae.db.session import get_session
from lae.ids import new_uuid7, record_uri
from lae.labels.transclude import hydrate_labels
from lae.logging import bind_log_context, get_logger
from lae.projection import (
    ProjectionError,
    assign_node_iris,
    canonical_json,
    hydrate,
    strip_internal_iris,
)
from lae.rdf.canonical import decompress
from lae.rdf.context import CONTEXT_URLS
from lae.settings import Settings

RECORD_NOT_FOUND: Final = "LAE-REC-001"
"""404: no published record with this id (never-published drafts are not found publicly)."""

RECORD_DELETED: Final = "LAE-REC-002"
"""410: the record was deleted; its URI stays a tombstone (spec §3.4)."""

RECORD_MERGED: Final = "LAE-REC-003"
"""301: the record was merged into another; param ``merged_into`` (the target's URI)."""

VERSION_NOT_FOUND: Final = "LAE-REC-004"
"""404: the record has no stored version ``version`` (param)."""

UNKNOWN_RECORD_TYPE: Final = "LAE-REC-005"
"""422: ``POST /api/records`` with a ``type`` that is not a record class; params ``type``,
``allowed``."""

INVALID_TEMPLATE: Final = "LAE-REC-006"
"""422: the ``template`` cannot be hydrated into a draft; param ``reason`` (a ``LAE-PROJ`` code)."""

RECORD_CODES: Final = (
    RECORD_NOT_FOUND,
    RECORD_DELETED,
    RECORD_MERGED,
    VERSION_NOT_FOUND,
    UNKNOWN_RECORD_TYPE,
    INVALID_TEMPLATE,
)
"""Every code the records and public routes emit."""

RECORD_TYPES: Final = frozenset(
    {
        "HumanMadeObject",
        "Person",
        "Group",
        "Place",
        "Type",
        "Language",
        "Material",
        "Currency",
        "MeasurementUnit",
        "VisualItem",
        "LinguisticObject",
        "DigitalObject",
        "Set",
        "Activity",
        "Event",
        "Period",
    }
)
"""Classes a record may have: the top-level classes of the Linked Art model (glossary "Record").
Embedded classes (``Name``, ``TimeSpan``, ...) never have a URI of their own."""

JSONLD_MEDIA_TYPE: Final = "application/ld+json"

RECORD_KEY_ORDER: Final = ("@context", "id", "type", "_label")
"""Keys put first in served JSON-LD (``jsonb`` does not keep key order)."""

logger = get_logger(__name__)

router = APIRouter(prefix="/api/records", tags=["records"])


class RecordCreate(BaseModel):
    """Body of ``POST /api/records``."""

    type: str
    """The record class, one of ``RECORD_TYPES``."""

    template: dict[str, Any] | None = None
    """Initial content of the draft; its ``@context``, ``id`` and ``type`` are replaced."""


class RecordCreated(BaseModel):
    """Answer of ``POST /api/records``."""

    id: UUID
    uri: str
    draft_epoch: int


class RevisionSummary(BaseModel):
    """One entry of ``GET /api/records/{id}/revisions`` (Step 1.14's shape)."""

    version: int
    created_at: datetime
    authors: list[UUID]
    published_by: UUID | None
    """``None`` only for a bulk-loaded version, which no one published through the editor."""
    summary: str


@dataclass(frozen=True, slots=True)
class RecordVersion:
    """One stored version of a record, as ``load_version`` finds it."""

    version: int
    data: dict[str, Any]
    context_version: int
    modified: datetime
    """When the version was published (``record_revisions.created_at``, else ``updated_at``)."""
    etag: str
    """Entity tag of the stored JSON-LD (``records.etag`` or a hash of the snapshot)."""


def record_etag(data: dict[str, Any]) -> str:
    """Return the ``records.etag`` of ``data``: BLAKE2b-128 hex of its canonical JSON.

    Spike S6 computes the bulk-load etag the same way; publish (Step 1.10) uses this function.
    """
    return hashlib.blake2b(canonical_json(data), digest_size=16).hexdigest()


def context_url(context_version: int) -> str:
    """Return the public ``@context`` URL of a pinned context version."""
    for url, version in CONTEXT_URLS.items():
        if version == context_version:
            return url
    raise LookupError(f"no pinned context URL for context version {context_version}")


def new_record_data(
    record_type: str, uri: str, context_version: int, template: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the JSON-LD of a new record: the skeleton plus the template's other keys.

    The skeleton is ``{"@context", "id", "type"}``. Internal node IRIs of the record the template
    was copied from (if it has an ``id``) are dropped, then every embedded node gets one of the
    new record, so no node of another record's namespace is carried over.
    """
    content: dict[str, Any] = {}
    if template:
        source = template.get("id")
        if isinstance(source, str) and source:
            template = strip_internal_iris(template, source)
        content = {k: v for k, v in template.items() if k not in ("@context", "id", "type")}
    data = {"@context": context_url(context_version), "id": uri, "type": record_type, **content}
    return assign_node_iris(data, uri)


def ordered_record(data: dict[str, Any]) -> dict[str, Any]:
    """Return ``data`` with ``@context``, ``id``, ``type``, ``_label`` first, the rest after."""
    first: dict[str, Any] = {key: data[key] for key in RECORD_KEY_ORDER if key in data}
    return {**first, **{k: v for k, v in data.items() if k not in first}}


def jsonld_response(
    data: dict[str, Any],
    *,
    media_type: str = JSONLD_MEDIA_TYPE,
    headers: dict[str, str] | None = None,
) -> Response:
    """Serialize a JSON-LD document (key order as ``ordered_record``) into a response."""
    return Response(orjson.dumps(ordered_record(data)), media_type=media_type, headers=headers)


def not_found() -> ApiError:
    """404 ``LAE-REC-001``."""
    return ApiError(status.HTTP_404_NOT_FOUND, RECORD_NOT_FOUND)


def check_status(record: Record, merged_location: str) -> None:
    """Raise 410 for a deleted record and 301 to ``merged_location`` for a merged one.

    A merged record without a ``merged_into`` is answered like a deleted one.
    """
    if record.status == RecordStatus.DELETED or (
        record.status == RecordStatus.MERGED and record.merged_into is None
    ):
        raise ApiError(status.HTTP_410_GONE, RECORD_DELETED)
    if record.status == RecordStatus.MERGED:
        raise ApiError(
            status.HTTP_301_MOVED_PERMANENTLY,
            RECORD_MERGED,
            {"merged_into": merged_location},
            headers={"Location": merged_location},
        )


def _read_snapshot(blob: bytes) -> tuple[dict[str, Any], str]:
    """Decompress a revision's zstd JSON-LD; return the document and a hash of its bytes."""
    raw = decompress(blob)
    data: dict[str, Any] = orjson.loads(raw)
    return data, hashlib.blake2b(raw, digest_size=16).hexdigest()


async def load_version(session: AsyncSession, record: Record, version: int) -> RecordVersion | None:
    """Return version ``version`` of ``record`` (see the module docstring), else ``None``."""
    if version < 1 or version > record.version:
        return None
    row = (
        await session.execute(
            select(
                RecordRevision.jsonld_zstd,
                RecordRevision.context_version,
                RecordRevision.created_at,
            ).where(RecordRevision.record_id == record.id, RecordRevision.version == version)
        )
    ).first()
    if row is not None:
        blob, revision_context, created_at = row
        data, digest = await asyncio.to_thread(_read_snapshot, blob)
        return RecordVersion(version, data, revision_context, created_at, digest)
    if version == record.version:
        return current_version(record)
    return None


def current_version(record: Record) -> RecordVersion:
    """Return the version in ``records.data``."""
    return RecordVersion(
        record.version, record.data, record.context_version, record.updated_at, record.etag
    )


async def has_draft(session: AsyncSession, record_id: UUID) -> bool:
    """Return whether the record has a ``crdt_drafts`` row (an open or persisted draft)."""
    found = await session.scalar(select(exists().where(CrdtDraft.record_id == record_id)))
    return bool(found)


async def get_record_or_404(session: AsyncSession, record_id: UUID) -> Record:
    """Return the ``records`` row, or raise 404 ``LAE-REC-001``."""
    record = await session.get(Record, record_id)
    if record is None:
        raise not_found()
    return record


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "LAE-REC-005, LAE-REC-006"}},
)
async def create_record(
    body: RecordCreate,
    response: Response,
    user: Annotated[CurrentUser, Depends(require("edit_draft"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> RecordCreated:
    """Create a never-published record (``version = 0``) and return ``{id, uri, draft_epoch}``.

    The row holds the skeleton ``{"@context", "id", "type"}``, plus the ``template``'s content
    when one is given, with internal node IRIs assigned.

    **Draft room (Step 1.8).** This endpoint does not open the draft yet and writes no
    ``crdt_drafts`` row: Step 1.8 (``crdt-ws``, ``RoomManager.open``) wires room opening in here
    and returns the room's ``draft_epoch``. Until then ``draft_epoch`` is that of a new record,
    ``0`` (``records.draft_epoch``, migration ``0004``, defaults to 0). ``RoomManager.open``
    hydrates the room from ``records.data``, so the template becomes the draft's content.
    """
    if body.type not in RECORD_TYPES:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            UNKNOWN_RECORD_TYPE,
            {"type": body.type, "allowed": sorted(RECORD_TYPES)},
        )
    record_id = new_uuid7()
    bind_log_context(user_id=user.id, record_id=record_id)
    uri = record_uri(record_id, base_uri=settings.base_uri)
    data = new_record_data(body.type, uri, settings.context_version, body.template)
    if body.template:
        try:
            # The room must be able to hydrate it (Step 1.8); refuse it now rather than then.
            await asyncio.to_thread(hydrate, data, uri)
        except ProjectionError as error:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_TEMPLATE, {"reason": error.code}
            ) from error
    session.add(
        Record(
            id=record_id,
            uri=uri,
            type=body.type,
            version=0,
            status=RecordStatus.ACTIVE,
            etag=record_etag(data),
            data=data,
            context_version=settings.context_version,
        )
    )
    await session.commit()
    logger.info("record_created", type=body.type)
    response.headers["Location"] = f"{router.prefix}/{record_id}"
    return RecordCreated(id=record_id, uri=uri, draft_epoch=0)


@router.get(
    "/{record_id}",
    response_class=JSONResponse,
    responses={
        status.HTTP_200_OK: {"content": {JSONLD_MEDIA_TYPE: {}}},
        status.HTTP_301_MOVED_PERMANENTLY: {"description": "LAE-REC-003"},
        status.HTTP_404_NOT_FOUND: {"description": "LAE-REC-001"},
        status.HTTP_410_GONE: {"description": "LAE-REC-002"},
    },
)
async def get_record(
    record_id: UUID,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    chain: Annotated[list[str], Depends(locale_chain)],
) -> Response:
    """Return the published JSON-LD with internal node IRIs and transcluded labels.

    A never-published record (``version = 0``) is found only while it has a draft. Deleted →
    410; merged → 301 to ``/api/records/{merged_into}``.
    """
    bind_log_context(user_id=user.id, record_id=record_id)
    record = await get_record_or_404(session, record_id)
    check_status(record, f"{router.prefix}/{record.merged_into}")
    if record.version == 0 and not await has_draft(session, record_id):
        raise not_found()
    return jsonld_response(await hydrate_labels(record.data, chain, session))


@router.get("/{record_id}/revisions", responses={status.HTTP_404_NOT_FOUND: {}})
async def list_revisions(
    record_id: UUID,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[RevisionSummary]:
    """List the record's versions, oldest first, whatever its status.

    A current version without a revision row (bulk-loaded, decision A9) is listed with the
    record's ``updated_at``, no authors, and no publisher.
    """
    bind_log_context(user_id=user.id, record_id=record_id)
    record = await get_record_or_404(session, record_id)
    rows = await session.execute(
        select(
            RecordRevision.version,
            RecordRevision.created_at,
            RecordRevision.authors,
            RecordRevision.published_by,
            RecordRevision.summary,
        )
        .where(RecordRevision.record_id == record_id)
        .order_by(RecordRevision.version)
    )
    revisions = [
        RevisionSummary(
            version=version,
            created_at=created_at,
            authors=authors,
            published_by=published_by,
            summary=summary,
        )
        for version, created_at, authors, published_by, summary in rows
    ]
    if record.version >= 1 and all(r.version != record.version for r in revisions):
        revisions.append(
            RevisionSummary(
                version=record.version,
                created_at=record.updated_at,
                authors=[],
                published_by=None,
                summary="",
            )
        )
    return revisions


@router.get(
    "/{record_id}/revisions/{version}",
    response_class=JSONResponse,
    responses={
        status.HTTP_200_OK: {"content": {JSONLD_MEDIA_TYPE: {}}},
        status.HTTP_404_NOT_FOUND: {"description": "LAE-REC-001, LAE-REC-004"},
    },
)
async def get_revision(
    record_id: UUID,
    version: int,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    chain: Annotated[list[str], Depends(locale_chain)],
) -> Response:
    """Return the stored JSON-LD of one version (internal node IRIs kept, labels transcluded)."""
    bind_log_context(user_id=user.id, record_id=record_id)
    record = await get_record_or_404(session, record_id)
    found = await load_version(session, record, version)
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, VERSION_NOT_FOUND, {"version": version})
    return jsonld_response(await hydrate_labels(found.data, chain, session))
