"""The vocabulary bundle and term search behind ``/api/vocab`` (Phase 2 Step 2.1; spec §7.1).

**Bundle.** ``get_bundle`` returns every Value Set with its terms, labels resolved, and every
binding: what the editor loads once at boot so that enumerated pickers need no request
(``GET /api/vocab/bundle``). A term's ``label`` is its ``entity_labels.labels`` with the curated
``label_override`` laid over it language by language; ``default_label`` is the override's ``en``,
else ``entity_labels.default_label``, else the URI. Rule Value Sets are included with their rule
and pinned shortlist (their members come from ``GET /api/vocab/{id}/terms``), so the client knows
how to render every binding.

**Cache key and ETag.** ``registry_version`` is ``max(vocabularies.updated_at)`` with the number
of Value Sets (so that deleting one that was not the newest changes it too). The bundle is built
once per process per version and served from memory; the ``ETag`` is derived from the version, so
a client revalidates with one cheap query. Every write to the registry moves ``updated_at``
(``lae.vocab.loader``; the admin endpoints of Step 2.4); Step 2.2 recompiles its validation rules
on the same key. Labels changed in ``entity_labels`` reach the bundle when the registry next
changes or the process restarts, which is why the ``ETag`` is weak.

**Terms.** ``search_terms`` answers ``GET /api/vocab/{id}/terms?q=&limit=``. For a rule Value
Set it is a prefix search (the default label, or any label, starts with ``q`` or has a word that
does, ignoring case) over ``vocabulary_membership`` joined to published ``entity_labels`` rows,
ordered by ``usage_count`` (most used first), then default label, then URI. With an empty ``q`` it
returns the pinned shortlist, then the most used members. For an enumerated Value Set it filters
the bundle's terms, in their order.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import orjson
from pydantic import BaseModel
from sqlalchemy import Text, any_, bindparam, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from lae.db.models import (
    EntityLabel,
    EntityLabelStatus,
    SlotVocabularyBinding,
    Vocabulary,
    VocabularyMembership,
    VocabularyMode,
    VocabularyTerm,
)
from lae.labels.transclude import match_label, resolve_label
from lae.vocab.loader import RULE_KEYS
from lae.vocab.membership import UnknownVocabularyError

ModeName = Literal["enumerated", "rule"]
EnforcementName = Literal["strict", "extensible", "advisory"]

_LIKE_SPECIAL: Final = re.compile(r"([\\%_])")


class BundleTerm(BaseModel):
    """One term of a Value Set in the bundle."""

    uri: str
    label: dict[str, str]
    """BCP-47 tag → label: ``entity_labels.labels`` overridden by the curated label."""
    default_label: str
    """The label to show when no language of ``label`` matches."""
    scope_note: dict[str, str] | None = None
    is_default: bool = False
    deprecated: bool = False
    flags: dict[str, Any] | None = None


class BundleRule(BaseModel):
    """The membership rule of a rule Value Set."""

    metatype_uris: list[str]
    broader_roots: list[str]
    exclusions: list[str]
    classes: list[str]


class BundleVocabulary(BaseModel):
    """A Value Set in the bundle: all terms of an enumerated set, the pinned ones of a rule set."""

    id: str
    version: int
    label: dict[str, str]
    scope_note: dict[str, str] | None = None
    mode: ModeName
    enforcement: EnforcementName
    rule: BundleRule | None = None
    terms: list[BundleTerm]


class BundleBinding(BaseModel):
    """A binding of a Value Set to ``(entity_class, property_path)``."""

    entity_class: str
    property_path: str
    vocabulary_id: str
    enforcement: EnforcementName | None
    """The binding's own level; ``None`` inherits the Value Set's."""
    effective_enforcement: EnforcementName
    """The level that applies: ``enforcement``, else the Value Set's."""


class VocabBundle(BaseModel):
    """Body of ``GET /api/vocab/bundle``."""

    version: str
    """The registry version the bundle was built from (its ``ETag`` is derived from it)."""
    vocabularies: list[BundleVocabulary]
    bindings: list[BundleBinding]


class TermOption(BaseModel):
    """One answer of ``GET /api/vocab/{id}/terms``."""

    uri: str
    label: str
    """In the caller's locale chain."""
    scope_note: str | None
    """The term's scope note in the caller's locale chain, if it has one."""
    deprecated: bool
    pinned: bool
    """A term listed in the Value Set itself (the shortlist of a rule set)."""
    usage_count: int
    source: str | None
    """``entity_labels.source`` (``local``, ``aat``, …), ``None`` without a row."""


# --- registry version and bundle ----------------------------------------------------------------


async def registry_version(session: AsyncSession) -> str:
    """The cache key of everything derived from the registry (see the module docstring)."""
    newest, count = (
        await session.execute(select(func.max(Vocabulary.updated_at), func.count(Vocabulary.id)))
    ).one()
    return f"{newest.isoformat() if newest is not None else '-'}|{count}"


