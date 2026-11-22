"""Validate the UI Profile and Value Set YAML under ``profiles/`` against their JSON Schemas.

Used by ``scripts/validate_profiles.py`` (CI ``config`` job) and, from Phase 1, by the API at
startup. Besides JSON Schema validation it runs the cross-file checks a schema cannot express;
``profiles/README.md`` lists them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from lae.profiles.paths import parse_path

SCHEMA_FILE = "_schema.json"
PROFILE_SUFFIX = ".yaml"
#: ``$defs`` that must be identical in the UI Profile and Value Set schemas.
SHARED_DEFS = ("LangMap", "Path", "RecordClass", "ValueSetId")
#: JSON-LD keys a slot or binding path may not name.
RESERVED_KEYS = frozenset({"id", "type"})

#: Readable names for schema regexes, keyed by (``$defs`` entry, sub-schema key or None).
_PATTERN_HINTS: dict[tuple[str, str | None], str] = {
    ("LangMap", "propertyNames"): "a BCP-47 language tag in canonical case (en, fr-CA, zh-Hant)",
    ("LangMap", "additionalProperties"): "a non-blank string",
    ("Path", None): "a profile path (grammar in profiles/README.md)",
    ("ValueSetId", None): "a Value Set id such as vocab:work_title_types",
    ("LocalId", None): "a lower snake_case id",
    ("Uri", None): "an absolute http(s) URI",
}

_Rel = Callable[[Path], str]


@dataclass
class ValidationReport:
    """Outcome of validating a ``profiles/`` directory."""

    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    files_checked: int = 0

    @property
    def ok(self) -> bool:
        """True when no errors were found."""
        return not self.errors


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys and non-string keys (e.g. an unquoted ``no:``)."""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[str, Any]:
    loader.flatten_mapping(node)
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"mapping key {key!r} is not a string; quote it "
                "(YAML reads no/yes/on/off as booleans)",
                key_node.start_mark,
            )
        if key in result:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate mapping key {key!r}", key_node.start_mark
            )
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_yaml(path: Path) -> Any:
    """Parse one profile YAML file, rejecting duplicate and non-string mapping keys."""
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_StrictLoader)  # _StrictLoader subclasses SafeLoader


def load_schema(path: Path) -> Draft202012Validator:
    """Load a JSON Schema file and check that it is itself a valid Draft 2020-12 schema."""
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@dataclass
class _Loaded:
    """A YAML file that passed its JSON Schema."""

    path: Path
    label: str
    data: dict[str, Any]


