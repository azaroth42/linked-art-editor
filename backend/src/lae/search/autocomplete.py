"""Entity autocomplete: ``GET /api/search/autocomplete`` (Phase 1 Step 1.13; spec §6.1).

``?q=&types=Person,Group&limit=20&locale=`` answers ``[{id, type, label, context, source}]``, the
published entities (local records and sidecar entities such as AAT terms) whose name starts with
what was typed, best first. Any signed-in user may call it (``current_user``).

**Two sources, one ranking.** Spike S1 (``docs/spikes/S1-qlever.md``) found that QLever's text
index is built once, so prefix search never sees a literal inserted by SPARQL Update. Every
request therefore asks both:

1. QLever, with the form S1 measured: ``?text ql:contains-entity ?lit ; ql:contains-word "w1* w2*"``
   (``TEXT_INDEX = from_literals`` makes every literal a text record that contains itself), joined
   to the entities whose ``rdfs:label`` is ``?lit`` or that are ``identified_by`` a ``Name`` with
   that ``content``, restricted to the requested classes (``rdf:type``). Internal node IRIs are
   filtered out: an embedded ``Person`` without an id of its own is a node, not an entity.
2. The **overlay**: ``entity_labels`` rows updated after ``sync_state.qlever_index_watermark_time``
   (the snapshot the live index was built from, written by ``lae bootstrap build-index``) whose
   ``default_label`` starts with ``q``, or has a word that does. Those are the entities published
   since the index was built. Without a recorded time the whole table is searched (nothing is known
   to be indexed), and so it is when no QLever is configured.

Both rank a match the same way, so their results merge into one list: a label that starts with
``q`` (case-insensitively) comes before one that only has a word starting with it, and shorter
labels come first (``rank_key``); ties go by URI. A URI found by both is listed once. The best
``limit`` of that list are then ordered for display (``display_order``): still those starting with
``q`` first, but within each group the entities records use most (``entity_labels.usage_count``,
Phase 2 Step 2.1) come first, then shorter labels.

**Labels and context.** Each result's ``label`` is chosen for the caller's locale chain
(``Accept-Language``, with ``locale`` put first) from its ``entity_labels`` row with
``lae.labels.transclude.resolve_label``, exactly as ``hydrate_labels`` does for references, and
``source`` is the row's dataset (``local``, ``aat``, ...); an entity QLever knows but that has no
row keeps the literal that matched and counts as ``local``. A second QLever query fetches
disambiguation ``context`` for the results only: birth and death years for a ``Person``
(``1606–1669``, ``1606–``, ``–1669``) and the label of the parent for a ``Place`` (``part_of``,
``crm:P89_falls_within``) or a concept (``broader``, ``skos:broader``), resolved like ``label``.

**Escaping.** Words of ``q`` reach QLever only as runs of letters and digits (QLever splits text at
everything else) inside ``string_literal``; the ranking prefix goes through ``string_literal`` as
typed. ``q`` shorter than 2 characters, after trimming, is refused (``LAE-SEARCH-001``).

QLever results are not cached (Phase 1 guide, Common Mistakes). If QLever is configured but fails,
the answer is 503 ``LAE-SEARCH-003`` rather than the overlay alone, which would hide entities that
exist and invite duplicates.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import Annotated, Any, Final

import httpx
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy import Text, any_, bindparam, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from lae.api.deps import locale_chain
from lae.api.errors import ApiError
from lae.api.records import RECORD_TYPES
from lae.app import get_app_settings, get_http_client
from lae.auth import CurrentUser, current_user
from lae.bootstrap.common import QLEVER_INDEX_WATERMARK_TIME
from lae.bootstrap.export import class_iri
from lae.db.models import EntityLabel, EntityLabelStatus, SyncState
from lae.db.session import get_session
from lae.labels.langtags import normalize_tag
from lae.labels.transclude import resolve_label
from lae.labels.upsert import LOCAL_SOURCE
from lae.logging import bind_log_context, get_logger
from lae.settings import Settings
from lae.sparql.terms import iri, string_literal
from lae.sync.qlever_client import QLeverClient, QLeverError

MIN_QUERY_LENGTH: Final = 2
"""Shortest ``q`` accepted, after trimming (Step 1.13)."""

MAX_QUERY_LENGTH: Final = 200
"""Longest ``q`` accepted; nobody types more into an autocomplete field."""

DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 100

QLEVER_TIMEOUT_S: Final = 10.0
"""Time one QLever query may take before the request fails with ``LAE-SEARCH-003``."""

NOT_AT_START: Final = 1_000_000
"""Added to the rank of a label that does not start with ``q`` (longer than any label)."""

QUERY_LENGTH: Final = "LAE-SEARCH-001"
"""422: ``q`` is shorter than ``min_length`` or longer than ``max_length`` (params)."""

UNKNOWN_TYPE: Final = "LAE-SEARCH-002"
"""422: ``types`` names a class that is not a record class; params ``type``, ``allowed``."""

INDEX_UNAVAILABLE: Final = "LAE-SEARCH-003"
"""503: the search index (QLever) did not answer."""

SEARCH_CODES: Final = (QUERY_LENGTH, UNKNOWN_TYPE, INDEX_UNAVAILABLE)
"""Every code this router emits (English text in ``frontend/public/locales/en/errors.json``)."""

PERSON: Final = "Person"
PLACE: Final = "Place"
CONCEPT_TYPES: Final = frozenset({"Type", "Language", "Material", "Currency", "MeasurementUnit"})
"""Classes whose ``part_of`` is ``skos:broader`` in the pinned context."""

PREFIXES: Final = (
    "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
    "PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>\n"
    "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
    "PREFIX ql: <http://qlever.cs.uni-freiburg.de/builtin-functions/>\n"
)

NODE_IRI_PATTERN: Final = "/node/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
"""Ends every internal and derived node IRI (``<record_uri>/node/<uuid>``, ``lae.ids``)."""

_WORD: Final = re.compile(r"[^\W_]+")
"""A run of letters and digits: what QLever's text index treats as one word."""

