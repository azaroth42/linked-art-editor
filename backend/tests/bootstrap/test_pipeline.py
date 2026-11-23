"""Integration tests: ``load``, ``aat-labels`` and ``export-nt`` against the test database."""

from __future__ import annotations

import gzip
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
import orjson
import pytest

import lae.bootstrap.load
import lae.bootstrap.rewrite
from lae.api.records import record_etag
from lae.bootstrap.aat import load_aat_labels
from lae.bootstrap.common import (
    CSV_ROW_INVALID,
    DUPLICATE_URI,
    EXPANSION_FAILED,
    NOT_A_RECORD,
    BootstrapError,
    asyncpg_dsn,
)
from lae.bootstrap.export import export_nt, meta_path
from lae.bootstrap.load import load
from lae.bootstrap.rewrite import acquire_load_lock, rewrite_refs
from lae.ids import new_uuid7
from lae.labels.refs import iter_references
from lae.rdf import EXPANDER_VERSION, expand_to_ntriples
from lae.settings import Settings
from tests.bootstrap.conftest import (
    BASE_URI,
    BROWN_TEA_POT,
    NIGHT_WATCH,
    YCBA_OBJECT,
    YCBA_PERSON,
    YCBA_SET,
    YUAG_OBJECT,
    YUAG_SET,
    corpus,
    corpus_path,
    fetch,
)

AAT = "http://vocab.getty.edu/aat/"


