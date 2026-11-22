"""Record ids, public record URIs, and internal node IRIs (spec §3.1, §3.3; decisions A6, A7).

Every record id is a UUIDv7 and its public URI is flat: ``<base_uri><uuid>``. Every embedded node
gets an internal IRI ``<record_uri>/node/<uuidv7>``; the namespace ``<record_uri>/node/`` belongs to
the record (ADR-0003). URIs are compared as strings, so only the canonical form minted here counts:
a lower-case hyphenated UUID (``str(UUID)``), optionally followed by ``/node/`` and a canonical
UUIDv7, and nothing else.

``base_uri`` defaults to ``Settings.base_uri`` from ``get_settings()``; code that holds its own
``Settings`` (the API, through ``get_app_settings``) passes ``base_uri=settings.base_uri``.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from uuid_utils.compat import uuid7

from lae.settings import get_settings

NODE_PATH: Final = "/node/"
"""Separator between a record URI and the id of one of its internal nodes."""

NODE_UUID_VERSION: Final = 7
"""UUID version of node ids; anything else after ``/node/`` is not an internal node IRI."""


def new_uuid7() -> UUID:
    """Mint a time-ordered UUIDv7 as a standard-library ``uuid.UUID``."""
    return uuid7()


def _base(base_uri: str | None) -> str:
    """Return ``base_uri``, or the configured ``Settings.base_uri`` when it is ``None``."""
    return get_settings().base_uri if base_uri is None else base_uri


def _canonical_uuid(text: str, version: int | None = None) -> UUID | None:
    """Parse ``text`` if it is exactly ``str(UUID)`` (of ``version``, when given); else ``None``."""
    try:
        value = UUID(text)
    except ValueError:
        return None
    if str(value) != text or (version is not None and value.version != version):
        return None
    return value


def _split_local(iri: str, base_uri: str | None) -> tuple[UUID, UUID | None] | None:
    """Split a local record URI or node IRI into ``(record_id, node_id | None)``; else ``None``."""
    base = _base(base_uri)
    if not iri.startswith(base):
        return None
    record_part, separator, node_part = iri[len(base) :].partition(NODE_PATH)
    record_id = _canonical_uuid(record_part)
    if record_id is None:
        return None
    if not separator:
        return record_id, None
    node_id = _canonical_uuid(node_part, NODE_UUID_VERSION)
    if node_id is None:
        return None
    return record_id, node_id


def record_uri(record_id: UUID, *, base_uri: str | None = None) -> str:
    """Return the public URI of a record: ``<base_uri><record_id>``."""
    return _base(base_uri) + str(record_id)


def record_id_from_uri(uri: str, *, base_uri: str | None = None) -> UUID | None:
    """Return the record id of a local record URI or of one of its node IRIs; else ``None``."""
    parts = _split_local(uri, base_uri)
    return None if parts is None else parts[0]


def node_iri(record_uri: str) -> str:
    """Mint a new internal node IRI ``<record_uri>/node/<uuidv7>`` for ``record_uri``."""
    return f"{record_uri}{NODE_PATH}{new_uuid7()}"


def is_internal_node_iri(
    iri: object, record_uri: str | None = None, *, base_uri: str | None = None
) -> bool:
    """True if ``iri`` is ``<record_uri>/node/<uuidv7>``, for any local record if none is given.

    With ``record_uri`` the record need not be local (corpus records keep their own URIs in
    tests; Spike S3 rule R15). Non-string values are never node IRIs, so JSON can be walked as is.
    """
    if not isinstance(iri, str):
        return False
    if record_uri is not None:
        prefix = record_uri + NODE_PATH
        return (
            iri.startswith(prefix)
            and _canonical_uuid(iri[len(prefix) :], NODE_UUID_VERSION) is not None
        )
    parts = _split_local(iri, base_uri)
    return parts is not None and parts[1] is not None


def owner_of(iri: str, *, base_uri: str | None = None) -> str:
    """Return the URI of the local record that owns ``iri`` (a node IRI or the record URI itself).

    Raises ``ValueError`` if ``iri`` is neither a local record URI nor one of its node IRIs.
    """
    parts = _split_local(iri, base_uri)
    if parts is None:
        raise ValueError(f"not a local record URI or internal node IRI: {iri!r}")
    return record_uri(parts[0], base_uri=base_uri)