_YEAR: Final = re.compile(r"\s*(-?)0*(\d+)-")
"""The (astronomical) year of an ``xsd:dateTime`` lexical form, without leading zeros."""

_LIKE_SPECIAL: Final = re.compile(r"([\\%_])")

logger = get_logger(__name__)

router = APIRouter(prefix="/api/search", tags=["search"])


class AutocompleteResult(BaseModel):
    """One entity in the answer of ``GET /api/search/autocomplete``."""

    id: str
    """The entity's URI."""

    type: str
    """Its Linked Art class, e.g. ``Person``."""

    label: str
    """Its label in the caller's locale chain."""

    context: str | None
    """Disambiguation: ``1606–1669`` for a person, the parent's label for a place or concept."""

    source: str
    """``entity_labels.source``: ``local`` for this installation's records, else the dataset."""


@dataclass(frozen=True, slots=True)
class Hit:
    """A matching entity before labels and context are resolved."""

    uri: str
    type: str
    rank: int
    label: str
    """The label that matched; used only if the entity has no ``entity_labels`` row."""


# --- query text ---------------------------------------------------------------------------------


def normalize_query(q: str) -> str:
    """``q`` trimmed, with runs of whitespace made single spaces."""
    return " ".join(q.split())


def query_words(q: str) -> list[str]:
    """The words of ``q`` as QLever's text index splits them, lower case."""
    return _WORD.findall(q.lower())


def rank_key(label: str, prefix: str) -> int:
    """Rank of a matching label: shorter is better, and starting with ``prefix`` beats all else.

    ``prefix`` is the normalized query in lower case. QLever computes the same number in SPARQL
    (``build_search_query``), so the two sources sort together.
    """
    return (0 if label.lower().startswith(prefix) else NOT_AT_START) + len(label)


