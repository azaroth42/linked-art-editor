"""Every golden-corpus record validates against the JSON Schema for its type (Phase 0, Step 0.4).

Files listed in ``tests/corpus/KNOWN_FAILURES.md`` are strict expected failures: a listed file
that starts to validate fails the run until its row is removed, and a new failure is never hidden.

A record is checked as ``lae.validation.validate_schema`` checks it: without the ``id`` of an
embedded node that has one of its own, and a nested repeat of the pinned ``@context``
(``lae.rdf.strip_embedded_ids``, ``lae.rdf.strip_nested_contexts``, ADR-0028).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from lae.corpus import CORPUS_DIR, corpus_files, load_record
from lae.rdf import strip_embedded_ids, strip_nested_contexts
from lae.validation.schema_map import (
    PROVENANCE_SCHEMA,
    SCHEMA_BY_TYPE,
    load_registry,
    schema_name_for,
    validator_for,
)

KNOWN_FAILURES_MD = CORPUS_DIR / "KNOWN_FAILURES.md"


def known_failures() -> set[str]:
    """Corpus paths (relative to tests/corpus) in the first column of KNOWN_FAILURES.md."""
    text = KNOWN_FAILURES_MD.read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([^`]+\.json)` \|", text, flags=re.MULTILINE))


KNOWN_FAILURES = known_failures()


def summarize(error: ValidationError) -> str:
    """One line per error: JSON Pointer, failing keyword, and a short message."""
    pointer = "/" + "/".join(str(part) for part in error.absolute_path)
    if error.validator == "anyOf":
        detail = "matches none of the allowed shapes"
    elif error.validator == "type":
        detail = f"is not of type {error.validator_value!r}"
    else:
        detail = error.message
    return f"{pointer} {error.validator}: {detail}"


def schema_error_summaries(validator: Draft7Validator, record: dict[str, Any]) -> list[str]:
    """Sorted summaries of every schema error; an unresolvable ``$ref`` counts as an error."""
    summaries: list[str] = []
    try:
        summaries.extend(summarize(error) for error in validator.iter_errors(record))
    except Unresolvable as exc:
        summaries.append(f"(schema) unresolvable $ref: {exc.ref}")
    return sorted(summaries)


def corpus_params() -> Iterator[Any]:
    """One pytest param per corpus file, strict-xfail if it is a known failure."""
    for path in corpus_files():
        relative = path.relative_to(CORPUS_DIR).as_posix()
        marks = (
            [pytest.mark.xfail(strict=True, reason="listed in tests/corpus/KNOWN_FAILURES.md")]
            if relative in KNOWN_FAILURES
            else []
        )
        yield pytest.param(path, id=relative, marks=marks)


@pytest.fixture(scope="module")
def registry() -> Registry[Any]:
    return load_registry()


@pytest.fixture(scope="module")
def validators(registry: Registry[Any]) -> dict[str, Draft7Validator]:
    names = set(SCHEMA_BY_TYPE.values()) | {PROVENANCE_SCHEMA}
    return {name: validator_for(name, registry) for name in names}


@pytest.mark.parametrize("path", list(corpus_params()))
def test_record_validates_against_its_schema(
    path: Path, validators: dict[str, Draft7Validator]
) -> None:
    record = load_record(path)
    record = strip_embedded_ids(strip_nested_contexts(record), record.get("id", ""))
    errors = schema_error_summaries(validators[schema_name_for(record)], record)
    assert not errors, "\n".join(errors)


def test_known_failures_list_only_corpus_files() -> None:
    files = {path.relative_to(CORPUS_DIR).as_posix() for path in corpus_files()}
    assert KNOWN_FAILURES, "KNOWN_FAILURES.md has no rows; the parser or the file changed"
    assert KNOWN_FAILURES <= files