def bundle_etag(version: str) -> str:
    """The weak ``ETag`` of the bundle built from registry ``version``."""
    return 'W/"vocab-' + hashlib.blake2b(version.encode(), digest_size=12).hexdigest() + '"'


def _resolved(
    term: VocabularyTerm, row: tuple[dict[str, str], str] | None
) -> tuple[dict[str, str], str]:
    """``(label, default_label)`` of a term from its ``entity_labels`` row and its override."""
    labels = dict(row[0]) if row is not None else {}
    override = term.label_override or {}
    labels.update(override)
    if "en" in override:
        return labels, override["en"]
    return labels, row[1] if row is not None else term.uri


async def _label_rows(session: AsyncSession, uris: Sequence[str]) -> dict[str, Any]:
    if not uris:
        return {}
    rows = await session.execute(
        select(
            EntityLabel.uri,
            EntityLabel.labels,
            EntityLabel.default_label,
            EntityLabel.usage_count,
            EntityLabel.source,
        ).where(EntityLabel.uri == any_(bindparam("uris", sorted(set(uris)), type_=ARRAY(Text))))
    )
    return {row.uri: row for row in rows}


async def build_bundle(session: AsyncSession, version: str) -> VocabBundle:
    """Read the registry and build the bundle (no cache)."""
    vocabularies = list(await session.scalars(select(Vocabulary).order_by(Vocabulary.id)))
    terms: dict[str, list[VocabularyTerm]] = {}
    for term in await session.scalars(
        select(VocabularyTerm).order_by(VocabularyTerm.vocabulary_id, VocabularyTerm.position)
    ):
        terms.setdefault(term.vocabulary_id, []).append(term)
    rows = await _label_rows(session, [t.uri for listed in terms.values() for t in listed])
    out = []
    for vocabulary in vocabularies:
        bundle_terms = []
        for term in terms.get(vocabulary.id, []):
            row = rows.get(term.uri)
            label, default_label = _resolved(
                term, (row.labels, row.default_label) if row is not None else None
            )
            bundle_terms.append(
                BundleTerm(
                    uri=term.uri,
                    label=label,
                    default_label=default_label,
                    scope_note=term.scope_note,
                    is_default=term.is_default,
                    deprecated=term.deprecated,
                    flags=term.flags,
                )
            )
        rule = vocabulary.rule
        out.append(
            BundleVocabulary(
                id=vocabulary.id,
                version=vocabulary.version,
                label=vocabulary.label,
                scope_note=vocabulary.scope_note,
                mode=vocabulary.mode.value,
                enforcement=vocabulary.enforcement.value,
                rule=BundleRule(**{key: list(rule.get(key, [])) for key in RULE_KEYS})
                if rule
                else None,
                terms=bundle_terms,
            )
        )
    levels = {vocabulary.id: vocabulary.enforcement.value for vocabulary in vocabularies}
    bindings = [
        BundleBinding(
            entity_class=binding.entity_class,
            property_path=binding.property_path,
            vocabulary_id=binding.vocabulary_id,
            enforcement=binding.enforcement.value if binding.enforcement else None,
            effective_enforcement=(
                binding.enforcement.value if binding.enforcement else levels[binding.vocabulary_id]
            ),
        )
        for binding in await session.scalars(
            select(SlotVocabularyBinding).order_by(
                SlotVocabularyBinding.entity_class,
                SlotVocabularyBinding.property_path,
                SlotVocabularyBinding.vocabulary_id,
            )
        )
    ]
    return VocabBundle(version=version, vocabularies=out, bindings=bindings)


@dataclass(frozen=True, slots=True)
class CachedBundle:
    """A built bundle with its ``ETag`` and JSON body."""

    version: str
    etag: str
    bundle: VocabBundle
    body: bytes


_cached: CachedBundle | None = None


def clear_bundle_cache() -> None:
    """Forget the in-process bundle (tests; the version check makes this unnecessary otherwise)."""
    global _cached
    _cached = None


async def cached_bundle(session: AsyncSession) -> CachedBundle:
    """The bundle of the current registry version, built at most once per version per process."""
    global _cached
    version = await registry_version(session)
    found = _cached
    if found is not None and found.version == version:
        return found
    bundle = await build_bundle(session, version)
    found = CachedBundle(version, bundle_etag(version), bundle, orjson.dumps(bundle.model_dump()))
    _cached = found
    return found


async def get_bundle(session: AsyncSession) -> VocabBundle:
    """Every Value Set with resolved labels and every binding (cached per registry version)."""
    return (await cached_bundle(session)).bundle


# --- term search --------------------------------------------------------------------------------


def like_escape(text: str) -> str:
    """Escape ``%``, ``_`` and the escape character for ``LIKE ... ESCAPE '\\'``."""
    return _LIKE_SPECIAL.sub(r"\\\1", text)


def label_matches(labels: Mapping[str, str], default_label: str, q: str) -> bool:
    """Whether a label starts with ``q`` or has a word that does, ignoring case."""
    wanted = q.lower()
    for label in (default_label, *labels.values()):
        lowered = label.lower()
        if lowered.startswith(wanted) or f" {wanted}" in lowered:
            return True
    return False