def like_escape(text: str) -> str:
    """Escape ``%``, ``_`` and the escape character itself for ``LIKE ... ESCAPE '\\'``."""
    return _LIKE_SPECIAL.sub(r"\\\1", text)


@cache
def class_iris(context_version: int) -> dict[str, str]:
    """Class IRI of every record class (``RECORD_TYPES``) in the pinned context."""
    found: dict[str, str] = {}
    for type_name in sorted(RECORD_TYPES):
        klass = class_iri(type_name, context_version)
        if klass is not None:
            found[type_name] = klass
    return found


def build_search_query(
    words: Sequence[str], prefix: str, classes: Iterable[str], limit: int
) -> str:
    """The QLever prefix search: the best ``limit`` entities of ``classes`` matching every word.

    Each word is searched as a prefix. The text scan is repeated inside both branches of the
    ``UNION`` so that QLever joins its few rows to the label and type triples instead of reading
    those predicates whole (measured on QLever 0.6.0).
    """
    if not words:
        raise ValueError("the query has no words")
    text = string_literal(" ".join(f"{word}*" for word in words))
    allowed = ", ".join(iri(klass) for klass in sorted(classes))
    return (
        PREFIXES
        + "SELECT ?entity (SAMPLE(?class) AS ?type) (MIN(?key) AS ?rank) (SAMPLE(?lit) AS ?label)"
        + " WHERE {\n"
        + "  {\n"
        + f"    ?text ql:contains-entity ?lit ; ql:contains-word {text} .\n"
        + "    ?entity rdfs:label ?lit .\n"
        + "    ?entity rdf:type ?class .\n"
        + "  } UNION {\n"
        + f"    ?text ql:contains-entity ?lit ; ql:contains-word {text} .\n"
        + "    ?name crm:P190_has_symbolic_content ?lit .\n"
        + "    ?name rdf:type crm:E33_E41_Linguistic_Appellation .\n"
        + "    ?entity crm:P1_is_identified_by ?name .\n"
        + "    ?entity rdf:type ?class .\n"
        + "  }\n"
        + f"  FILTER(?class IN ({allowed}))\n"
        + f"  FILTER(!REGEX(STR(?entity), {string_literal(NODE_IRI_PATTERN)}))\n"
        + f"  BIND(IF(STRSTARTS(LCASE(STR(?lit)), {string_literal(prefix)}), 0, {NOT_AT_START})"
        + " + STRLEN(STR(?lit)) AS ?key)\n"
        + "}\n"
        + "GROUP BY ?entity\n"
        + "ORDER BY ?rank ?entity\n"
        + f"LIMIT {int(limit)}"
    )


def _values(uris: Iterable[str]) -> str:
    return " ".join(iri(uri) for uri in sorted(set(uris)))


def build_context_query(
    persons: Iterable[str], places: Iterable[str], concepts: Iterable[str]
) -> str | None:
    """The disambiguation query for the given entities; ``None`` if there is nothing to ask.

    Rows carry ``?entity`` and, where they exist, ``?birth`` / ``?death`` (``xsd:dateTime``) for
    persons, and ``?parent`` with its ``rdfs:label`` ``?parentLabel`` for places and concepts.
    """
    blocks = []
    if persons := _values(persons):
        blocks.append(
            f"{{ VALUES ?entity {{ {persons} }}\n"
            "    OPTIONAL { ?entity crm:P98i_was_born/crm:P4_has_time-span/"
            "crm:P82a_begin_of_the_begin ?birth }\n"
            "    OPTIONAL { ?entity crm:P100i_died_in/crm:P4_has_time-span/"
            "crm:P82b_end_of_the_end ?death } }"
        )
    for uris, predicate in ((places, "crm:P89_falls_within"), (concepts, "skos:broader")):
        if values := _values(uris):
            blocks.append(
                f"{{ VALUES ?entity {{ {values} }}\n"
                f"    OPTIONAL {{ ?entity {predicate} ?parent"
                " OPTIONAL { ?parent rdfs:label ?parentLabel } } }"
            )
    if not blocks:
        return None
    return (
        PREFIXES
        + "SELECT ?entity ?birth ?death ?parent ?parentLabel WHERE {\n  "
        + "\n  UNION\n  ".join(blocks)
        + "\n}"
    )


