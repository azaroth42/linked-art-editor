"""Codes and errors of publish and discard (``LAE-PUB-NNN``).

Bodies are ``{code, path, params}`` (``lae.api.errors``). Every code has an English message under
the same key in ``frontend/public/locales/en/errors.json`` (a test keeps the two equal). None of
these errors has written anything: the transaction is rolled back, or was never opened, and the
draft is as it was.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final
from uuid import UUID

from fastapi import status

from lae.api.errors import ApiError
from lae.validation import Diagnostic

NO_DRAFT: Final = "LAE-PUB-001"
"""409: the record named by param ``id`` has no draft. It is the record being published, or a
never-published record the draft refers to (a dependent stub), which cannot be published
without one."""

WARNINGS_NOT_ACCEPTED: Final = "LAE-PUB-002"
"""409: validation found warnings the request did not accept. Params: ``records`` (record id →
every warning diagnostic of that record, which is what a retry has to accept) and ``unaccepted``
(``[{record_id, code, path}]``, the ones missing from the request)."""

VALIDATION_FAILED: Final = "LAE-PUB-003"
"""422: a document of the bundle has a violation. Param ``records``: record id → all of that
record's diagnostics, for every record that has a violation."""

DRAFT_CHANGED: Final = "LAE-PUB-004"
"""409: the draft of the record named by param ``id`` was edited, published or discarded after
the snapshot that was validated. Nothing was published and no edit is lost; publish again."""

NOT_PUBLISHABLE: Final = "LAE-PUB-005"
"""409: the record named by param ``id`` is deleted or merged (param ``status``)."""

STALE_BASE: Final = "LAE-PUB-006"
"""409: the draft of record ``id`` was hydrated from version ``base_version`` but the record is
now at ``version``. Publishing would overwrite a version the draft never saw."""

PUBLISH_CODES: Final = (
    NO_DRAFT,
    WARNINGS_NOT_ACCEPTED,
    VALIDATION_FAILED,
    DRAFT_CHANGED,
    NOT_PUBLISHABLE,
    STALE_BASE,
)
"""Every code of this module."""


class PublishError(ApiError):
    """A publish or discard that was refused; nothing was written."""


def _grouped(records: Mapping[UUID, list[Diagnostic]]) -> dict[str, list[dict[str, Any]]]:
    return {
        str(record_id): [item.model_dump(mode="json") for item in items]
        for record_id, items in sorted(records.items())
    }


def no_draft(record_id: UUID) -> PublishError:
    """409 ``LAE-PUB-001``."""
    return PublishError(status.HTTP_409_CONFLICT, NO_DRAFT, {"id": str(record_id)})


def warnings_not_accepted(
    warnings: Mapping[UUID, list[Diagnostic]], unaccepted: list[tuple[UUID, str, str]]
) -> PublishError:
    """409 ``LAE-PUB-002`` with every current warning and the ones that were not accepted."""
    return PublishError(
        status.HTTP_409_CONFLICT,
        WARNINGS_NOT_ACCEPTED,
        {
            "records": _grouped(warnings),
            "unaccepted": [
                {"record_id": str(record_id), "code": code, "path": path}
                for record_id, code, path in sorted(unaccepted)
            ],
        },
    )


def validation_failed(records: Mapping[UUID, list[Diagnostic]]) -> PublishError:
    """422 ``LAE-PUB-003`` with the diagnostics of every record that has a violation."""
    return PublishError(
        status.HTTP_422_UNPROCESSABLE_CONTENT, VALIDATION_FAILED, {"records": _grouped(records)}
    )


def draft_changed(record_id: UUID) -> PublishError:
    """409 ``LAE-PUB-004``."""
    return PublishError(status.HTTP_409_CONFLICT, DRAFT_CHANGED, {"id": str(record_id)})


def not_publishable(record_id: UUID, record_status: str) -> PublishError:
    """409 ``LAE-PUB-005``."""
    return PublishError(
        status.HTTP_409_CONFLICT, NOT_PUBLISHABLE, {"id": str(record_id), "status": record_status}
    )


def stale_base(record_id: UUID, base_version: int, version: int) -> PublishError:
    """409 ``LAE-PUB-006``."""
    return PublishError(
        status.HTTP_409_CONFLICT,
        STALE_BASE,
        {"id": str(record_id), "base_version": base_version, "version": version},
    )
