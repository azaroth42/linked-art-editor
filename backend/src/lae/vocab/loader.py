"""``lae vocab import`` / ``lae vocab export``: Value Set YAML ⇄ the registry tables (spec §7.4).

``load_from_yaml`` reads every ``*.yaml`` file of a directory (by default the repository's
``profiles/vocab/``), validates each against ``profiles/vocab/_schema.json`` (Step 0.12), and
upserts ``vocabularies``, ``vocabulary_terms`` and ``slot_vocabulary_bindings`` in the caller's
transaction. Nothing is written if any file is invalid (``VocabConfigError`` lists every problem).

Upsert means: a Value Set in the files is created, or replaced by its definition there, terms
included (the file holds the full ordered list); a binding in the files is created or gets the
file's ``enforcement``. Value Sets and bindings that are only in the database are kept, so a file
holding one Value Set can be imported on its own. A binding may name a Value Set that is only in
the database. A Value Set is written, and its ``updated_at`` (the bundle's cache key,
``lae.vocab.service``) moves, only when something about it or its bindings changed; importing the
same files twice changes nothing. ``updated_at`` is ``clock_timestamp()`` so that it orders like
commits even when imports overlap.

The membership of every enumerated Value Set in the files is then refreshed
(``lae.vocab.membership``); rule Value Sets need QLever and are refreshed with
``lae vocab refresh-membership``.

``export_to_yaml`` writes the registry back in the layout of ``profiles/vocab/``: one
``<name>.yaml`` per Value Set ``vocab:<name>``, holding the set and the bindings that name it,
plus a copy of ``_schema.json``. Keys are written in the order of the schema, ``LangMap``s with
``en`` first, absent optional values (``is_default: false``, ``deprecated: false``, no
``scope_note``) omitted, bindings sorted by class and path. ``profiles/vocab/`` is kept in exactly
this form, so ``lae vocab import`` then ``lae vocab export`` reproduces it byte for byte.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lae.db.models import (
    Enforcement,
    SlotVocabularyBinding,
    Vocabulary,
    VocabularyMode,
    VocabularyTerm,
)
from lae.profiles.validation import SCHEMA_FILE, load_schema, load_yaml

VOCAB_DIR: Final = Path(__file__).resolve().parents[4] / "profiles" / "vocab"
"""The repository's ``profiles/vocab/``: the default of ``lae vocab import``."""

SCHEMA_PATH: Final = VOCAB_DIR / SCHEMA_FILE
"""The Value Set file schema every imported file is validated against."""

FILE_SUFFIX: Final = ".yaml"
ID_PREFIX: Final = "vocab:"
RULE_KEYS: Final = ("metatype_uris", "broader_roots", "exclusions", "classes")
FILE_HEADER: Final = (
    "# yaml-language-server: $schema=_schema.json\n"
    "# Value Set {id} and its bindings. Written by `lae vocab export`; edit it and run\n"
    "# `lae vocab import` (profiles/README.md, spec section 7).\n"
)

LangMap = dict[str, str]