# --- merging and context ------------------------------------------------------------------------


def display_order(hits: Iterable[Hit], usage: Mapping[str, int]) -> list[Hit]:
    """``hits`` with those starting with ``q`` first, then most used, then by rank and URI."""
    return sorted(
        hits,
        key=lambda hit: (hit.rank >= NOT_AT_START, -usage.get(hit.uri, 0), hit.rank, hit.uri),
    )


def merge_hits(indexed: Iterable[Hit], overlay: Iterable[Hit], limit: int) -> list[Hit]:
    """Both sources in one ranking, each URI once (its best rank; QLever's on a tie), ``limit``."""
    best: dict[str, Hit] = {}
    for hit in (*indexed, *overlay):
        kept = best.get(hit.uri)
        if kept is None or hit.rank < kept.rank:
            best[hit.uri] = hit
    return sorted(best.values(), key=lambda hit: (hit.rank, hit.uri))[:limit]


def year_of(value: Any) -> str | None:
    """The year of an ``xsd:dateTime`` lexical form (``-43`` for 44 BCE), else ``None``."""
    if not isinstance(value, str):
        return None
    match = _YEAR.match(value)
    if match is None:
        return None
    sign, digits = match.groups()
    return f"-{digits}" if sign and digits != "0" else digits


def life_dates(births: Iterable[Any], deaths: Iterable[Any]) -> str | None:
    """``birth–death`` from the earliest birth and the latest death year; ``None`` if neither."""
    birth_years = [year for value in births if (year := year_of(value)) is not None]
    death_years = [year for value in deaths if (year := year_of(value)) is not None]
    birth = min(birth_years, key=int) if birth_years else ""
    death = max(death_years, key=int) if death_years else ""
    return f"{birth}–{death}" if birth or death else None


@dataclass(slots=True)
class _Context:
    births: set[str]
    deaths: set[str]
    parents: dict[str, str | None]
    """Parent URI → its ``rdfs:label`` in QLever (``None`` if it has none)."""


def collect_context(rows: Iterable[Mapping[str, Any]]) -> dict[str, _Context]:
    """Group the rows of the context query by entity."""
    found: dict[str, _Context] = {}
    for row in rows:
        entity = row.get("entity")
        if not isinstance(entity, str):
            continue
        context = found.setdefault(entity, _Context(set(), set(), {}))
        if isinstance(row.get("birth"), str):
            context.births.add(row["birth"])
        if isinstance(row.get("death"), str):
            context.deaths.add(row["death"])
        parent = row.get("parent")
        if isinstance(parent, str):
            label = row.get("parentLabel")
            if context.parents.get(parent) is None:
                context.parents[parent] = label if isinstance(label, str) else None
    return found


# --- database -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabelRow:
    """The ``entity_labels`` columns autocomplete reads."""

    uri: str
    type: str
    labels: dict[str, str]
    default_label: str
    source: str
    usage_count: int


async def watermark_time(session: AsyncSession) -> datetime | None:
    """``sync_state.qlever_index_watermark_time``, or ``None`` if it is not recorded."""
    value = await session.scalar(
        select(SyncState.value).where(SyncState.key == QLEVER_INDEX_WATERMARK_TIME)
    )
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning("watermark_time_unreadable", value=value)
        return None


