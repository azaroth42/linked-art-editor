"""Tests for ``lae.bootstrap.source``: directories of JSON files and JSON Lines."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from lae.bootstrap.source import SourceItem, input_files, iter_source


def test_directory_yields_json_files_and_jsonl_lines_in_order(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "a.json").write_bytes(b'{"id": "urn:a"}')
    (tmp_path / "b" / "c.jsonl").write_bytes(b'{"id": "urn:c1"}\n\n  \n{"id": "urn:c2"}\n')
    (tmp_path / "b" / "d.jsonl.gz").write_bytes(gzip.compress(b'{"id": "urn:d"}\n'))
    (tmp_path / "notes.txt").write_text("ignored")
    assert list(iter_source(tmp_path)) == [
        SourceItem("a.json", b'{"id": "urn:a"}'),
        SourceItem("b/c.jsonl:1", b'{"id": "urn:c1"}'),
        SourceItem("b/c.jsonl:4", b'{"id": "urn:c2"}'),
        SourceItem("b/d.jsonl.gz:1", b'{"id": "urn:d"}'),
    ]


def test_a_single_jsonl_file(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{"id": "urn:1"}\n{"id": "urn:2"}')
    assert [item.source for item in iter_source(path)] == ["records.jsonl:1", "records.jsonl:2"]


def test_a_single_json_file_is_one_record(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b'{\n  "id": "urn:1"\n}\n')
    assert list(iter_source(path)) == [SourceItem("record.json", b'{\n  "id": "urn:1"\n}\n')]


def test_missing_input_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        input_files(tmp_path / "missing")


def test_the_golden_corpus_is_276_records() -> None:
    from lae.corpus import CORPUS_DIR

    items = list(iter_source(CORPUS_DIR))
    assert len(items) == 276
    assert {item.source.split("/")[0] for item in items} == {"institutional", "linkedart", "lux"}
