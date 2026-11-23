"""Materialized Value Set membership: ``vocabulary_membership`` and ``entity_labels.vocab_ids``.

``refresh_membership(session, vocabulary_id, qlever=...)`` recomputes one Value Set:

- **enumerated**: its members are its terms (deprecated ones included: deprecation hides a term
  from pickers, it does not make existing values invalid).
- **rule** (ADR-0032): the union of
  1. QLever, over the default graph and every ``urn:graph:sidecar:*`` graph (QLever's default
     graph is their union): concepts with ``crm:P2_has_type`` any of ``metatype_uris``, or that
     reach any of ``broader_roots`` through ``(skos:broader|crm:P127_has_broader_term)+``,
     restricted to ``classes`` (to every concept class when ``classes`` is empty) by ``rdf:type``;
     with neither metatypes nor roots, every concept of ``classes``;
  2. ``entity_labels`` rows (published) whose ``metatype_uris`` meet ``metatype_uris`` and whose
     ``type`` is in those classes, or, with neither metatypes nor roots, every row of those
     classes. This is where the metatypes records state inline on concept references are
     (``lae.vocab.metatypes``): QLever never has them (ADR-0021);
  3. the Value Set's pinned terms;
  minus ``exclusions`` (the listed concepts only, not what is narrower than them). Without a
  QLever client only parts 2 and 3 are used.

The result replaces the set's ``vocabulary_membership`` rows; then one ``UPDATE … FROM`` adds the
set's id to ``entity_labels.vocab_ids`` of every new member and removes it from every former one.
All writes happen in the caller's transaction, after the QLever query, with the Value Set's row
locked so that two refreshes of one set do not interleave; commit afterwards.

The SPARQL is built from ``lae.sparql.terms.iri`` (every IRI is checked); Step 2.2 moves it onto
the vendored builder.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from functools import cache
from typing import Final

from sqlalchemy import Text, any_, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from lae.db.models import (
    EntityLabel,
    EntityLabelStatus,
    Vocabulary,
    VocabularyMode,
    VocabularyTerm,
)
from lae.sparql.terms import iri
from lae.sync.qlever_client import QLeverClient
from lae.vocab.metatypes import CONCEPT_CLASSES

PREFIXES: Final = (
    "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
    "PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>\n"
    "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
)
BROADER_PATH: Final = "(skos:broader|crm:P127_has_broader_term)+"
"""Linked Art's ``broader`` is ``skos:broader``; sidecars may use the CRM property."""

_ARRAY = ARRAY(Text)

_REPLACE_MEMBERS: Final = text(
    "INSERT INTO vocabulary_membership (vocabulary_id, uri) "
    "SELECT :vid, uri FROM unnest(CAST(:uris AS text[])) AS uri"
)
_UPDATE_VOCAB_IDS: Final = text(
    """
UPDATE entity_labels AS e
SET vocab_ids = CASE
    WHEN a.member
    THEN ARRAY(SELECT DISTINCT v FROM unnest(array_append(e.vocab_ids, :vid)) AS v ORDER BY v)
    ELSE array_remove(e.vocab_ids, :vid)
END
FROM (
    SELECT m.uri, true AS member FROM vocabulary_membership AS m WHERE m.vocabulary_id = :vid
    UNION ALL
    SELECT f.uri, false AS member FROM entity_labels AS f
    WHERE f.vocab_ids @> ARRAY[CAST(:vid AS text)]
      AND NOT EXISTS (
          SELECT 1 FROM vocabulary_membership AS m
          WHERE m.vocabulary_id = :vid AND m.uri = f.uri
      )
) AS a
WHERE e.uri = a.uri AND (CAST(:vid AS text) = ANY(e.vocab_ids)) <> a.member
"""
)


class UnknownVocabularyError(LookupError):
    """No Value Set has this id."""

    def __init__(self, vocabulary_id: str) -> None:
        super().__init__(vocabulary_id)
        self.vocabulary_id = vocabulary_id


@cache
def concept_class_iris(context_version: int) -> dict[str, str]:
    """Class IRI of every concept class in the pinned context (``Type`` → ``crm:E55_Type``)."""
    from lae.bootstrap.export import class_iri

    found: dict[str, str] = {}
    for name in sorted(CONCEPT_CLASSES):
        klass = class_iri(name, context_version)
        if klass is not None:
            found[name] = klass
    return found


def rule_classes(rule: Mapping[str, Sequence[str]]) -> list[str]:
    """The concept classes a rule admits: its ``classes``, else every concept class."""
    return sorted(rule.get("classes") or CONCEPT_CLASSES)


