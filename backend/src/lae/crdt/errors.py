"""Diagnostic codes of the ``crdt-ws`` internal RPC (``LAE-CRDT-NNN``).

Bodies are ``{code, path, params}`` (``lae.api.errors``). Every code has an English message
under the same key in ``frontend/public/locales/en/errors.json`` (a test keeps the two equal).
"""

from __future__ import annotations

from typing import Final

INTERNAL_TOKEN_REQUIRED: Final = "LAE-CRDT-001"
"""401: ``X-Internal-Token`` is missing or wrong, or the server has no ``LAE_INTERNAL_TOKEN``."""

RECORD_NOT_FOUND: Final = "LAE-CRDT-002"
"""404: there is no record with this id."""

DRAFT_NOT_FOUND: Final = "LAE-CRDT-003"
"""404: the record has no draft."""

DRAFT_EXISTS: Final = "LAE-CRDT-004"
"""409: the record already has a draft, so one cannot be created from the given content."""

RECORD_NOT_EDITABLE: Final = "LAE-CRDT-005"
"""409: the record is deleted or merged."""

INVALID_UPDATE: Final = "LAE-CRDT-006"
"""422: the update is not base64 of a Yjs update."""

DRAFT_SUPERSEDED: Final = "LAE-CRDT-007"
"""409: the draft was published or discarded while the update was on its way."""

INVALID_CONTENT: Final = "LAE-CRDT-008"
"""422: the content cannot be hydrated; param ``projection_code`` (``LAE-PROJ-NNN``)."""

CRDT_WS_UNAVAILABLE: Final = "LAE-CRDT-009"
"""503 (raised by ``lae.crdt.client``): the ``crdt-ws`` process cannot be reached."""

CRDT_CODES: Final = (
    INTERNAL_TOKEN_REQUIRED,
    RECORD_NOT_FOUND,
    DRAFT_NOT_FOUND,
    DRAFT_EXISTS,
    RECORD_NOT_EDITABLE,
    INVALID_UPDATE,
    DRAFT_SUPERSEDED,
    INVALID_CONTENT,
    CRDT_WS_UNAVAILABLE,
)
"""Every code of this module."""