def _localized(value: Mapping[str, str] | None, chain: Sequence[str]) -> str | None:
    if not value:
        return None
    return match_label(value, chain) or value.get("en")


async def _enumerated_terms(
    session: AsyncSession, vocabulary_id: str, q: str, limit: int, chain: Sequence[str]
) -> list[TermOption]:
    bundle = await get_bundle(session)
    vocabulary = next((v for v in bundle.vocabularies if v.id == vocabulary_id), None)
    if vocabulary is None:  # deleted since the caller looked it up
        raise UnknownVocabularyError(vocabulary_id)
    rows = await _label_rows(session, [term.uri for term in vocabulary.terms])
    options = []
    for term in vocabulary.terms:
        if q and not label_matches(term.label, term.default_label, q):
            continue
        row = rows.get(term.uri)
        options.append(
            TermOption(
                uri=term.uri,
                label=resolve_label(term.label, term.default_label, chain),
                scope_note=_localized(term.scope_note, chain),
                deprecated=term.deprecated,
                pinned=True,
                usage_count=row.usage_count if row is not None else 0,
                source=row.source if row is not None else None,
            )
        )
    return options[:limit]


async def _rule_terms(
    session: AsyncSession, vocabulary_id: str, q: str, limit: int, chain: Sequence[str]
) -> list[TermOption]:
    pinned = {
        term.uri: term
        for term in await session.scalars(
            select(VocabularyTerm)
            .where(VocabularyTerm.vocabulary_id == vocabulary_id)
            .order_by(VocabularyTerm.position)
        )
    }
    options: list[TermOption] = []
    if not q:
        rows = await _label_rows(session, list(pinned))
        for term in pinned.values():
            row = rows.get(term.uri)
            label, default_label = _resolved(
                term, (row.labels, row.default_label) if row is not None else None
            )
            options.append(
                TermOption(
                    uri=term.uri,
                    label=resolve_label(label, default_label, chain),
                    scope_note=_localized(term.scope_note, chain),
                    deprecated=term.deprecated,
                    pinned=True,
                    usage_count=row.usage_count if row is not None else 0,
                    source=row.source if row is not None else None,
                )
            )
        if len(options) >= limit:
            return options[:limit]
    statement = (
        select(
            EntityLabel.uri,
            EntityLabel.labels,
            EntityLabel.default_label,
            EntityLabel.usage_count,
            EntityLabel.source,
        )
        .join(VocabularyMembership, VocabularyMembership.uri == EntityLabel.uri)
        .where(
            VocabularyMembership.vocabulary_id == vocabulary_id,
            EntityLabel.status == EntityLabelStatus.PUBLISHED,
        )
        .order_by(
            EntityLabel.usage_count.desc(),
            EntityLabel.default_label.collate("C"),
            EntityLabel.uri.collate("C"),
        )
    )
    if q:
        prefix, word = f"{like_escape(q)}%", f"% {like_escape(q)}%"
        values = func.jsonb_each_text(EntityLabel.labels).table_valued("key", "value").alias("l")
        statement = statement.where(
            or_(
                EntityLabel.default_label.ilike(prefix, escape="\\"),
                EntityLabel.default_label.ilike(word, escape="\\"),
                exists(
                    select(literal(1))
                    .select_from(values)
                    .where(
                        or_(
                            values.c.value.ilike(prefix, escape="\\"),
                            values.c.value.ilike(word, escape="\\"),
                        )
                    )
                ),
            )
        )
    else:
        statement = statement.where(EntityLabel.uri.not_in(sorted(pinned)))
    for row in await session.execute(statement.limit(limit - len(options))):
        listed = pinned.get(row.uri)
        labels = dict(row.labels)
        default_label = row.default_label
        if listed is not None:
            labels, default_label = _resolved(listed, (row.labels, row.default_label))
        options.append(
            TermOption(
                uri=row.uri,
                label=resolve_label(labels, default_label, chain),
                scope_note=_localized(listed.scope_note, chain) if listed is not None else None,
                deprecated=listed.deprecated if listed is not None else False,
                pinned=listed is not None,
                usage_count=row.usage_count,
                source=row.source,
            )
        )
    return options


async def search_terms(
    session: AsyncSession, vocabulary_id: str, q: str, limit: int, chain: Sequence[str]
) -> list[TermOption]:
    """Terms of a Value Set matching ``q`` (see the module docstring); raises
    ``UnknownVocabularyError``."""
    mode = await session.scalar(select(Vocabulary.mode).where(Vocabulary.id == vocabulary_id))
    if mode is None:
        raise UnknownVocabularyError(vocabulary_id)
    q = " ".join(q.split())
    if mode == VocabularyMode.ENUMERATED:
        return await _enumerated_terms(session, vocabulary_id, q, limit, chain)
    return await _rule_terms(session, vocabulary_id, q, limit, chain)
