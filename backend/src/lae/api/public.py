"""Public dereference of record URIs: ``/id/{uuid}`` and ``/id/{uuid}/node/{nid}``.

Phase 1, Step 1.6 (spec §3.3, §3.4, §6.6, §12; decisions A6, A7, D8). These routes never require
authentication and never expose draft data (conventions §9): a record that was never published
(``version = 0``) is 404.

``GET /id/{uuid}`` negotiates the representation on ``Accept``:

- ``application/ld+json`` (with or without the Linked Art ``profile`` parameter; also
  ``application/json``): JSON-LD served as ``application/ld+json;profile="<context URL>"``;
- ``text/turtle``: Turtle (``lae.rdf.to_turtle``);
- ``text/html``, ``*/*``, no ``Accept``, or nothing acceptable: an HTML page. Until the Public
  Reader (Step 1.21) replaces it, a minimal page with the record's label (in the caller's locale)
  and links to the two serializations.

Browsers cannot set ``Accept`` on a link, so ``?format=jsonld|ttl|html`` overrides it; the HTML
page links to ``?format=jsonld`` and ``?format=ttl``. ``?version=N`` serves the stored JSON-LD of
version N (``lae.api.records.load_version``) in any of the three representations.

Every representation is made from one public document: the stored JSON-LD after
``strip_internal_iris`` (applied here and nowhere else on this path, conventions §4), with
``_label`` transcluded for the caller's ``Accept-Language`` (``hydrate_labels``). Embedded nodes
are therefore anonymous (blank nodes in Turtle). Derived node IRIs (ADR-0021) exist only in the
RDF sent to QLever, never in JSON-LD, so they cannot appear either; an embedded node that came
with an ``id`` of its own keeps that original ``id``, as stored.

Status: ``deleted`` → 410; ``merged`` → 301 to the URI of ``merged_into`` (keeping ``?format``,
dropping ``?version``). The status is checked before the version.

Caching: ``ETag`` is weak, ``W/"<entity tag of the stored version>-<representation>"``; the
record's ``etag`` for the current version, a hash of the snapshot for ``?version=N``. It is weak
because transcluded labels may change without the record changing. ``If-None-Match`` (weak
comparison, or ``*``) is answered with 304. ``Last-Modified`` is when the version was published.
``Vary: Accept, Accept-Language``.

``GET /id/{uuid}/node/{nid}`` answers 303 with the record URI for any node id that
``lae.ids.record_id_from_uri`` accepts: a UUIDv7 internal node id or a UUIDv5 derived one
(ADR-0021). Internal node IRIs appear in SPARQL results, so they must dereference (ADR-0003).
Anything else is 404. The record itself is not looked up; its URI answers for its status.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from datetime import UTC
from email.utils import format_datetime
from enum import StrEnum
from typing import Annotated, Any, Final
from uuid import UUID

import orjson
from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from lae.api.deps import locale_chain
from lae.api.errors import ApiError
from lae.api.records import (
    JSONLD_MEDIA_TYPE,
    VERSION_NOT_FOUND,
    RecordVersion,
    check_status,
    context_url,
    current_version,
    load_version,
    not_found,
    ordered_record,
)
from lae.app import get_app_settings
from lae.db.models import Record
from lae.db.session import get_session
from lae.ids import record_id_from_uri, record_uri
from lae.labels.extract import labels_from_record
from lae.labels.langtags import UNDETERMINED
from lae.labels.transclude import hydrate_labels, resolve_label
from lae.logging import bind_log_context
from lae.projection import strip_internal_iris
from lae.rdf import to_turtle
from lae.settings import Settings

TURTLE_MEDIA_TYPE: Final = "text/turtle"
HTML_MEDIA_TYPE: Final = "text/html"

VARY: Final = "Accept, Accept-Language"
"""Request headers every representation depends on."""


class Representation(StrEnum):
    """A representation of a record; the value is what ``?format=`` takes."""

    HTML = "html"
    JSONLD = "jsonld"
    TURTLE = "ttl"


NEGOTIATED: Final[tuple[tuple[Representation, tuple[str, ...]], ...]] = (
    (Representation.HTML, (HTML_MEDIA_TYPE,)),
    (Representation.JSONLD, (JSONLD_MEDIA_TYPE, "application/json")),
    (Representation.TURTLE, (TURTLE_MEDIA_TYPE,)),
)
"""Media types answered by each representation, in order of preference for ties."""

_ETAGC: Final = re.compile(r"[\x21\x23-\x7e]+")
"""RFC 9110 ``etagc``: what may stand between the quotes of an entity tag."""

router = APIRouter(prefix="/id", tags=["public"])


def jsonld_content_type(context_version: int) -> str:
    """``application/ld+json;profile="<pinned context URL>"`` (the Linked Art profile)."""
    return f'{JSONLD_MEDIA_TYPE};profile="{context_url(context_version)}"'


def _accept_entries(accept: str) -> list[tuple[str, float]]:
    """Return the ``(media range, q)`` pairs of an ``Accept`` value; malformed ones are skipped."""
    entries: list[tuple[str, float]] = []
    for entry in accept.split(","):
        media_range, *parameters = entry.split(";")
        media_range = media_range.strip().lower()
        if media_range.count("/") != 1:
            continue
        quality = 1.0
        for parameter in parameters:
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = -1.0
        if 0.0 <= quality <= 1.0:
            entries.append((media_range, quality))
    return entries


def _specificity(media_range: str, media_type: str) -> int | None:
    """2 if the range names the type, 1 for ``type/*``, 0 for ``*/*``; ``None`` if no match."""
    if media_range == media_type:
        return 2
    if media_range == "*/*":
        return 0
    major, _, minor = media_range.partition("/")
    if minor == "*" and media_type.startswith(major + "/"):
        return 1
    return None


def negotiate(accept: str | None) -> Representation:
    """Choose a representation for an ``Accept`` header (RFC 9110 §12.5.1).

    Each representation takes the ``q`` of the most specific range matching one of its media
    types. The highest ``q`` wins, then a type named explicitly over a wildcard, then the order of
    ``NEGOTIATED``. Without ``Accept``, or with nothing acceptable, HTML.
    """
    if not accept:
        return Representation.HTML
    entries = _accept_entries(accept)
    best: tuple[float, int, int] | None = None
    chosen = Representation.HTML
    for rank, (representation, media_types) in enumerate(NEGOTIATED):
        matches = [
            (specificity, quality)
            for media_type in media_types
            for media_range, quality in entries
            if (specificity := _specificity(media_range, media_type)) is not None
        ]
        if not matches:
            continue
        specificity, quality = max(matches, key=lambda match: match[0])
        if quality <= 0:
            continue
        score = (quality, specificity, -rank)
        if best is None or score > best:
            best, chosen = score, representation
    return chosen


def entity_tag(version_etag: str, representation: Representation) -> str:
    """Return the weak ``ETag`` of one representation of a stored version."""
    opaque = version_etag if _ETAGC.fullmatch(version_etag) else _hash(version_etag)
    return f'W/"{opaque}-{representation.value}"'


def _hash(text: str) -> str:
    return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()


def if_none_match(header: str | None, etag: str) -> bool:
    """Return whether ``If-None-Match`` matches ``etag`` (``*`` or weak comparison)."""
    if header is None:
        return False
    if header.strip() == "*":
        return True
    opaque = etag.removeprefix("W/")
    return any(candidate.strip().removeprefix("W/") == opaque for candidate in header.split(","))


def placeholder_html(document: dict[str, Any], chain: list[str], version: int | None) -> str:
    """Return the minimal HTML page: the record's label and links to JSON-LD and Turtle.

    Step 1.21 replaces it with the server-rendered Public Reader.
    """
    labels, default_label = labels_from_record(document)
    label = resolve_label(labels, default_label, chain)
    label_lang = next(
        (tag for tag, text in labels.items() if text == label and tag != UNDETERMINED), None
    )
    version_query = f"version={version}&" if version is not None else ""
    jsonld_href = html.escape(f"?{version_query}format={Representation.JSONLD.value}")
    turtle_href = html.escape(f"?{version_query}format={Representation.TURTLE.value}")
    title = html.escape(label)
    heading_lang = f' lang="{html.escape(label_lang)}"' if label_lang else ""
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{html.escape(chain[0])}">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n"
        f'<link rel="alternate" type="{JSONLD_MEDIA_TYPE}" href="{jsonld_href}">\n'
        f'<link rel="alternate" type="{TURTLE_MEDIA_TYPE}" href="{turtle_href}">\n'
        "</head>\n"
        "<body>\n"
        "<main>\n"
        f"<h1{heading_lang}>{title}</h1>\n"
        "<ul>\n"
        f'<li><a href="{jsonld_href}" type="{JSONLD_MEDIA_TYPE}">JSON-LD</a></li>\n'
        f'<li><a href="{turtle_href}" type="{TURTLE_MEDIA_TYPE}">Turtle</a></li>\n'
        "</ul>\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


async def public_document(
    found: RecordVersion, uri: str, chain: list[str], session: AsyncSession
) -> dict[str, Any]:
    """The JSON-LD every public representation is made from: stripped, labels transcluded."""
    return await hydrate_labels(strip_internal_iris(found.data, uri), chain, session)


def parse_record_id(value: str, base_uri: str) -> UUID:
    """Return the record id of a ``{uuid}`` path segment in canonical form, else raise 404."""
    record_id = record_id_from_uri(base_uri + value, base_uri=base_uri)
    if record_id is None or "/" in value:
        raise not_found()
    return record_id


@router.get(
    "/{record_uuid}",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {JSONLD_MEDIA_TYPE: {}, TURTLE_MEDIA_TYPE: {}, HTML_MEDIA_TYPE: {}}
        },
        status.HTTP_301_MOVED_PERMANENTLY: {"description": "Merged (LAE-REC-003)"},
        status.HTTP_304_NOT_MODIFIED: {"description": "If-None-Match matched"},
        status.HTTP_404_NOT_FOUND: {"description": "LAE-REC-001, LAE-REC-004"},
        status.HTTP_410_GONE: {"description": "Deleted (LAE-REC-002)"},
    },
)
async def dereference(
    record_uuid: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    chain: Annotated[list[str], Depends(locale_chain)],
    version: Annotated[int | None, Query(description="Serve this stored version")] = None,
    format_: Annotated[
        Representation | None, Query(alias="format", description="Overrides Accept")
    ] = None,
) -> Response:
    """Serve a published record as JSON-LD, Turtle, or HTML (see the module docstring)."""
    record_id = parse_record_id(record_uuid, settings.base_uri)
    bind_log_context(record_id=record_id)
    record = await session.get(Record, record_id)
    if record is None or record.version < 1:
        raise not_found()
    merged_location = ""
    if record.merged_into is not None:
        merged_location = record_uri(record.merged_into, base_uri=settings.base_uri)
        if format_ is not None:
            merged_location += f"?format={format_.value}"
    check_status(record, merged_location)
    if version is None:
        found = current_version(record)
    else:
        maybe = await load_version(session, record, version)
        if maybe is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, VERSION_NOT_FOUND, {"version": version})
        found = maybe
    representation = format_ or negotiate(request.headers.get("accept"))
    headers = {
        "ETag": entity_tag(found.etag, representation),
        "Last-Modified": format_datetime(found.modified.astimezone(UTC), usegmt=True),
        "Vary": VARY,
    }
    if if_none_match(request.headers.get("if-none-match"), headers["ETag"]):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    document = await public_document(found, record.uri, chain, session)
    if representation is Representation.JSONLD:
        return Response(
            orjson.dumps(ordered_record(document)),
            media_type=jsonld_content_type(found.context_version),
            headers=headers,
        )
    if representation is Representation.TURTLE:
        turtle = await asyncio.to_thread(to_turtle, document, found.context_version)
        return Response(turtle, media_type=TURTLE_MEDIA_TYPE, headers=headers)
    page = placeholder_html(document, chain, version)
    return Response(page, media_type=HTML_MEDIA_TYPE, headers=headers)


@router.get(
    "/{record_uuid}/node/{node_id}",
    response_class=Response,
    status_code=status.HTTP_303_SEE_OTHER,
    responses={
        status.HTTP_303_SEE_OTHER: {"description": "See the record that owns the node"},
        status.HTTP_404_NOT_FOUND: {"description": "LAE-REC-001"},
    },
)
async def dereference_node(
    record_uuid: str,
    node_id: str,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> Response:
    """Redirect (303) an internal or derived node IRI to the record that owns it."""
    base = settings.base_uri
    record_id = record_id_from_uri(f"{base}{record_uuid}/node/{node_id}", base_uri=base)
    if record_id is None or "/" in record_uuid or "/" in node_id:
        raise not_found()
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": record_uri(record_id, base_uri=base)},
    )