def report_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def make_input(tmp_path: Path) -> tuple[Path, str]:
    """Three valid records, a schema failure, a duplicate, a local record, an unexpandable record
    and a broken line."""
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
    lines = [orjson.dumps(corpus(NIGHT_WATCH)), orjson.dumps(local), UNEXPANDABLE, b"{broken"]
    (source / "more.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    return source, local_uri


UNEXPANDABLE_URI = "https://example.org/object/undated"
UNEXPANDABLE = orjson.dumps(
    {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": UNEXPANDABLE_URI,
        "type": "HumanMadeObject",
        "_label": "A year is not an xsd:dateTime",
        "produced_by": {
            "type": "Production",
            "timespan": {"type": "TimeSpan", "begin_of_the_begin": "1850"},
        },
    }
)


def write_jsonl(path: Path, documents: list[dict[str, Any]]) -> Path:
    path.write_bytes(b"\n".join(orjson.dumps(document) for document in documents) + b"\n")
    return path


def references(value: Any) -> set[str]:
    """Every ``id`` below the root of a document."""
    found: set[str] = set()

    def walk(item: Any, root: bool) -> None:
        if isinstance(item, list):
            for entry in item:
                walk(entry, False)
        elif isinstance(item, dict):
            if not root and isinstance(item.get("id"), str):
                found.add(item["id"])
            for key, entry in item.items():
                if key != "@context":
                    walk(entry, False)

    walk(value, True)
    return found


async def stored(settings: Settings) -> dict[str, dict[str, Any]]:
    """``{source URI (or own URI): row}`` for every record, with ``data`` parsed."""
    rows = await fetch(
        settings,
        "SELECT r.id, r.uri, r.type, r.version, r.status::text AS status, r.etag, "
        "r.data::text AS data, r.context_version, a.alias_uri, a.source AS alias_source "
        "FROM records r LEFT JOIN record_aliases a ON a.record_id = r.id",
    )
    return {
        (row["alias_uri"] or row["uri"]): {**dict(row), "data": json.loads(row["data"])}
        for row in rows
    }


async def test_load_remints_flags_rejects_and_records_sync_state(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    source, local_uri = make_input(tmp_path)
    report = tmp_path / "load-report.jsonl"
    summary = await load(
        source, settings=bootstrap_settings, report_path=report, batch=2, streams=2, workers=2
    )
    assert summary["items"] == 8
    assert summary["records_written"] == 5
    assert summary["records_reminted"] == 4
    assert summary["records_flagged"] == 1
    assert summary["findings"] == {"LAE-SCHEMA-type": 1}
    assert summary["failures"] == {NOT_A_RECORD: 1, DUPLICATE_URI: 1, EXPANSION_FAILED: 1}
    failures = {line["code"]: line for line in report_lines(report)}
    assert failures[NOT_A_RECORD]["source"] == "more.jsonl:4"
    assert failures[EXPANSION_FAILED]["uri"] == UNEXPANDABLE_URI
    assert failures[EXPANSION_FAILED]["errors"][0].startswith("InvalidLiteralError")
    # The two copies are in batches that two workers write concurrently: either may be kept.
    assert failures[DUPLICATE_URI]["uri"] == corpus(NIGHT_WATCH)["id"]
    assert failures[DUPLICATE_URI]["source"] in {"more.jsonl:1", Path(NIGHT_WATCH).name}

    records = await stored(bootstrap_settings)
    sources = {
        corpus(name)["id"] for name in (BROWN_TEA_POT, NIGHT_WATCH, YCBA_PERSON, YUAG_OBJECT)
    }
    assert set(records) == sources | {local_uri}
    for key, row in records.items():
        data = row["data"]
        assert (row["version"], row["status"], row["context_version"]) == (1, "active", 1)
        assert row["etag"] == record_etag(data)
        assert row["uri"] == f"{BASE_URI}{row['id']}" == data["id"]
        expand_to_ntriples(data, 1, record_uri=row["uri"])  # node IRIs everywhere
        if key == local_uri:
            assert (row["alias_uri"], "equivalent" in data) == (None, False)
        else:
            assert row["id"].version == 7
            assert row["alias_source"] == "bootstrap"
            assert {"id": key, "type": row["type"]} in data["equivalent"]
            assert [entry["id"] for entry in data["equivalent"]].count(key) == 1
    # Nothing of the rejected record reached any table.
    assert not await fetch(
        bootstrap_settings,
        "SELECT 1 FROM record_aliases WHERE alias_uri = $1 "
        "UNION ALL SELECT 1 FROM entity_labels WHERE default_label LIKE 'A year%'",
        UNEXPANDABLE_URI,
    )

    labels = await fetch(
        bootstrap_settings,
        "SELECT uri, type, labels::text, default_label, source, status::text FROM entity_labels",
    )
    by_uri = {row["uri"]: row for row in labels}
    assert set(by_uri) == {row["uri"] for row in records.values()}
    night_watch = by_uri[records[corpus(NIGHT_WATCH)["id"]]["uri"]]
    assert json.loads(night_watch["labels"]) == {"en": "The Night Watch", "nl": "De Nachtwacht"}
    assert (night_watch["source"], night_watch["status"]) == ("local", "published")

    findings = await fetch(
        bootstrap_settings,
        "SELECT code, severity::text, record_id, node_id, params::text, status::text, "
        "claimed_by, resolved_by, sweep_run_id FROM qa_findings",
    )
    [finding] = findings
    assert finding["record_id"] == records[corpus(YUAG_OBJECT)["id"]]["id"]
    assert (finding["code"], finding["severity"], finding["status"]) == (
        "LAE-SCHEMA-type",
        "error",
        "open",
    )
    assert (finding["node_id"], finding["claimed_by"], finding["resolved_by"]) == (None,) * 3
    assert str(finding["sweep_run_id"]) == summary["run_id"]
    params = json.loads(finding["params"])
    assert (params["source"], params["count"]) == ("bootstrap", 1)
    assert params["diagnostics"][0]["path"] == "/used_for"

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
    counts = (
        "SELECT (SELECT count(*) FROM records), (SELECT count(*) FROM record_aliases), "
        "(SELECT count(*) FROM qa_findings), (SELECT count(*) FROM entity_labels)"
    )
    rows = "SELECT id, uri, etag, data::text FROM records ORDER BY id"
    before = await fetch(bootstrap_settings, rows)
    counted = await fetch(bootstrap_settings, counts)
    assert tuple(counted[0]) == (5, 4, 1, 5)
    summary = await load(
        source, settings=bootstrap_settings, report_path=tmp_path / "2.jsonl", workers=1
    )
    assert summary["records_written"] == 0
    assert summary["index_rebuild_needed"] is False
    # The six records of the first run; the unexpandable one is rejected again, not a duplicate.
    assert summary["failures"] == {NOT_A_RECORD: 1, DUPLICATE_URI: 6, EXPANSION_FAILED: 1}
    assert summary["rewrite"]["changed"] == 0
    assert await fetch(bootstrap_settings, rows) == before
    assert await fetch(bootstrap_settings, counts) == counted


async def test_a_rejected_item_does_not_make_a_later_one_a_duplicate(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    """Three items with one source URI in one batch: unexpandable, good, good again."""
    good = {**json.loads(UNEXPANDABLE), "produced_by": {"type": "Production"}}
    source = tmp_path / "in.jsonl"
    source.write_bytes(b"\n".join([UNEXPANDABLE, orjson.dumps(good), orjson.dumps(good)]) + b"\n")
    report = tmp_path / "report.jsonl"
    summary = await load(source, settings=bootstrap_settings, report_path=report, workers=1)
    assert summary["records_written"] == 1
    assert summary["failures"] == {DUPLICATE_URI: 1, EXPANSION_FAILED: 1}
    assert [(line["source"], line["code"]) for line in report_lines(report)] == [
        ("in.jsonl:1", EXPANSION_FAILED),
        ("in.jsonl:3", DUPLICATE_URI),
    ]
    assert set(await stored(bootstrap_settings)) == {UNEXPANDABLE_URI}
    again = await load(source, settings=bootstrap_settings, report_path=report, workers=1)
    assert again["records_written"] == 0
    assert again["failures"] == {DUPLICATE_URI: 3}


async def test_references_between_loaded_records_are_rewritten(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    """Two pairs of corpus records, each an object that is a member of a set in the corpus."""
    source = tmp_path / "input"
    source.mkdir()
    names = (YCBA_OBJECT, YCBA_SET, YUAG_OBJECT, YUAG_SET, BROWN_TEA_POT)
    for name in names:
        shutil.copy(corpus_path(name), source / Path(name).name)
    summary = await load(
        source, settings=bootstrap_settings, report_path=tmp_path / "r.jsonl", batch=1, workers=2
    )
    assert summary["records_written"] == 5 and summary["failures"] == {}
    assert summary["rewrite"]["records"] == 5
    assert summary["rewrite"]["references_rewritten"] >= 2
    records = await stored(bootstrap_settings)
    aliases = {key: row["uri"] for key, row in records.items()}
    for member, container in ((YCBA_OBJECT, YCBA_SET), (YUAG_OBJECT, YUAG_SET)):
        row = records[corpus(member)["id"]]
        set_source = corpus(container)["id"]
        assert set_source in references(corpus(member))
        assert set_source not in references(row["data"])
        assert aliases[set_source] in references(row["data"])
        [reference] = [r for r in row["data"]["member_of"] if r["id"] == aliases[set_source]]
        # The target is a loaded record, so its label is transcluded on read, not stored.
        assert "_label" not in reference
        assert row["etag"] == record_etag(row["data"])
        lines = expand_to_ntriples(row["data"], 1, record_uri=row["uri"])
        assert any(
            f"<{row['uri']}>" in line and f"<{aliases[set_source]}>" in line for line in lines
        )
    # No reference anywhere still points at a URI that was re-minted, except each record's own
    # source URI in its equivalent.
    for key, row in records.items():
        assert references(row["data"]) & set(aliases) == {key}


async def test_a_later_load_resolves_references_to_earlier_aliases(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    first = write_jsonl(tmp_path / "first.jsonl", [corpus(YCBA_SET)])
    await load(first, settings=bootstrap_settings, report_path=tmp_path / "1.jsonl", workers=1)
    [(set_uri,)] = await fetch(bootstrap_settings, "SELECT uri FROM records")
    second = write_jsonl(tmp_path / "second.jsonl", [corpus(YCBA_OBJECT)])
    summary = await load(
        second, settings=bootstrap_settings, report_path=tmp_path / "2.jsonl", workers=1
    )
    assert summary["records_written"] == 1
    assert summary["rewrite"]["records"] == 1  # only the rows of this load
    records = await stored(bootstrap_settings)
    data = records[corpus(YCBA_OBJECT)["id"]]["data"]
    assert set_uri in references(data) and corpus(YCBA_SET)["id"] not in references(data)


async def test_rewrite_refs_points_old_rows_at_later_records_and_is_idempotent(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    """The object is loaded before the set it refers to; ``rewrite-refs`` then fixes it."""
    first = write_jsonl(tmp_path / "first.jsonl", [corpus(YCBA_OBJECT)])
    await load(first, settings=bootstrap_settings, report_path=tmp_path / "1.jsonl", workers=1)
    second = write_jsonl(tmp_path / "second.jsonl", [corpus(YCBA_SET)])
    await load(second, settings=bootstrap_settings, report_path=tmp_path / "2.jsonl", workers=1)
    set_source = corpus(YCBA_SET)["id"]
    records = await stored(bootstrap_settings)
    assert set_source in references(records[corpus(YCBA_OBJECT)["id"]]["data"])

    # An index was built in between: what the pass changes is not in it.
    await fetch(
        bootstrap_settings,
        "INSERT INTO sync_state (key, value) VALUES ('qlever_index_watermark', '0')",
    )
    summary = await rewrite_refs(settings=bootstrap_settings, workers=1)
    assert (summary["records"], summary["changed"]) == (2, 1)
    assert summary["references_rewritten"] == 1
    assert summary["index_rebuild_needed"] is True
    records = await stored(bootstrap_settings)
    row = records[corpus(YCBA_OBJECT)["id"]]
    assert records[set_source]["uri"] in references(row["data"])
    assert set_source not in references(row["data"])
    assert row["etag"] == record_etag(row["data"])

    again = await rewrite_refs(settings=bootstrap_settings, workers=1)
    assert (again["records"], again["changed"], again["index_rebuild_needed"]) == (2, 0, False)
    assert await stored(bootstrap_settings) == records


async def test_labels_are_stripped_only_from_references_with_a_known_target(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    csv_path = tmp_path / "aat.csv"
    csv_path.write_text("aat_code,preferred_name\n300033618,paintings\n", encoding="utf-8")
    await load_aat_labels(csv_path, settings=bootstrap_settings, report_path=tmp_path / "a.jsonl")
    source = write_jsonl(tmp_path / "in.jsonl", [corpus(BROWN_TEA_POT)])
    summary = await load(
        source, settings=bootstrap_settings, report_path=tmp_path / "r.jsonl", workers=1
    )
    assert summary["rewrite"]["labels_stripped"] >= 1
    [row] = (await stored(bootstrap_settings)).values()
    labelled = {
        reference["id"]: "_label" in reference for reference in iter_references(row["data"])
    }
    assert labelled[AAT + "300033618"] is False
    # Every other reference is to something with no entity_labels row: its label is all there is.
    source_labels = {
        reference["id"]: "_label" in reference
        for reference in iter_references(corpus(BROWN_TEA_POT))
    }
    for uri, had_label in source_labels.items():
        if uri != AAT + "300033618":
            assert labelled[uri] == had_label
    assert row["etag"] == record_etag(row["data"])
    assert row["data"]["_label"] == "The Brown Tea Pot"  # the root keeps its own


async def test_an_interrupted_load_is_completed_by_the_next_one(
    bootstrap_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    source = write_jsonl(tmp_path / "in.jsonl", [corpus(YCBA_OBJECT), corpus(YCBA_SET)])
    with monkeypatch.context() as patch:
        patch.setattr(lae.bootstrap.load, "rewrite_pass", interrupted)
        with pytest.raises(KeyboardInterrupt):
            await load(
                source, settings=bootstrap_settings, report_path=tmp_path / "1.jsonl", workers=1
            )
    # Both records and their aliases are there; the reference still carries the source URI,
    # which resolves through record_aliases; the marker says the load is unfinished.
    set_source = corpus(YCBA_SET)["id"]
    records = await stored(bootstrap_settings)
    assert len(records) == 2
    assert set_source in references(records[corpus(YCBA_OBJECT)["id"]]["data"])
    marker = await fetch(
        bootstrap_settings, "SELECT 1 FROM sync_state WHERE key = 'bootstrap_rewrite_since'"
    )
    assert len(marker) == 1

    other = write_jsonl(tmp_path / "other.jsonl", [corpus(BROWN_TEA_POT)])
    summary = await load(
        other, settings=bootstrap_settings, report_path=tmp_path / "2.jsonl", workers=1
    )
    assert summary["rewrite"]["records"] == 3
    records = await stored(bootstrap_settings)
    data = records[corpus(YCBA_OBJECT)["id"]]["data"]
    assert records[set_source]["uri"] in references(data) and set_source not in references(data)
    assert not await fetch(
        bootstrap_settings, "SELECT 1 FROM sync_state WHERE key = 'bootstrap_rewrite_since'"
    )


async def test_published_and_drafted_records_are_not_rewritten(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    source = write_jsonl(tmp_path / "in.jsonl", [corpus(YCBA_OBJECT), corpus(YUAG_OBJECT)])
    await load(source, settings=bootstrap_settings, report_path=tmp_path / "1.jsonl", workers=1)
    records = await stored(bootstrap_settings)
    published = records[corpus(YCBA_OBJECT)["id"]]
    drafted = records[corpus(YUAG_OBJECT)["id"]]
    await fetch(
        bootstrap_settings,
        "INSERT INTO record_revisions (record_id, version, ntriples_zstd, jsonld_zstd, "
        "context_version, published_by) VALUES ($1, 1, '', '', 1, $2)",
        published["id"],
        new_uuid7(),
    )
    await fetch(
        bootstrap_settings,
        "INSERT INTO crdt_drafts (record_id, draft_epoch, base_version, state) "
        "VALUES ($1, 0, 1, '')",
        drafted["id"],
    )
    sets = write_jsonl(tmp_path / "sets.jsonl", [corpus(YCBA_SET), corpus(YUAG_SET)])
    await load(sets, settings=bootstrap_settings, report_path=tmp_path / "2.jsonl", workers=1)
    summary = await rewrite_refs(settings=bootstrap_settings, workers=1)
    assert summary["records"] == 2  # the two sets
    after = await stored(bootstrap_settings)
    for key in (corpus(YCBA_OBJECT)["id"], corpus(YUAG_OBJECT)["id"]):
        assert after[key]["data"] == records[key]["data"]
    await fetch(bootstrap_settings, "TRUNCATE record_revisions, crdt_drafts")


async def test_the_update_skips_a_row_that_got_a_draft_after_it_was_selected(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    """The pass selects its rows first; a draft opened before the write must still win."""
    source = write_jsonl(tmp_path / "in.jsonl", [corpus(YCBA_OBJECT), corpus(YUAG_OBJECT)])
    await load(source, settings=bootstrap_settings, report_path=tmp_path / "1.jsonl", workers=1)
    records = await stored(bootstrap_settings)
    drafted, plain = records[corpus(YCBA_OBJECT)["id"]], records[corpus(YUAG_OBJECT)["id"]]
    await fetch(
        bootstrap_settings,
        "INSERT INTO crdt_drafts (record_id, draft_epoch, base_version, state) "
        "VALUES ($1, 0, 1, '')",
        drafted["id"],
    )
    conn = await asyncpg.connect(asyncpg_dsn(bootstrap_settings.database_url))
    try:
        for row in (drafted, plain):
            await conn.execute(lae.bootstrap.rewrite._UPDATE, row["id"], "{}", "new", row["etag"])
    finally:
        await conn.close()
    after = await stored(bootstrap_settings)
    assert after[corpus(YCBA_OBJECT)["id"]]["etag"] == drafted["etag"]
    assert after[corpus(YUAG_OBJECT)["id"]]["etag"] == "new"
    await fetch(bootstrap_settings, "TRUNCATE crdt_drafts")


async def test_only_one_load_runs_at_a_time(bootstrap_settings: Settings, tmp_path: Path) -> None:
    conn = await asyncpg.connect(asyncpg_dsn(bootstrap_settings.database_url))
    try:
        await acquire_load_lock(conn)
        source = write_jsonl(tmp_path / "in.jsonl", [corpus(BROWN_TEA_POT)])
        with pytest.raises(BootstrapError, match="another lae bootstrap"):
            await load(source, settings=bootstrap_settings, report_path=tmp_path / "r.jsonl")
        with pytest.raises(BootstrapError):
            await rewrite_refs(settings=bootstrap_settings)
    finally:
        await conn.close()
    assert not await fetch(bootstrap_settings, "SELECT 1 FROM records")


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
    assert summary["records"] == 5
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
