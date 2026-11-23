"""Record aliases: the URIs a record had before it got ``<base_uri><uuid>`` (ADR-0030).

``lae bootstrap load`` gives every record that arrives with a URI from elsewhere a new
``<base_uri><uuidv7>`` URI and writes the old one to ``record_aliases``. The functions here are
the lookups over that table, next to the pure URI functions of ``lae.ids``:

- ``resolve_alias`` / ``resolve_aliases``: alias URI → record id.
- ``resolve_record_id``: any URI a record answers to (its own URI, one of its node IRIs, or an
  alias) → record id. Code that takes a URI from outside (an import, a pasted reference, a
  SPARQL result from before a re-mint) uses this instead of ``lae.ids.record_id_from_uri``.
- ``local_alias_candidates`` / ``resolve_local_alias``: for a request the editor received, the
  alias URIs it may have been made for. The editor can only be asked for URIs on its own host
  (the host of ``Settings.base_uri``), so only those can be redirected (``lae.api.public``).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Text, any_, bindparam, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from lae.db.models import RecordAlias
from lae.ids import record_id_from_uri

BOOTSTRAP_SOURCE: Final = "bootstrap"
"""``record_aliases.source`` of aliases written by ``lae bootstrap load``."""

_SCHEMES: Final = ("https", "http")

_ALIASES: Final = select(RecordAlias.alias_uri, RecordAlias.record_id).where(
    RecordAlias.alias_uri == any_(bindparam("uris", type_=ARRAY(Text)))
)


async def resolve_aliases(session: AsyncSession, uris: Collection[str]) -> dict[str, UUID]:
    """Return ``{alias URI: record id}`` for those of ``uris`` that are aliases (one query)."""
    if not uris:
        return {}
    rows = await session.execute(_ALIASES, {"uris": sorted(set(uris))})
    return {alias: record_id for alias, record_id in rows}


async def resolve_alias(session: AsyncSession, uri: str) -> UUID | None:
    """Return the id of the record that ``uri`` is an alias of, else ``None``."""
    return (await resolve_aliases(session, [uri])).get(uri)


async def resolve_record_id(
    session: AsyncSession, uri: str, *, base_uri: str | None = None
) -> UUID | None:
    """Return the id of the record ``uri`` names: a local record URI or node IRI, or an alias.

    A local URI is answered without a query and without checking that the record exists.
    """
    local = record_id_from_uri(uri, base_uri=base_uri)
    if local is not None:
        return local
    return await resolve_alias(session, uri)


def local_alias_candidates(path: str, query: str, base_uri: str) -> list[str]:
    """Return the alias URIs a request for ``path`` (and ``query``) on the editor's host may mean.

    The host is that of ``base_uri``, never the request's ``Host`` header. Most specific first:
    with the query string before without it, and the scheme of ``base_uri`` before the other of
    ``http`` / ``https`` (old URIs are often ``http``). ``path`` is the path as it was sent,
    percent-encoding included, starting with ``/``.
    """
    base = urlsplit(base_uri)
    if not base.netloc or not path.startswith("/"):
        return []
    schemes = [base.scheme, *(scheme for scheme in _SCHEMES if scheme != base.scheme)]
    targets = [f"{path}?{query}", path] if query else [path]
    return [f"{scheme}://{base.netloc}{target}" for target in targets for scheme in schemes]


async def resolve_local_alias(
    session: AsyncSession, path: str, query: str, base_uri: str
) -> tuple[str, UUID] | None:
    """Return ``(alias URI, record id)`` for the first candidate that is an alias, else ``None``."""
    candidates = local_alias_candidates(path, query, base_uri)
    found = await resolve_aliases(session, candidates)
    for candidate in candidates:
        if candidate in found:
            return candidate, found[candidate]
    return None
