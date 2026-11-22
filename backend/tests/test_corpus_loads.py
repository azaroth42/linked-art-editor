"""Every golden-corpus file is a parseable Linked Art record (Phase 0, Step 0.4)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from lae.corpus import CORPUS_DIR, corpus_files, iter_corpus
from lae.validation.schema_map import RECORD_TYPES

PINNED_CONTEXT_URL = "https://linked.art/ns/v1/linked-art.json"
PINNED_CONTEXT_FILE = CORPUS_DIR.parents[1] / "profiles" / "context" / "v1.json"
LINKEDART_EXAMPLES = 211
MIN_INSTITUTIONAL = 20
INSTITUTIONAL_CLASSES = {
    "HumanMadeObject",
    "Person",
    "Group",
    "Place",
    "Type",
    "VisualItem",
    "LinguisticObject",
    "Set",
    "Activity",
}

FILES = corpus_files()


def corpus_id(path: Path) -> str:
    """Test id: the path relative to tests/corpus."""
    return path.relative_to(CORPUS_DIR).as_posix()


@pytest.mark.parametrize("path", FILES, ids=corpus_id)
def test_file_is_a_linked_art_record(path: Path) -> None:
    data = json.loads(path.read_bytes())
    assert isinstance(data, dict)
    for key in ("@context", "id", "type"):
        assert key in data, f"missing {key}"
    assert isinstance(data["id"], str) and data["id"]
    assert data["type"] in RECORD_TYPES


@pytest.mark.parametrize("path", FILES, ids=corpus_id)
def test_file_uses_the_pinned_context(path: Path) -> None:
    assert json.loads(path.read_bytes())["@context"] == PINNED_CONTEXT_URL


def test_corpus_size_and_institutional_coverage() -> None:
    records = list(iter_corpus())
    per_subset = Counter(path.parent.name for path, _ in records)
    assert per_subset["linkedart"] >= LINKEDART_EXAMPLES
    assert per_subset["institutional"] >= MIN_INSTITUTIONAL
    institutional_types = {r["type"] for p, r in records if p.parent.name == "institutional"}
    assert INSTITUTIONAL_CLASSES <= institutional_types


def test_iter_corpus_yields_every_file_once() -> None:
    assert [path for path, _ in iter_corpus()] == FILES
    assert len(set(FILES)) == len(FILES)


def test_pinned_context_is_vendored() -> None:
    context = json.loads(PINNED_CONTEXT_FILE.read_bytes())
    terms = context["@context"]
    assert terms["crm"] == "http://www.cidoc-crm.org/cidoc-crm/"
    assert terms["HumanMadeObject"]["@id"] == "crm:E22_Human-Made_Object"