def _values(variable: str, uris: Iterable[str]) -> str:
    return f"VALUES ?{variable} {{ " + " ".join(iri(uri) for uri in sorted(set(uris))) + " }"


def build_membership_query(rule: Mapping[str, Sequence[str]], class_iris: Mapping[str, str]) -> str:
    """The QLever query for the members of a rule (part 1 of the module docstring).

    Raises ``ValueError`` if a URI cannot be an IRI or no class of the rule has a class IRI.
    """
    classes = [class_iris[name] for name in rule_classes(rule) if name in class_iris]
    if not classes:
        raise ValueError(f"no class IRI for the rule's classes {rule_classes(rule)}")
    branches = []
    if metatypes := list(rule.get("metatype_uris") or []):
        branches.append(
            f"{{ {_values('metatype', metatypes)} ?concept crm:P2_has_type ?metatype }}"
        )
    if roots := list(rule.get("broader_roots") or []):
        branches.append(f"{{ {_values('root', roots)} ?concept {BROADER_PATH} ?root }}")
    where = "  " + "\n  UNION\n  ".join(branches) + "\n" if branches else ""
    return (
        PREFIXES
        + "SELECT DISTINCT ?concept WHERE {\n"
        + where
        + f"  {_values('class', classes)}\n"
        + "  ?concept rdf:type ?class .\n"
        + "}"
    )


async def label_members(session: AsyncSession, rule: Mapping[str, Sequence[str]]) -> set[str]:
    """Part 2: the ``entity_labels`` rows a rule selects by metatype (or by class alone)."""
    metatypes = list(rule.get("metatype_uris") or [])
    roots = list(rule.get("broader_roots") or [])
    if not metatypes and roots:
        return set()
    statement = select(EntityLabel.uri).where(
        EntityLabel.status == EntityLabelStatus.PUBLISHED,
        EntityLabel.type == any_(bindparam("classes", rule_classes(rule), type_=_ARRAY)),
    )
    if metatypes:
        statement = statement.where(
            EntityLabel.metatype_uris.overlap(bindparam("metatypes", sorted(metatypes), _ARRAY))
        )
    return set(await session.scalars(statement))


async def qlever_members(
    qlever: QLeverClient, rule: Mapping[str, Sequence[str]], context_version: int
) -> set[str]:
    """Part 1: the concepts QLever has for a rule."""
    rows = await qlever.query(build_membership_query(rule, concept_class_iris(context_version)))
    return {row["concept"] for row in rows if isinstance(row.get("concept"), str)}


async def compute_members(
    session: AsyncSession,
    vocabulary: Vocabulary,
    *,
    qlever: QLeverClient | None = None,
    context_version: int = 1,
) -> set[str]:
    """The members of ``vocabulary`` as the module docstring defines them (nothing is written)."""
    terms = set(
        await session.scalars(
            select(VocabularyTerm.uri).where(VocabularyTerm.vocabulary_id == vocabulary.id)
        )
    )
    if vocabulary.mode == VocabularyMode.ENUMERATED or not vocabulary.rule:
        return terms
    rule = vocabulary.rule
    members = terms | await label_members(session, rule)
    if qlever is not None:
        members |= await qlever_members(qlever, rule, context_version)
    return members - set(rule.get("exclusions") or [])


async def write_members(
    session: AsyncSession, vocabulary_id: str, members: Collection[str]
) -> None:
    """Replace the set's ``vocabulary_membership`` rows and update ``entity_labels.vocab_ids``."""
    await session.execute(
        select(Vocabulary.id).where(Vocabulary.id == vocabulary_id).with_for_update()
    )
    await session.execute(
        text("DELETE FROM vocabulary_membership WHERE vocabulary_id = :vid"), {"vid": vocabulary_id}
    )
    if members:
        await session.execute(_REPLACE_MEMBERS, {"vid": vocabulary_id, "uris": sorted(members)})
    await session.execute(_UPDATE_VOCAB_IDS, {"vid": vocabulary_id})


async def refresh_membership(
    session: AsyncSession,
    vocabulary_id: str,
    *,
    qlever: QLeverClient | None = None,
    context_version: int = 1,
) -> int:
    """Recompute and store the members of one Value Set; return how many there are.

    Raises ``UnknownVocabularyError``, and ``QLeverError`` if QLever fails (nothing is written).
    """
    # populate_existing: an import in the same session may have rewritten the row.
    vocabulary = await session.get(Vocabulary, vocabulary_id, populate_existing=True)
    if vocabulary is None:
        raise UnknownVocabularyError(vocabulary_id)
    members = await compute_members(
        session, vocabulary, qlever=qlever, context_version=context_version
    )
    await write_members(session, vocabulary_id, members)
    return len(members)