async def overlay_rows(
    session: AsyncSession,
    q: str,
    types: Sequence[str],
    limit: int,
    *,
    since: datetime | None,
) -> list[LabelRow]:
    """Published ``entity_labels`` rows updated after ``since`` whose ``default_label`` matches.

    A label matches when it starts with ``q`` or has a word (after a space) that does, ignoring
    case. Rows come in ``rank_key`` order, so the first ``limit`` are the best.
    """
    pattern = like_escape(q)
    label = EntityLabel.default_label
    statement = (
        select(
            EntityLabel.uri,
            EntityLabel.type,
            EntityLabel.labels,
            EntityLabel.default_label,
            EntityLabel.source,
            EntityLabel.usage_count,
        )
        .where(
            EntityLabel.status == EntityLabelStatus.PUBLISHED,
            EntityLabel.type == any_(bindparam("types", list(types), type_=ARRAY(Text))),
            or_(label.ilike(f"{pattern}%", escape="\\"), label.ilike(f"% {pattern}%", escape="\\")),
        )
        .order_by(
            func.lower(label).like(f"{like_escape(q.lower())}%", escape="\\").desc(),
            func.length(label),
            EntityLabel.uri.collate("C"),
        )
        .limit(limit)
    )
    if since is not None:
        statement = statement.where(EntityLabel.updated_at > since)
    rows = await session.execute(statement)
    return [LabelRow(*row) for row in rows]


async def label_rows(session: AsyncSession, uris: Iterable[str]) -> dict[str, LabelRow]:
    """The ``entity_labels`` rows of ``uris`` (one query), by URI."""
    wanted = sorted(set(uris))
    if not wanted:
        return {}
    rows = await session.execute(
        select(
            EntityLabel.uri,
            EntityLabel.type,
            EntityLabel.labels,
            EntityLabel.default_label,
            EntityLabel.source,
            EntityLabel.usage_count,
        ).where(EntityLabel.uri == any_(bindparam("uris", wanted, type_=ARRAY(Text))))
    )
    return {row.uri: LabelRow(*row) for row in rows}


# --- the endpoint -------------------------------------------------------------------------------


def requested_types(types: str | None) -> list[str]:
    """The record classes named in ``types`` (comma-separated), or all of them; else 422."""
    if types is None or not types.strip():
        return sorted(RECORD_TYPES)
    names: list[str] = []
    for name in (part.strip() for part in types.split(",")):
        if not name or name in names:
            continue
        if name not in RECORD_TYPES:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                UNKNOWN_TYPE,
                {"type": name, "allowed": sorted(RECORD_TYPES)},
            )
        names.append(name)
    return names


def with_locale(chain: Sequence[str], locale: str | None) -> list[str]:
    """``chain`` with ``locale`` (a BCP 47 tag) moved to the front; a malformed one is ignored."""
    tag = normalize_tag(locale) if locale else None
    if tag is None:
        return list(chain)
    return [tag, *(item for item in chain if item != tag)]


def qlever_client(settings: Settings, http_client: httpx.AsyncClient) -> QLeverClient | None:
    """A client for the configured QLever, sharing the app's connection pool; else ``None``."""
    if not settings.qlever_url:
        return None
    token = settings.qlever_access_token
    return QLeverClient(
        settings.qlever_url,
        settings.qlever_update_url,
        token.get_secret_value() if token is not None else None,
        http_client=http_client,
        timeout=QLEVER_TIMEOUT_S,
    )


async def _indexed_hits(
    client: QLeverClient | None,
    words: Sequence[str],
    prefix: str,
    types: Sequence[str],
    limit: int,
    context_version: int,
) -> list[Hit]:
    if client is None or not words:
        return []
    by_type = class_iris(context_version)
    names = {by_type[name]: name for name in types if name in by_type}
    if not names:
        return []
    rows = await client.query(build_search_query(words, prefix, names, limit))
    hits = []
    for row in rows:
        uri, klass, rank, label = row["entity"], row["type"], row["rank"], row["label"]
        if isinstance(uri, str) and klass in names and isinstance(rank, int):
            hits.append(Hit(uri, names[klass], rank, label if isinstance(label, str) else uri))
    return hits