class VocabConfigError(ValueError):
    """The Value Set files are invalid; ``errors`` has one line per problem."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__(f"{len(errors)} problem(s) in the Value Set files:\n" + "\n".join(errors))
        self.errors = list(errors)


@dataclass(frozen=True, slots=True)
class TermDef:
    """One term of a Value Set (``vocabulary_terms``)."""

    uri: str
    label: LangMap | None = None
    """``label_override``; ``None`` means the label comes from ``entity_labels``."""
    scope_note: LangMap | None = None
    is_default: bool = False
    deprecated: bool = False
    flags: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ValueSetDef:
    """A Value Set definition (``vocabularies``) with its ordered terms."""

    id: str
    version: int
    label: LangMap
    mode: VocabularyMode
    enforcement: Enforcement
    scope_note: LangMap | None = None
    rule: dict[str, list[str]] | None = None
    terms: tuple[TermDef, ...] = ()

    @property
    def name(self) -> str:
        """The id without ``vocab:``, used as the export file name."""
        return self.id.removeprefix(ID_PREFIX)


@dataclass(frozen=True, slots=True)
class BindingDef:
    """A binding of a Value Set to ``(entity_class, property_path)``."""

    entity_class: str
    property_path: str
    vocabulary_id: str
    enforcement: Enforcement | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        """The unique key of ``slot_vocabulary_bindings``."""
        return (self.entity_class, self.property_path, self.vocabulary_id)


@dataclass(slots=True)
class VocabConfig:
    """Value Sets and bindings, read from files or from the database."""

    value_sets: list[ValueSetDef] = field(default_factory=list)
    bindings: list[BindingDef] = field(default_factory=list)


@dataclass(slots=True)
class ImportSummary:
    """What ``load_from_yaml`` did."""

    files: int
    value_sets: int
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    bindings: int = 0
    bindings_created: int = 0
    bindings_updated: int = 0
    membership: dict[str, int] = field(default_factory=dict)
    """Enumerated Value Set id → members after the refresh."""

    def as_json(self) -> dict[str, Any]:
        """The summary ``lae vocab import`` prints."""
        return {
            "files": self.files,
            "value_sets": self.value_sets,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.value_sets - len(self.created) - len(self.updated),
            "bindings": self.bindings,
            "bindings_created": self.bindings_created,
            "bindings_updated": self.bindings_updated,
            "membership": self.membership,
        }


# --- reading files ------------------------------------------------------------------------------


def _term(data: Mapping[str, Any]) -> TermDef:
    return TermDef(
        uri=data["uri"],
        label=data.get("label"),
        scope_note=data.get("scope_note"),
        is_default=bool(data.get("is_default", False)),
        deprecated=bool(data.get("deprecated", False)),
        flags=data.get("flags"),
    )


def _value_set(data: Mapping[str, Any]) -> ValueSetDef:
    rule = data.get("rule")
    return ValueSetDef(
        id=data["id"],
        version=data["version"],
        label=data["label"],
        scope_note=data.get("scope_note"),
        mode=VocabularyMode(data["mode"]),
        enforcement=Enforcement(data["enforcement"]),
        rule={key: list(rule[key]) for key in RULE_KEYS} if rule is not None else None,
        terms=tuple(_term(term) for term in data.get("terms", [])),
    )


def _binding(data: Mapping[str, Any]) -> BindingDef:
    enforcement = data.get("enforcement")
    return BindingDef(
        entity_class=data["class"],
        property_path=data["path"],
        vocabulary_id=data["value_set"],
        enforcement=Enforcement(enforcement) if enforcement is not None else None,
    )


def _vocab_files(directory: Path, errors: list[str]) -> list[Path]:
    if not directory.is_dir():
        raise VocabConfigError([f"{directory}: not a directory"])
    files = []
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or path.name == SCHEMA_FILE:
            continue
        if path.is_file() and path.suffix == FILE_SUFFIX:
            files.append(path)
        else:
            errors.append(f"{path.name}: unexpected entry; Value Set files are *.yaml")
    return files


def read_vocab_dir(directory: Path, schema_path: Path = SCHEMA_PATH) -> tuple[VocabConfig, int]:
    """Read and validate every Value Set file in ``directory``; return the config and file count.

    Raises ``VocabConfigError`` listing every problem: schema errors, a Value Set id defined twice,
    a term URI repeated within a set, a binding listed twice.
    """
    try:
        validator: Draft202012Validator = load_schema(schema_path)
    except (OSError, json.JSONDecodeError, SchemaError) as error:
        raise VocabConfigError([f"{schema_path}: cannot load the schema: {error}"]) from error
    errors: list[str] = []
    files = _vocab_files(directory, errors)
    config = VocabConfig()
    defined: dict[str, str] = {}
    bound: dict[tuple[str, str, str], str] = {}
    for path in files:
        try:
            data = load_yaml(path)
        except (yaml.YAMLError, UnicodeDecodeError) as error:
            errors.append(f"{path.name}: invalid YAML: {error}")
            continue
        problems = sorted(validator.iter_errors(data), key=lambda e: e.json_path)
        errors.extend(
            f"{path.name}: {problem.json_path}: {problem.message}" for problem in problems
        )
        if problems:
            continue
        for index, raw in enumerate(data.get("value_sets", [])):
            value_set = _value_set(raw)
            where = f"{path.name}: $.value_sets[{index}]"
            if value_set.id in defined:
                errors.append(
                    f"{where}.id: {value_set.id!r} is already defined in {defined[value_set.id]}"
                )
                continue
            defined[value_set.id] = path.name
            uris = [term.uri for term in value_set.terms]
            for uri in sorted({uri for uri in uris if uris.count(uri) > 1}):
                errors.append(f"{where}.terms: duplicate term uri {uri!r}")
            config.value_sets.append(value_set)
        for index, raw in enumerate(data.get("bindings", [])):
            binding = _binding(raw)
            if binding.key in bound:
                errors.append(
                    f"{path.name}: $.bindings[{index}]: duplicate binding {binding.key!r} "
                    f"(also in {bound[binding.key]})"
                )
                continue
            bound[binding.key] = path.name
            config.bindings.append(binding)
    if errors:
        raise VocabConfigError(errors)
    return config, len(files)


# --- the database -------------------------------------------------------------------------------


def _term_from_row(row: VocabularyTerm) -> TermDef:
    return TermDef(
        uri=row.uri,
        label=row.label_override,
        scope_note=row.scope_note,
        is_default=row.is_default,
        deprecated=row.deprecated,
        flags=row.flags,
    )


def _value_set_from_row(row: Vocabulary, terms: Iterable[VocabularyTerm]) -> ValueSetDef:
    return ValueSetDef(
        id=row.id,
        version=row.version,
        label=row.label,
        scope_note=row.scope_note,
        mode=VocabularyMode(row.mode),
        enforcement=Enforcement(row.enforcement),
        rule={key: list(row.rule.get(key, [])) for key in RULE_KEYS} if row.rule else None,
        terms=tuple(_term_from_row(term) for term in terms),
    )


async def read_registry(session: AsyncSession, ids: Iterable[str] | None = None) -> VocabConfig:
    """Read Value Sets (all, or those in ``ids``) and their bindings from the database."""
    vocab_query = select(Vocabulary).order_by(Vocabulary.id)
    term_query = select(VocabularyTerm).order_by(
        VocabularyTerm.vocabulary_id, VocabularyTerm.position
    )
    binding_query = select(SlotVocabularyBinding).order_by(
        SlotVocabularyBinding.entity_class,
        SlotVocabularyBinding.property_path,
        SlotVocabularyBinding.vocabulary_id,
    )
    if ids is not None:
        wanted = sorted(set(ids))
        vocab_query = vocab_query.where(Vocabulary.id.in_(wanted))
        term_query = term_query.where(VocabularyTerm.vocabulary_id.in_(wanted))
        binding_query = binding_query.where(SlotVocabularyBinding.vocabulary_id.in_(wanted))
    terms: dict[str, list[VocabularyTerm]] = {}
    for term in await session.scalars(term_query):
        terms.setdefault(term.vocabulary_id, []).append(term)
    config = VocabConfig()
    for row in await session.scalars(vocab_query):
        config.value_sets.append(_value_set_from_row(row, terms.get(row.id, [])))
    for binding in await session.scalars(binding_query):
        config.bindings.append(
            BindingDef(
                entity_class=binding.entity_class,
                property_path=binding.property_path,
                vocabulary_id=binding.vocabulary_id,
                enforcement=Enforcement(binding.enforcement) if binding.enforcement else None,
            )
        )
    return config


def _vocabulary_values(value_set: ValueSetDef, updated_by: UUID | None) -> dict[str, Any]:
    return {
        "label": value_set.label,
        "scope_note": value_set.scope_note,
        "mode": value_set.mode,
        "rule": value_set.rule,
        "enforcement": value_set.enforcement,
        "version": value_set.version,
        "updated_at": func.clock_timestamp(),
        "updated_by": updated_by,
    }


async def _write_value_set(
    session: AsyncSession, value_set: ValueSetDef, updated_by: UUID | None
) -> None:
    """Insert or replace one Value Set and its whole term list."""
    values = _vocabulary_values(value_set, updated_by)
    statement = insert(Vocabulary).values(id=value_set.id, **values)
    await session.execute(
        statement.on_conflict_do_update(index_elements=[Vocabulary.id], set_=values)
    )
    await session.execute(
        delete(VocabularyTerm).where(VocabularyTerm.vocabulary_id == value_set.id)
    )
    if value_set.terms:
        await session.execute(
            insert(VocabularyTerm),
            [
                {
                    "vocabulary_id": value_set.id,
                    "uri": term.uri,
                    "position": position,
                    "is_default": term.is_default,
                    "deprecated": term.deprecated,
                    "scope_note": term.scope_note,
                    "label_override": term.label,
                    "flags": term.flags,
                }
                for position, term in enumerate(value_set.terms)
            ],
        )


async def load_from_yaml(
    session: AsyncSession, path: Path = VOCAB_DIR, *, updated_by: UUID | None = None
) -> ImportSummary:
    """Upsert the Value Sets and bindings of the files in ``path`` (see the module docstring).

    Runs in the caller's transaction; commit afterwards. Raises ``VocabConfigError``.
    """
    from lae.vocab.membership import refresh_membership

    config, files = read_vocab_dir(path)
    in_files = {value_set.id for value_set in config.value_sets}
    current = await read_registry(session, in_files)
    existing = {value_set.id: value_set for value_set in current.value_sets}
    bound_ids = {binding.vocabulary_id for binding in config.bindings}
    known = in_files | set(
        await session.scalars(select(Vocabulary.id).where(Vocabulary.id.in_(sorted(bound_ids))))
    )
    unknown = sorted(bound_ids - known)
    if unknown:
        raise VocabConfigError(
            [f"a binding names the unknown Value Set {name!r}" for name in unknown]
        )

    summary = ImportSummary(files=files, value_sets=len(config.value_sets))
    for value_set in config.value_sets:
        before = existing.get(value_set.id)
        if before == value_set:
            continue
        await _write_value_set(session, value_set, updated_by)
        (summary.updated if before is not None else summary.created).append(value_set.id)

    touched: set[str] = set()
    rows = await session.scalars(
        select(SlotVocabularyBinding).where(
            SlotVocabularyBinding.vocabulary_id.in_(sorted(bound_ids))
        )
    )
    stored = {(b.entity_class, b.property_path, b.vocabulary_id): b for b in rows}
    for binding in config.bindings:
        row = stored.get(binding.key)
        if row is None:
            session.add(
                SlotVocabularyBinding(
                    entity_class=binding.entity_class,
                    property_path=binding.property_path,
                    vocabulary_id=binding.vocabulary_id,
                    enforcement=binding.enforcement,
                )
            )
            summary.bindings_created += 1
        elif row.enforcement != binding.enforcement:
            row.enforcement = binding.enforcement
            summary.bindings_updated += 1
        else:
            continue
        touched.add(binding.vocabulary_id)
    summary.bindings = len(config.bindings)
    await session.flush()
    rewritten = set(summary.created) | set(summary.updated)
    if touched - rewritten:
        await session.execute(
            update(Vocabulary)
            .where(Vocabulary.id.in_(sorted(touched - rewritten)))
            .values(updated_at=func.clock_timestamp(), updated_by=updated_by)
        )
    for value_set in config.value_sets:
        if value_set.mode is VocabularyMode.ENUMERATED:
            summary.membership[value_set.id] = await refresh_membership(session, value_set.id)
    return summary


# --- writing files ------------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Block-style YAML with list items indented under their key, as the profiles are written."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def _lang_map(value: Mapping[str, str]) -> dict[str, str]:
    """A LangMap with ``en`` first, then the other tags in code-point order."""
    return {tag: value[tag] for tag in sorted(value, key=lambda tag: (tag != "en", tag))}


def _term_yaml(term: TermDef) -> dict[str, Any]:
    data: dict[str, Any] = {"uri": term.uri}
    if term.label is not None:
        data["label"] = _lang_map(term.label)
    if term.scope_note is not None:
        data["scope_note"] = _lang_map(term.scope_note)
    if term.is_default:
        data["is_default"] = True
    if term.deprecated:
        data["deprecated"] = True
    if term.flags is not None:
        data["flags"] = dict(sorted(term.flags.items()))
    return data


def value_set_yaml(value_set: ValueSetDef) -> dict[str, Any]:
    """One ``value_sets`` entry, keys in schema order."""
    data: dict[str, Any] = {
        "id": value_set.id,
        "version": value_set.version,
        "label": _lang_map(value_set.label),
    }
    if value_set.scope_note is not None:
        data["scope_note"] = _lang_map(value_set.scope_note)
    data["mode"] = value_set.mode.value
    data["enforcement"] = value_set.enforcement.value
    if value_set.rule is not None:
        data["rule"] = {key: list(value_set.rule.get(key, [])) for key in RULE_KEYS}
    if value_set.terms:
        data["terms"] = [_term_yaml(term) for term in value_set.terms]
    return data


def binding_yaml(binding: BindingDef) -> dict[str, Any]:
    """One ``bindings`` entry, keys in schema order."""
    data: dict[str, Any] = {
        "class": binding.entity_class,
        "path": binding.property_path,
        "value_set": binding.vocabulary_id,
    }
    if binding.enforcement is not None:
        data["enforcement"] = binding.enforcement.value
    return data


def dump_value_set_file(value_set: ValueSetDef, bindings: Iterable[BindingDef]) -> str:
    """The text of ``<name>.yaml`` for one Value Set and the bindings that name it."""
    document: dict[str, Any] = {"value_sets": [value_set_yaml(value_set)]}
    ordered = sorted(bindings, key=lambda b: (b.entity_class, b.property_path))
    if ordered:
        document["bindings"] = [binding_yaml(binding) for binding in ordered]
    body = yaml.dump(
        document,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    return FILE_HEADER.format(id=value_set.id) + body


def write_vocab_dir(config: VocabConfig, directory: Path) -> list[Path]:
    """Write one file per Value Set and a copy of the schema into ``directory``; return them."""
    directory.mkdir(parents=True, exist_ok=True)
    by_set: dict[str, list[BindingDef]] = {}
    for binding in config.bindings:
        by_set.setdefault(binding.vocabulary_id, []).append(binding)
    written = []
    for value_set in sorted(config.value_sets, key=lambda v: v.id):
        target = directory / f"{value_set.name}{FILE_SUFFIX}"
        target.write_text(
            dump_value_set_file(value_set, by_set.get(value_set.id, [])), encoding="utf-8"
        )
        written.append(target)
    schema_target = directory / SCHEMA_FILE
    if schema_target.resolve() != SCHEMA_PATH.resolve():
        shutil.copyfile(SCHEMA_PATH, schema_target)
    return written


async def export_to_yaml(session: AsyncSession, path: Path) -> list[Path]:
    """Write the whole registry to ``path`` as Value Set files; return the files written."""
    return write_vocab_dir(await read_registry(session), path)
