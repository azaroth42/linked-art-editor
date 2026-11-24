"""Diagnostics of the sync worker (Phase 1 Step 1.11, contract §2.4).

An ``EventError`` says why one outbox event could not be translated into a SPARQL Update or was
refused by QLever. It is an operator diagnostic: ``str(error)`` is ``"<code> <json params>"`` and
is what ``outbox_events.last_error`` and the log line hold. The codes need no ``errors.json``
entry because no user sees them.

QLever being unavailable (no answer, a timeout, 401, 403, 408, 429, 5xx) is not an
``EventError``: it is never written to an event and costs no attempt
(``lae.sync.worker.classify``).

``LAE-SYNC-004`` keeps the first ``REJECTED_BODY_CHARS`` characters of QLever's answer **after**
the access token was removed from it (contract §8.6): the worker passes ``rejected()`` the
result of ``lae.sync.worker.redacted_body`` (redact, then truncate), and ``__str__`` encodes it
as JSON only then, so neither a token across the cut nor a token that JSON escapes survives.
``REJECTED_BODY_CHARS`` is the one limit the worker uses for every body it stores or logs.
"""

from __future__ import annotations

import json
from typing import Any, Final

REVISION_MISSING: Final = "LAE-SYNC-001"
"""A revision the event needs does not exist, or the record row does not."""

UNREADABLE: Final = "LAE-SYNC-002"
"""Stored N-Triples or JSON-LD cannot be read (zstd, not canonical, not JSON), or a subject
cannot be written as an IRI."""

UNTRANSLATABLE: Final = "LAE-SYNC-003"
"""Kind ``merge`` or ``split`` (later steps), ``publish`` without ``to_version``, ``delete``
without ``from_version``."""

REJECTED: Final = "LAE-SYNC-004"
"""QLever rejected the update of this event (a status ``classify`` calls ``rejected``)."""

SYNC_CODES: Final = (REVISION_MISSING, UNREADABLE, UNTRANSLATABLE, REJECTED)

REJECTED_BODY_CHARS: Final = 2000
"""How much of QLever's answer a ``LAE-SYNC-004``, a ``Refusal`` or a log line keeps."""


class EventError(Exception):
    """One event cannot be translated, or QLever rejected it; counted toward its attempts."""

    def __init__(self, code: str, **params: Any) -> None:
        super().__init__(code)
        self.code = code
        self.params = params

    def __str__(self) -> str:
        return f"{self.code} {json.dumps(self.params, sort_keys=True, default=str)}"


def rejected(event_id: int, status: int | None, body: str) -> EventError:
    """``LAE-SYNC-004`` for one event, keeping the first 2000 characters of QLever's answer."""
    return EventError(REJECTED, event=event_id, status=status, body=body[:REJECTED_BODY_CHARS])