async def _overlay_hits(
    session: AsyncSession,
    q: str,
    prefix: str,
    types: Sequence[str],
    limit: int,
    *,
    indexed: bool,
) -> list[Hit]:
    since = await watermark_time(session) if indexed else None
    rows = await overlay_rows(session, q, types, limit, since=since)
    return [
        Hit(row.uri, row.type, rank_key(row.default_label, prefix), row.default_label)
        for row in rows
    ]


def _parent_label(
    parents: Mapping[str, str | None], rows: Mapping[str, LabelRow], chain: Sequence[str]
) -> str | None:
    labels = []
    for parent, fallback in parents.items():
        row = rows.get(parent)
        label = resolve_label(row.labels, row.default_label, chain) if row else fallback
        if label:
            labels.append(label)
    return min(labels) if labels else None


@router.get(
    "/autocomplete",
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "LAE-SEARCH-001, LAE-SEARCH-002"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "LAE-SEARCH-003"},
    },
)
async def autocomplete(
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    chain: Annotated[list[str], Depends(locale_chain)],
    q: Annotated[str, Query(description="What was typed; at least 2 characters.")] = "",
    types: Annotated[
        str | None, Query(description="Record classes, comma-separated (default: all).")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    locale: Annotated[
        str | None, Query(description="BCP 47 tag tried before Accept-Language.")
    ] = None,
) -> list[AutocompleteResult]:
    """Published entities whose name starts with ``q``, best first (see the module docstring)."""
    started = time.perf_counter()
    bind_log_context(user_id=user.id)
    q = normalize_query(q)
    if not MIN_QUERY_LENGTH <= len(q) <= MAX_QUERY_LENGTH:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            QUERY_LENGTH,
            {"min_length": MIN_QUERY_LENGTH, "max_length": MAX_QUERY_LENGTH},
        )
    names = requested_types(types)
    chain = with_locale(chain, locale)
    prefix = q.lower()
    client = qlever_client(settings, http_client)
    # QLever and Postgres are asked at the same time; the session runs one statement at a time.
    search = asyncio.ensure_future(
        _indexed_hits(client, query_words(q), prefix, names, limit, settings.context_version)
    )
    try:
        overlay = await _overlay_hits(session, q, prefix, names, limit, indexed=client is not None)
    except BaseException:
        search.cancel()
        raise
    try:
        indexed = await search
        hits = merge_hits(indexed, overlay, limit)
        context_query = build_context_query(
            (hit.uri for hit in hits if hit.type == PERSON),
            (hit.uri for hit in hits if hit.type == PLACE),
            (hit.uri for hit in hits if hit.type in CONCEPT_TYPES),
        )
        context_rows = (
            await client.query(context_query) if client is not None and context_query else []
        )
    except QLeverError as error:
        logger.warning("autocomplete_index_unavailable", error=str(error)[:500])
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, INDEX_UNAVAILABLE) from error
    contexts = collect_context(context_rows)
    parents = {parent for context in contexts.values() for parent in context.parents}
    rows = await label_rows(session, [*(hit.uri for hit in hits), *parents])
    hits = display_order(hits, {uri: row.usage_count for uri, row in rows.items()})

    results = []
    for hit in hits:
        row = rows.get(hit.uri)
        found = contexts.get(hit.uri)
        context = None
        if found is not None:
            if hit.type == PERSON:
                context = life_dates(found.births, found.deaths)
            else:
                context = _parent_label(found.parents, rows, chain)
        results.append(
            AutocompleteResult(
                id=hit.uri,
                type=hit.type,
                label=resolve_label(row.labels, row.default_label, chain) if row else hit.label,
                context=context,
                source=row.source if row else LOCAL_SOURCE,
            )
        )
    logger.debug(
        "autocomplete",
        q_length=len(q),
        types=len(names),
        indexed=len(indexed),
        overlay=len(overlay),
        results=len(results),
        ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return results