def validate_profiles_dir(profiles_dir: Path) -> ValidationReport:
    """Validate ``profiles_dir/ui`` and ``profiles_dir/vocab`` and return every problem found."""
    report = ValidationReport()
    root = profiles_dir.parent

    def rel(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()

    validators: dict[str, Draft202012Validator] = {}
    for sub in ("ui", "vocab"):
        schema_path = profiles_dir / sub / SCHEMA_FILE
        try:
            validators[sub] = load_schema(schema_path)
        except FileNotFoundError:
            report.errors.append(f"{rel(schema_path)}: schema file is missing")
        except (json.JSONDecodeError, SchemaError) as exc:
            report.errors.append(f"{rel(schema_path)}: not a valid JSON Schema: {exc}")
    if len(validators) < 2:
        return report
    _check_shared_defs(validators, rel(profiles_dir), report)

    hints = {sub: _pattern_hints(validator) for sub, validator in validators.items()}
    loaded: dict[str, list[_Loaded]] = {"ui": [], "vocab": []}
    found: dict[str, int] = {"ui": 0, "vocab": 0}
    for sub, validator in validators.items():
        for path in _profile_files(profiles_dir / sub, rel, report):
            report.files_checked += 1
            found[sub] += 1
            try:
                data = load_yaml(path)
            except (yaml.YAMLError, UnicodeDecodeError) as exc:
                report.errors.append(f"{rel(path)}: invalid YAML: {exc}")
                continue
            errors = sorted(validator.iter_errors(data), key=lambda e: e.json_path)
            for error in errors:
                message = _error_message(error, hints[sub])
                report.errors.append(f"{rel(path)}: {error.json_path}: {message}")
            if not errors:
                loaded[sub].append(_Loaded(path, rel(path), data))

    for profile in loaded["ui"]:
        _check_profile(profile, report)
    value_set_ids = _check_vocab(loaded["vocab"], report)
    if found["vocab"] == 0:
        report.notes.append(
            "no Value Set files in profiles/vocab/; skipped the slot value_set reference check"
        )
    elif len(loaded["vocab"]) == found["vocab"]:
        _check_value_set_refs(loaded["ui"], value_set_ids, report)
    located = _located_paths(loaded)
    for where, profile_path in located:
        for segment in parse_path(profile_path):
            if segment.key in RESERVED_KEYS:
                report.errors.append(f"{where}: {segment.key!r} cannot appear in a path")
    _check_context_terms(profiles_dir / "context", located, rel, report)
    return report


def _profile_files(directory: Path, rel: _Rel, report: ValidationReport) -> Iterator[Path]:
    """Yield the YAML files of a profiles sub-directory, flagging anything unexpected."""
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or path.name == SCHEMA_FILE:
            continue
        if path.is_file() and path.suffix == PROFILE_SUFFIX:
            yield path
        else:
            report.errors.append(f"{rel(path)}: unexpected entry; profiles must be *.yaml files")


def _check_shared_defs(
    validators: dict[str, Draft202012Validator], where: str, report: ValidationReport
) -> None:
    ui_defs, vocab_defs = (_defs(validators[sub]) for sub in ("ui", "vocab"))
    for name in SHARED_DEFS:
        if ui_defs.get(name) != vocab_defs.get(name):
            report.errors.append(
                f"{where}: $defs.{name} differs between ui/{SCHEMA_FILE} and vocab/{SCHEMA_FILE}"
            )


def _defs(validator: Draft202012Validator) -> Mapping[str, Any]:
    schema = validator.schema
    defs = schema.get("$defs", {}) if isinstance(schema, Mapping) else {}
    return defs if isinstance(defs, Mapping) else {}


def _pattern_hints(validator: Draft202012Validator) -> dict[str, str]:
    defs = _defs(validator)
    hints: dict[str, str] = {}
    for (name, sub), text in _PATTERN_HINTS.items():
        node = defs.get(name)
        if isinstance(node, Mapping) and sub is not None:
            node = node.get(sub)
        if isinstance(node, Mapping) and isinstance(node.get("pattern"), str):
            hints[node["pattern"]] = text
    return hints


def _error_message(error: ValidationError, hints: dict[str, str]) -> str:
    value = error.validator_value
    if error.validator == "pattern" and isinstance(value, str) and value in hints:
        return f"{error.instance!r} is not {hints[value]}"
    if error.validator == "not" and isinstance(value, Mapping) and set(value) == {"required"}:
        names = ", ".join(repr(name) for name in value["required"])
        return f"{names} is not allowed here (see the conditions in the schema)"
    return error.message


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for value in values:
        if value in seen and value not in dupes:
            dupes.append(value)
        seen.add(value)
    return dupes


def _check_profile(profile: _Loaded, report: ValidationReport) -> None:
    data = profile.data
    if profile.path.stem != data["class"]:
        report.errors.append(
            f"{profile.label}: $.class: {data['class']!r} must match the file name "
            f"({profile.path.stem}{PROFILE_SUFFIX})"
        )
    section_ids = [section["id"] for section in data["sections"]]
    for dupe in _duplicates(section_ids):
        report.errors.append(f"{profile.label}: duplicate section id {dupe!r}")
    slot_ids = [slot["id"] for section in data["sections"] for slot in section["slots"]]
    for dupe in _duplicates(slot_ids):
        report.errors.append(f"{profile.label}: duplicate slot id {dupe!r}")
    known_slots = set(slot_ids)
    columns = data.get("grid_columns", [])
    for index, column in enumerate(columns):
        if column["slot"] not in known_slots:
            report.errors.append(
                f"{profile.label}: $.grid_columns[{index}].slot: unknown slot {column['slot']!r}"
            )
    for dupe in _duplicates([column["slot"] for column in columns]):
        report.errors.append(f"{profile.label}: grid column {dupe!r} listed more than once")


def _check_vocab(files: list[_Loaded], report: ValidationReport) -> set[str]:
    """Check Value Set ids, term URIs and bindings across all vocab files; return the set ids."""
    defined: dict[str, str] = {}
    for vocab in files:
        for index, value_set in enumerate(vocab.data.get("value_sets", [])):
            where = f"{vocab.label}: $.value_sets[{index}]"
            set_id = value_set["id"]
            if set_id in defined:
                report.errors.append(
                    f"{where}.id: {set_id!r} is already defined in {defined[set_id]}"
                )
            else:
                defined[set_id] = vocab.label
            for dupe in _duplicates([term["uri"] for term in value_set.get("terms", [])]):
                report.errors.append(f"{where}.terms: duplicate term uri {dupe!r}")
    bound: dict[tuple[str, str, str], str] = {}
    for vocab in files:
        for index, binding in enumerate(vocab.data.get("bindings", [])):
            where = f"{vocab.label}: $.bindings[{index}]"
            if binding["value_set"] not in defined:
                report.errors.append(
                    f"{where}.value_set: unknown Value Set {binding['value_set']!r}"
                )
            key = (binding["class"], binding["path"], binding["value_set"])
            if key in bound:
                report.errors.append(f"{where}: duplicate binding {key!r} (also in {bound[key]})")
            else:
                bound[key] = vocab.label
    return set(defined)


def _check_value_set_refs(
    profiles: list[_Loaded], value_set_ids: set[str], report: ValidationReport
) -> None:
    for profile in profiles:
        for s_index, section in enumerate(profile.data["sections"]):
            for index, slot in enumerate(section["slots"]):
                value_set = slot.get("value_set")
                if value_set is not None and value_set not in value_set_ids:
                    report.errors.append(
                        f"{profile.label}: $.sections[{s_index}].slots[{index}].value_set: "
                        f"{value_set!r} is not defined in profiles/vocab/"
                    )


def _context_terms(context_files: list[Path]) -> set[str]:
    """Collect every term defined in the given JSON-LD contexts, including scoped contexts."""
    terms: set[str] = set()

    def walk(context: Any) -> None:
        if isinstance(context, list):
            for item in context:
                walk(item)
        elif isinstance(context, dict):
            for term, definition in context.items():
                if not term.startswith("@"):
                    terms.add(term)
                if isinstance(definition, dict) and "@context" in definition:
                    walk(definition["@context"])

    for path in context_files:
        walk(json.loads(path.read_text(encoding="utf-8")).get("@context", {}))
    return terms


def _located_paths(loaded: dict[str, list[_Loaded]]) -> list[tuple[str, str]]:
    """Every slot and binding path of the schema-valid files, with its location."""
    located: list[tuple[str, str]] = []
    for profile in loaded["ui"]:
        for s_index, section in enumerate(profile.data["sections"]):
            for index, slot in enumerate(section["slots"]):
                where = f"{profile.label}: $.sections[{s_index}].slots[{index}].path"
                located.append((where, slot["path"]))
    for vocab in loaded["vocab"]:
        for index, binding in enumerate(vocab.data.get("bindings", [])):
            located.append((f"{vocab.label}: $.bindings[{index}].path", binding["path"]))
    return located


def _check_context_terms(
    context_dir: Path, located: list[tuple[str, str]], rel: _Rel, report: ValidationReport
) -> None:
    context_files = sorted(context_dir.glob("*.json")) if context_dir.is_dir() else []
    if not context_files:
        report.notes.append("no pinned @context in profiles/context/; skipped the path term check")
        return
    try:
        terms = _context_terms(context_files)
    except (json.JSONDecodeError, AttributeError) as exc:
        report.errors.append(f"{rel(context_dir)}: cannot read @context: {exc}")
        return
    for where, path in located:
        for segment in parse_path(path):
            if segment.key not in terms:
                report.errors.append(f"{where}: {segment.key!r} is not a Linked Art term")
            if segment.type_filter is not None and segment.type_filter not in terms:
                report.errors.append(f"{where}: {segment.type_filter!r} is not a Linked Art class")
