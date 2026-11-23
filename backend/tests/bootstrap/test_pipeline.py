"""Integration tests: ``load``, ``aat-labels`` and ``export-nt`` against the test database."""

from __future__ import annotations

import gzip
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from lae.api.records import record_etag
from lae.bootstrap.aat import load_aat_labels
from lae.bootstrap.common import (
    CSV_ROW_INVALID,
    DUPLICATE_URI,
    EXPANSION_FAILED,
    NOT_A_RECORD,
    SCHEMA_INVALID,
    asyncpg_dsn,
)
from lae.bootstrap.export import export_nt, meta_path
from lae.bootstrap.load import load
from lae.ids import new_uuid7
from lae.rdf import EXPANDER_VERSION, expand_to_ntriples
from lae.settings import Settings
from tests.bootstrap.conftest import (
    BASE_URI,
    BROWN_TEA_POT,
    NIGHT_WATCH,
    YCBA_PERSON,
    YUAG_OBJECT,
    corpus,
    corpus_path,
    fetch,
)

AAT = "http://vocab.getty.edu/aat/"


def report_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def make_input(tmp_path: Path) -> tuple[Path, str]:
    """Three valid records, a schema failure, a duplicate, a local record and a broken line."""
    source = tmp_path / "input"
    source.mkdir()
    for name in (BROWN_TEA_POT, NIGHT_WATCH, YCBA_PERSON, YUAG_OBJECT):
        shutil.copy(corpus_path(name), source / Path(name).name)
    local_uri = f"{BASE_URI}{new_uuid7()}"
    local = {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": local_uri,
        "type": "Type",
        "_label": "Local concept",
        "identified_by": [{"type": "Name", "content": "Local concept"}],
    }
    lines = [orjson.dumps(corpus(NIGHT_WATCH)), orjson.dumps(local), b"{broken"]
    (source / "more.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    return source, local_uri


async def test_load_writes_records_labels_and_sync_state(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    source, local_uri = make_input(tmp_path)
    report = tmp_path / "load-report.jsonl"
    summary = await load(
        source, settings=bootstrap_settings, report_path=report, batch=2, streams=2, workers=2
    )
    assert summary["items"] == 7
    assert summary["records_written"] == 4
    assert summary["failures"] == {NOT_A_RECORD: 1, SCHEMA_INVALID: 1, DUPLICATE_URI: 1}
    failures = {line["code"]: line for line in report_lines(report)}
    assert failures[NOT_A_RECORD]["source"] == "more.jsonl:3"
    assert failures[SCHEMA_INVALID]["source"] == Path(YUAG_OBJECT).name
    # The two copies are in batches that two workers write concurrently: either may be kept.
    assert failures[DUPLICATE_URI]["uri"] == corpus(NIGHT_WATCH)["id"]
    assert failures[DUPLICATE_URI]["source"] in {"more.jsonl:1", Path(NIGHT_WATCH).name}

    records = await fetch(
        bootstrap_settings,
        "SELECT id, uri, type, version, status::text, etag, data::text, context_version "
        "FROM records ORDER BY uri",
    )
    assert {row["uri"] for row in records} == {
        corpus(name)["id"] for name in (BROWN_TEA_POT, NIGHT_WATCH, YCBA_PERSON)
    } | {local_uri}
    for row in records:
        data = json.loads(row["data"])
        assert (row["version"], row["status"], row["context_version"]) == (1, "active", 1)
        assert row["etag"] == record_etag(data)
        assert data["id"] == row["uri"]
        expand_to_ntriples(data, 1, record_uri=row["uri"])  # node IRIs everywhere
        if row["uri"] == local_uri:
            assert str(row["id"]) == local_uri.removeprefix(BASE_URI)
        else:
            assert row["id"].version == 7

    labels = await fetch(
        bootstrap_settings,
        "SELECT uri, type, labels::text, default_label, source, status::text FROM entity_labels",
    )
    by_uri = {row["uri"]: row for row in labels}
    assert set(by_uri) == {row["uri"] for row in records}
    night_watch = by_uri[corpus(NIGHT_WATCH)["id"]]
    assert json.loads(night_watch["labels"]) == {"en": "The Night Watch", "nl": "De Nachtwacht"}
    assert (night_watch["source"], night_watch["status"]) == ("local", "published")

    state = await fetch(bootstrap_settings, "SELECT key, value::text FROM sync_state")
    assert {row["key"]: json.loads(row["value"]) for row in state} == {
        "bootstrap_context_version": 1,
        "bootstrap_expander_version": EXPANDER_VERSION,
    }


async def test_loading_again_reports_duplicates_and_changes_nothing(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    source, _ = make_input(tmp_path)
    await load(source, settings=bootstrap_settings, report_path=tmp_path / "1.jsonl", workers=1)
    before = await fetch(bootstrap_settings, "SELECT id, etag FROM records ORDER BY id")
    summary = await load(
        source, settings=bootstrap_settings, report_path=tmp_path / "2.jsonl", workers=1
    )
    assert summary["records_written"] == 0
    assert summary["failures"][DUPLICATE_URI] == 5
    assert await fetch(bootstrap_settings, "SELECT id, etag FROM records ORDER BY id") == before


async def test_aat_labels_are_upserted_and_bad_rows_reported(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    csv_path = tmp_path / "aat.csv"
    csv_path.write_text(
        "aat_code,preferred_name,description\n"
        '300033618,paintings (visual works),"Unique works, ""in"" paint"\n'
        "300404670,preferred terms,\n"
        "notanumber,x,\n"
        "300015050,,\n"
        "300033618,repeat,\n",
        encoding="utf-8",
    )
    report = tmp_path / "aat-report.jsonl"
    summary = await load_aat_labels(csv_path, settings=bootstrap_settings, report_path=report)
    assert (summary["terms"], summary["inserted"], summary["rows_skipped"]) == (2, 2, 3)
    assert [line["code"] for line in report_lines(report)] == [CSV_ROW_INVALID] * 3
    rows = await fetch(
        bootstrap_settings,
        "SELECT uri, type, labels::text, default_label, source, status::text, metatype_uris "
        "FROM entity_labels ORDER BY uri",
    )
    assert [dict(row) | {"labels": json.loads(row["labels"])} for row in rows] == [
        {
            "uri": AAT + "300033618",
            "type": "Type",
            "labels": {"en": "paintings (visual works)"},
            "default_label": "paintings (visual works)",
            "source": "aat",
            "status": "published",
            "metatype_uris": [],
        },
        {
            "uri": AAT + "300404670",
            "type": "Type",
            "labels": {"en": "preferred terms"},
            "default_label": "preferred terms",
            "source": "aat",
            "status": "published",
            "metatype_uris": [],
        },
    ]
    await fetch(
        bootstrap_settings,
        "UPDATE entity_labels SET metatype_uris = '{urn:m}', default_label = 'old'",
    )
    again = await load_aat_labels(csv_path, settings=bootstrap_settings, report_path=report)
    assert (again["inserted"], again["updated"]) == (0, 2)
    rows = await fetch(bootstrap_settings, "SELECT default_label, metatype_uris FROM entity_labels")
    assert sorted((row["default_label"], row["metatype_uris"]) for row in rows) == [
        ("paintings (visual works)", ["urn:m"]),
        ("preferred terms", ["urn:m"]),
    ]


@pytest.mark.parametrize("suffix", [".nt", ".nt.gz"])
async def test_export_writes_every_record_sidecar_labels_and_the_watermark(
    bootstrap_settings: Settings, tmp_path: Path, suffix: str
) -> None:
    source, _ = make_input(tmp_path)
    await load(source, settings=bootstrap_settings, report_path=tmp_path / "l.jsonl", workers=1)
    csv_path = tmp_path / "aat.csv"
    csv_path.write_text("aat_code,preferred_name\n300033618,paintings\n", encoding="utf-8")
    await load_aat_labels(csv_path, settings=bootstrap_settings, report_path=tmp_path / "a.jsonl")
    broken_uri = "https://example.org/object/broken"
    broken = {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": broken_uri,
        "type": "HumanMadeObject",
        "identified_by": [{"type": "Name", "content": "no node IRI"}],
    }
    await fetch(
        bootstrap_settings,
        "INSERT INTO records (id, uri, type, version, etag, data, context_version) "
        "VALUES ($1, $2, 'HumanMadeObject', 1, 'x', $3::jsonb, 1)",
        new_uuid7(),
        broken_uri,
        json.dumps(broken),
    )
    for _ in range(3):
        await fetch(
            bootstrap_settings,
            "INSERT INTO outbox_events (kind, record_id) VALUES ('publish', $1)",
            new_uuid7(),
        )

    out = tmp_path / f"export{suffix}"
    summary = await export_nt(out, settings=bootstrap_settings, workers=2, batch=2)
    assert summary["watermark"] == 3
    # Every label row in the export was written before the snapshot (autocomplete's overlay).
    [newest] = await fetch(bootstrap_settings, "SELECT max(updated_at) AS t FROM entity_labels")
    assert datetime.fromisoformat(summary["snapshot_at"]) >= newest["t"]
    assert summary["records"] == 4
    assert summary["failures"] == {EXPANSION_FAILED: 1}
    assert summary["sidecar_rows"] == 1 and summary["sidecar_quads"] == 2
    assert json.loads(meta_path(out).read_text()) == summary
    [failure] = report_lines(Path(summary["report"]))
    assert (failure["code"], failure["uri"]) == (EXPANSION_FAILED, broken_uri)

    raw = out.read_bytes()
    text = (gzip.decompress(raw) if suffix.endswith(".gz") else raw).decode()
    lines = text.splitlines()
    assert len(lines) == summary["triples"] + summary["sidecar_quads"]
    expected: set[str] = set()
    for row in await fetch(
        bootstrap_settings, "SELECT uri, data::text FROM records WHERE uri <> $1", broken_uri
    ):
        expected |= expand_to_ntriples(json.loads(row["data"]), 1, record_uri=row["uri"])
    graph = "<urn:graph:sidecar:aat>"
    sidecar = {line for line in lines if line.endswith(f"{graph} .")}
    assert set(lines) - sidecar == expected
    assert sidecar == {
        f"<{AAT}300033618> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        f"<http://www.cidoc-crm.org/cidoc-crm/E55_Type> {graph} .",
        f'<{AAT}300033618> <http://www.w3.org/2000/01/rdf-schema#label> "paintings"@en {graph} .',
    }


def test_asyncpg_dsn_drops_the_sqlalchemy_driver() -> None:
    assert asyncpg_dsn("postgresql+asyncpg://u:p@h:5433/db") == "postgresql://u:p@h:5433/db"
