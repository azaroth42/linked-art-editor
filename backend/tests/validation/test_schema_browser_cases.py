"""The server's verdict on the documents the browser's schema check is compared with (Step 1.19).

The editor checks drafts against the JSON Schema in the browser (``frontend/src/editor/
validation/schemaCheck.ts``, Ajv) with the same files ``GET /api/profiles/schema/{class}`` serves.
``data/schema_browser_cases.json`` holds, for hand-made documents and for every corpus record of
a class the browser can check, what ``lae.validation.schema.validate_schema`` finds: the
``code`` and ``path`` of each violation, and the property names of ``required`` (``missing``) and
``additionalProperties`` (``extra``). ``schemaCheck.test.ts`` must find exactly the same.

The file is generated from the code below; this test fails when it is out of date. Regenerate
with ``uv run python -m tests.validation.test_schema_browser_cases`` (from ``backend/``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from lae.corpus import CORPUS_DIR, iter_corpus
from lae.projection import assign_node_iris
from lae.validation.schema import validate_schema
from lae.validation.schema_map import SCHEMA_BY_TYPE, is_provenance_activity

FIXTURE: Final = Path(__file__).parent / "data" / "schema_browser_cases.json"
SHAPE_CASES: Final = json.loads((Path(__file__).parent / "data" / "shape_cases.json").read_bytes())
NODE: Final = "/node/01937a4e-0000-7000-8000-0000000000{:02x}"

Document = dict[str, Any]


def _base(name: str = "HumanMadeObject") -> Document:
    """A schema-valid base record with internal node IRIs, as the projection holds it."""
    doc: Document = json.loads(json.dumps(SHAPE_CASES["bases"][name]))
    counter = iter(range(256))
    return assign_node_iris(doc, doc["id"], mint=lambda: doc["id"] + NODE.format(next(counter)))


def _cases() -> dict[str, Document]:
    """Hand-made documents, each exercising one part of the server's preparation or reporting."""
    cases: dict[str, Document] = {"valid object": _base()}

    doc = _base()
    del doc["_label"]
    cases["missing root _label"] = doc

    doc = _base()
    doc["identified_by"][0]["content"] = 42
    cases["content of the wrong JSON type"] = doc

    doc = _base()
    doc["colour"] = "red"
    doc["shape"] = "round"
    cases["two properties the schema does not allow"] = doc

    doc = _base()
    doc["identified_by"].append({"type": "Name"})
    cases["a Name without content"] = doc

    doc = _base()
    timespan = doc["produced_by"]["timespan"]
    timespan["begin_of_the_begin"] = "-0479-01-01T00:00:00Z"
    timespan["end_of_the_end"] = "1642-13-01T00:00:00Z"
    cases["a BCE date and a month 13"] = doc

    doc = _base()
    doc["made_of"].append({"id": "not an iri", "type": "Material"})
    doc["made_of"].append({"id": "http://example.org/with space", "type": "Material"})
    cases["a reference that is not an IRI and one that is repaired"] = doc

    doc = _base()
    doc["identified_by"].append({"type": "Nonsense", "content": "x"})
    cases["an identified_by of no allowed class"] = doc

    doc = _base()
    doc["identified_by"].append(
        {"id": "https://elsewhere.example/name/1", "type": "Name", "content": "Foreign"}
    )
    cases["an embedded Name with a foreign id"] = doc

    doc = _base()
    doc["identified_by"][0]["@context"] = doc["@context"]
    cases["a nested context that repeats the root context"] = doc

    doc = _base()
    doc["equivalent"] = [{"id": "https://elsewhere.example/object/1", "type": "HumanMadeObject"}]
    cases["an equivalent of the record's own class"] = doc

    doc = _base()
    doc["produced_by"] = [doc["produced_by"]]
    cases["produced_by as a list"] = doc

    doc = _base()
    doc["dimension"][0]["value"] = "50.5"
    del doc["dimension"][0]["unit"]["type"]
    cases["a dimension value as text and a unit without type"] = doc

    for name in ("Person", "Place", "Type"):
        doc = _base(name)
        cases[f"valid {name}"] = doc
        doc = _base(name)
        del doc["_label"]
        doc["unknown_property"] = True
        cases[f"{name} without _label and with an unknown property"] = doc
    return cases


def _verdict(doc: Document) -> list[dict[str, Any]]:
    """What the browser must reproduce of the server's schema diagnostics."""
    out = []
    for item in validate_schema(doc):
        found: dict[str, Any] = {"code": item.code, "path": item.path}
        for key in ("missing", "extra"):
            if key in item.params:
                found[key] = sorted(item.params[key])
        out.append(found)
    return sorted(out, key=lambda found: (found["path"], found["code"]))


def build() -> dict[str, Any]:
    """The contents of ``FIXTURE``."""
    corpus: dict[str, list[dict[str, Any]]] = {}
    for path, record in iter_corpus():
        record_type = record.get("type")
        # Provenance activities use provenance.json, which only the server tells apart.
        if record_type not in SCHEMA_BY_TYPE or is_provenance_activity(record):
            continue
        corpus[path.relative_to(CORPUS_DIR).as_posix()] = _verdict(record)
    return {
        "description": __doc__.split("\n\n")[0] if __doc__ else "",
        "cases": [
            {"name": name, "document": doc, "expected": _verdict(doc)}
            for name, doc in _cases().items()
        ],
        "corpus": corpus,
    }


def _text(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def test_the_browser_cases_are_the_servers_current_verdict() -> None:
    assert FIXTURE.read_text(encoding="utf-8") == _text(build()), (
        "regenerate: uv run python -m tests.validation.test_schema_browser_cases"
    )


def test_the_cases_cover_what_the_browser_must_mirror() -> None:
    data = build()
    codes = {found["code"] for case in data["cases"] for found in case["expected"]}
    assert {
        "LAE-SCHEMA-required",
        "LAE-SCHEMA-type",
        "LAE-SCHEMA-additionalProperties",
        "LAE-SCHEMA-format",
        "LAE-SCHEMA-anyOf",
    } <= codes
    valid = [case for case in data["cases"] if case["name"].startswith("valid")]
    assert valid and all(case["expected"] == [] for case in valid)
    # Stripped before the check: foreign embedded ids, the repeated context, own-class equivalent.
    for name in (
        "an embedded Name with a foreign id",
        "a nested context that repeats the root context",
        "an equivalent of the record's own class",
    ):
        assert next(c for c in data["cases"] if c["name"] == name)["expected"] == []
    assert len(data["corpus"]) > 100


if __name__ == "__main__":
    FIXTURE.write_text(_text(build()), encoding="utf-8")
