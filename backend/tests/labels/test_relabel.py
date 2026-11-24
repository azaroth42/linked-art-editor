"""``lae.labels.relabel.relabel`` against PostgreSQL (Step 1.19b, contract §5 tests 2-15).

Every test starts from empty data tables and an empty vocabulary registry (``clean``), because a
run visits every ``entity_labels`` row. Interleavings are forced by patching the module's
coroutines or by a row lock another connection holds; nothing sleeps.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, MutableMapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import orjson
import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import lae.labels.relabel as relabel_module
from lae.bootstrap.common import asyncpg_dsn
from lae.labels.extract import labels_from_record
from lae.projection import ops
from lae.vocab.service import registry_version
from tests.api.conftest import ENGLISH, FRENCH, PRIMARY_NAME
from tests.auth.conftest import Served
from tests.labels.relabel_support import (
    AAT,
    UNTOUCHED_TABLES,
    defaults,
    dump,
    dump_all,
    empty_tables,
    fetch,
    import_registry,
    label_rows,
    load_corpus,
    put_label,
    put_record,
    report_lines,
    run,
)
from tests.publish.conftest import Network, caller, edit, new_draft, person, publish

SUMMARY_KEYS = {
    "command",
    "label_language",
    "dry_run",
    "batch",
    "rows",
    "changed",
    "changed_total",
    "unchanged",
    "no_record",
    "kept",
    "retried",
    "skipped_locked",
    "gone",
    "registry_touched",
    "report",
    "seconds",
}
COUNTS = ("rows", "changed", "changed_total", "unchanged", "no_record", "kept")


@pytest.fixture
async def clean(db_url: str, db_engine: AsyncEngine) -> AsyncIterator[None]:
    """Empty data tables and registry before and after the test."""
    await empty_tables(db_url, db_engine)
    try:
        yield
    finally:
        await empty_tables(db_url, db_engine)


def bilingual(uri: str, english: str, french: str) -> dict[str, Any]:
    """A record with an English and a French primary name, English first."""
    return {
        "id": uri,
        "type": "HumanMadeObject",
        "_label": english,
        "identified_by": [
            {
                "type": "Name",
                "classified_as": [PRIMARY_NAME],
                "content": english,
                "language": [ENGLISH],
            },
            {
                "type": "Name",
                "classified_as": [PRIMARY_NAME],
                "content": french,
                "language": [FRENCH],
            },
        ],
    }


async def put_local(db_url: str, data: dict[str, Any], label_language: str = "en") -> None:
    """A record and its ``entity_labels`` row as publish writes it under ``label_language``."""
    await put_record(db_url, data["id"], data)
    labels, default_label = labels_from_record(data, label_language)
    await put_label(db_url, data["id"], labels, default_label, source="local", type_name="X")


async def seed_seven(db_url: str) -> dict[str, str]:
    """Seven rows, one of each kind; return the default label each must have under ``fr``."""
    await put_local(db_url, bilingual("https://ex.org/a1", "The Bridge", "Le Pont"))
    await put_local(
        db_url,
        {
            "id": "https://ex.org/a2",
            "type": "Person",
            "identified_by": [{"type": "Name", "content": "Ada"}],
        },
    )
    await put_label(db_url, "https://ex.org/a3", {"fr": "Orphelin"}, "Orphan", source="local")
    await put_label(db_url, f"{AAT}1", {"en": "paintings", "fr": "peintures"}, "paintings")
    await put_label(db_url, f"{AAT}2", {"en": "sculpture"}, "sculpture")
    await put_label(db_url, "https://ex.org/w1", {"de": "Gemälde"}, "w1", source="wikidata")
    await put_label(
        db_url, "https://ex.org/w2", {"de": "Gemälde", "fr": "Peinture"}, "w2", source="wikidata"
    )
    return {
        "https://ex.org/a1": "Le Pont",
        "https://ex.org/a2": "Ada",
        "https://ex.org/a3": "Orphan",
        f"{AAT}1": "peintures",
        f"{AAT}2": "sculpture",
        "https://ex.org/w1": "w1",
        "https://ex.org/w2": "Peinture",
    }


SEVEN_UNDER_FR = {
    "rows": 7,
    "changed": {"aat": 1, "local": 1, "wikidata": 1},
    "changed_total": 3,
    "unchanged": 2,
    "no_record": 1,
    "kept": 1,
    "retried": 0,
    "skipped_locked": 0,
    "gone": 0,
}


def counts(
    summary: dict[str, Any], keys: tuple[str, ...] = tuple(SEVEN_UNDER_FR)
) -> dict[str, Any]:
    return {key: summary[key] for key in keys}


def adds_up(summary: dict[str, Any]) -> bool:
    return bool(
        summary["rows"]
        == summary["changed_total"]
        + summary["unchanged"]
        + summary["no_record"]
        + summary["kept"]
        + summary["skipped_locked"]
        + summary["gone"]
        and summary["changed_total"] == sum(summary["changed"].values())
    )


async def publish_a_person(served: Served, network: Network, engine: AsyncEngine) -> None:
    """A record published through the real API, and another left with an edited draft."""
    editor = await caller(engine)
    record_id, _ = await new_draft(served, editor, "Person", person("Ada Lovelace"))
    response = await publish(served, editor, record_id)
    assert response.status_code == 200, response.text
    draft_id, _ = await new_draft(served, editor, "Person", person("Charles Babbage"))
    await edit(
        network,
        draft_id,
        editor.id,
        lambda doc: ops.add_node(doc, "identified_by", {"type": "Name", "content": "Babbage"}),
    )


class HeldLock:
    """Another connection holding ``FOR UPDATE`` on one ``entity_labels`` row."""

    def __init__(self, db_url: str, uri: str) -> None:
        self.db_url, self.uri = db_url, uri
        self.conn: asyncpg.Connection | None = None
        self.transaction: Any = None

    async def __aenter__(self) -> HeldLock:
        self.conn = await asyncpg.connect(asyncpg_dsn(self.db_url))
        self.transaction = self.conn.transaction()
        await self.transaction.start()
        await self.conn.fetch("SELECT 1 FROM entity_labels WHERE uri = $1 FOR UPDATE", self.uri)
        return self

    async def release(self) -> None:
        if self.transaction is not None:
            await self.transaction.rollback()
            self.transaction = None

    async def __aexit__(self, *exc: object) -> None:
        await self.release()
        assert self.conn is not None
        await self.conn.close()


# --- 2-5: what a run computes and writes --------------------------------------------------------


async def test_relabel_under_the_same_language_changes_nothing(
    clean: None,
    served: Served,
    network: Network,
    db_url: str,
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    await load_corpus(db_url, "en", tmp_path)
    await publish_a_person(served, network, db_engine)
    await put_label(db_url, f"{AAT}300033618", {"en": "paintings"}, "paintings")
    before = await dump(db_url, "entity_labels")
    assert len(before) > 250

    summary = await run(db_url, "en")

    assert summary["changed_total"] == 0 and summary["changed"] == {}
    assert summary["skipped_locked"] == 0 and summary["gone"] == 0
    assert summary["rows"] == len(before)
    assert await dump(db_url, "entity_labels") == before


async def test_the_default_label_becomes_what_publish_would_write(
    clean: None, db_url: str, tmp_path: Path
) -> None:
    await load_corpus(db_url, "en", tmp_path)
    local = await fetch(db_url, "SELECT count(*) AS n FROM entity_labels WHERE source = 'local'")
    # Under de there is no German name: the first primary name, English, again.
    for language, mona_lisa in (("fr", "La Joconde"), ("de", "Mona Lisa")):
        summary = await run(db_url, language)
        assert summary["changed_total"] >= 1, language
        assert summary["skipped_locked"] == summary["no_record"] == 0
        rows = await fetch(
            db_url,
            "SELECT e.default_label, r.data::text AS data FROM entity_labels e "
            "JOIN records r ON r.uri = e.uri WHERE e.source = 'local'",
        )
        assert len(rows) == local[0]["n"] > 250
        for row in rows:
            data = json.loads(row["data"])
            assert row["default_label"] == labels_from_record(data, language)[1], data["id"]
        found = await fetch(
            db_url,
            "SELECT e.default_label FROM record_aliases a JOIN records r ON r.id = a.record_id "
            "JOIN entity_labels e ON e.uri = r.uri WHERE a.alias_uri = $1",
            "https://linked.art/example/object/0",
        )
        assert [row["default_label"] for row in found] == [mona_lisa]


async def test_relabel_writes_only_default_label(
    clean: None,
    served: Served,
    network: Network,
    db_url: str,
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    await load_corpus(db_url, "en", tmp_path)
    await publish_a_person(served, network, db_engine)
    await import_registry(db_engine)
    await put_label(db_url, f"{AAT}1", {"en": "paintings", "fr": "peintures"}, "paintings")
    # A local row whose labels are not what its record gives: recomputing them would show.
    await put_local(db_url, bilingual("https://ex.org/b1", "The Bridge", "Le Pont"))
    await fetch(
        db_url,
        "UPDATE entity_labels SET labels = '{\"xx\": \"by hand\"}' WHERE uri = 'https://ex.org/b1'",
    )
    for table in UNTOUCHED_TABLES:
        assert await dump(db_url, table), f"{table} must have rows for this test"
    tables_before = await dump_all(db_url)
    vocabularies_before = await dump(db_url, "vocabularies")
    before = await label_rows(db_url)

    summary = await run(db_url, "fr")

    assert summary["changed"]["local"] >= 2 and summary["changed"]["aat"] == 1
    after = await label_rows(db_url)
    assert set(after) == set(before)
    changed = {uri for uri in before if before[uri]["default_label"] != after[uri]["default_label"]}
    assert len(changed) == summary["changed_total"]
    assert {"https://ex.org/b1", f"{AAT}1"} <= changed
    for uri, row in before.items():
        assert {k: v for k, v in after[uri].items() if k != "default_label"} == {
            k: v for k, v in row.items() if k != "default_label"
        }, uri
    assert after["https://ex.org/b1"]["labels"] == {"xx": "by hand"}
    assert await dump_all(db_url) == tables_before
    vocabularies_after = await dump(db_url, "vocabularies")
    moved = [
        (json.loads(old), json.loads(new))
        for old, new in zip(
            sorted(vocabularies_before, key=lambda r: json.loads(r)["id"]),
            sorted(vocabularies_after, key=lambda r: json.loads(r)["id"]),
            strict=True,
        )
        if old != new
    ]
    assert len(moved) == 1
    [(old, new)] = moved
    assert {k for k in old if old[k] != new[k]} == {"updated_at"}


async def test_relabel_is_idempotent_and_reversible(
    clean: None, db_url: str, tmp_path: Path
) -> None:
    await load_corpus(db_url, "en", tmp_path)
    await put_label(db_url, f"{AAT}1", {"en": "paintings", "fr": "peintures"}, "paintings")
    await put_label(db_url, f"{AAT}2", {"en": "sculpture", "fr-CA": "sculpture (CA)"}, "sculpture")
    first = await dump(db_url, "entity_labels")

    french = await run(db_url, "fr")
    assert french["changed_total"] >= 3
    after_french = await dump(db_url, "entity_labels")
    again = await run(db_url, "fr")
    assert again["changed_total"] == 0 and again["changed"] == {}
    assert await dump(db_url, "entity_labels") == after_french
    back = await run(db_url, "en")
    assert back["changed_total"] == french["changed_total"]

    assert await dump(db_url, "entity_labels") == first


# --- 6: dry run ------------------------------------------------------------------------------


async def test_dry_run_writes_nothing_and_reports_what_the_run_then_changes(
    clean: None, db_url: str, db_engine: AsyncEngine, tmp_path: Path
) -> None:
    await load_corpus(db_url, "en", tmp_path)
    await import_registry(db_engine)
    expected = await seed_seven(db_url)
    tables = (*UNTOUCHED_TABLES, "entity_labels", "vocabularies")
    before = await dump_all(db_url, tables)
    version = await current_registry_version(db_engine)

    # A dry run takes no lock, so a row another transaction holds is read, not skipped.
    async with HeldLock(db_url, f"{AAT}1"):
        dry = await asyncio.wait_for(
            run(db_url, "fr", dry_run=True, report_path=tmp_path / "dry.jsonl"), 30
        )
    assert dry["dry_run"] is True and dry["registry_touched"] is False
    assert dry["skipped_locked"] == dry["retried"] == dry["gone"] == 0
    assert await dump_all(db_url, tables) == before
    assert await current_registry_version(db_engine) == version

    real = await run(db_url, "fr", report_path=tmp_path / "real.jsonl")
    assert counts(dry, COUNTS) == counts(real, COUNTS)
    assert dry["changed"]["aat"] == 1 and dry["changed"]["local"] >= 2
    assert report_lines(tmp_path / "dry.jsonl") == report_lines(tmp_path / "real.jsonl")
    assert len(report_lines(tmp_path / "real.jsonl")) == real["changed_total"]
    assert {uri: (await defaults(db_url))[uri] for uri in expected} == expected


# --- 7-9: which rows, and every row once ----------------------------------------------------------


async def test_non_local_rows_follow_the_label_language(clean: None, db_url: str) -> None:
    rows = {
        f"{AAT}1": ({"en": "paintings"}, "paintings", "paintings"),
        f"{AAT}2": ({"en": "paintings", "fr": "peintures"}, "paintings", "peintures"),
        f"{AAT}3": ({"en": "sculpture", "fr-CA": "sculpture (CA)"}, "x", "sculpture (CA)"),
        f"{AAT}4": ({}, "nothing", "nothing"),
        f"{AAT}5": ({"fr": "  ", "en": "drawings"}, "stale", "drawings"),
        "https://ex.org/w1": ({"de": "Gemälde"}, "w1", "w1"),
        "https://ex.org/w2": ({"de": "Gemälde", "fr": "Peinture"}, "w2", "Peinture"),
    }
    for uri, (labels, default_label, _) in rows.items():
        source = "wikidata" if uri.startswith("https://ex.org/w") else "aat"
        await put_label(db_url, uri, labels, default_label, source=source)
    labels_before = {uri: row["labels"] for uri, row in (await label_rows(db_url)).items()}

    summary = await run(db_url, "fr")

    assert await defaults(db_url) == {uri: new for uri, (_, _, new) in rows.items()}
    assert summary["changed"] == {"aat": 3, "wikidata": 1}
    assert (summary["unchanged"], summary["kept"], summary["no_record"]) == (1, 2, 0)
    assert {uri: row["labels"] for uri, row in (await label_rows(db_url)).items()} == labels_before


async def test_a_local_row_without_a_record_is_counted_and_left_alone(
    clean: None, db_url: str
) -> None:
    await put_label(db_url, "https://ex.org/orphan", {"fr": "Orphelin"}, "Orphan", source="local")
    before = await dump(db_url, "entity_labels")
    summary = await run(db_url, "fr")
    assert (summary["rows"], summary["no_record"], summary["changed_total"]) == (1, 1, 0)
    assert await dump(db_url, "entity_labels") == before


@pytest.mark.parametrize("batch", [1, 2, 3, 500])
async def test_every_row_is_visited_exactly_once_for_any_batch_size(
    clean: None, db_url: str, batch: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = await seed_seven(db_url)
    pages: list[list[str]] = []
    page = relabel_module._page

    async def recorded(conn: Any, after: str, limit: int) -> list[str]:
        uris = await page(conn, after, limit)
        pages.append(uris)
        return uris

    monkeypatch.setattr(relabel_module, "_page", recorded)
    summary = await run(db_url, "fr", batch=batch)

    assert counts(summary) == SEVEN_UNDER_FR
    assert summary["batch"] == batch
    visited = [uri for uris in pages for uri in uris]
    assert sorted(visited) == sorted(expected) and len(visited) == 7
    assert all(len(uris) <= batch for uris in pages) and pages[-1] == []
    assert await defaults(db_url) == expected


# --- 10-12: locks, deletions, failures ---------------------------------------------------------


async def test_a_locked_row_is_skipped_without_waiting_and_reported(
    clean: None, db_url: str
) -> None:
    expected = await seed_seven(db_url)
    locked = f"{AAT}1"
    async with HeldLock(db_url, locked):
        summary = await asyncio.wait_for(run(db_url, "fr", batch=2), 30)
        assert summary["skipped_locked"] == 1 and summary["gone"] == 0
        assert summary["changed_total"] == 2 and adds_up(summary)
        labels = await defaults(db_url)
        assert labels[locked] == "paintings"
        assert {uri: labels[uri] for uri in expected if uri != locked} == {
            uri: label for uri, label in expected.items() if uri != locked
        }
    again = await run(db_url, "fr")
    assert again["changed_total"] == 1 and again["changed"] == {"aat": 1}
    assert again["skipped_locked"] == 0
    assert await defaults(db_url) == expected


async def test_a_deferred_row_is_relabelled_on_a_retry_pass(
    clean: None, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = await seed_seven(db_url)
    pauses: list[float] = []
    async with HeldLock(db_url, f"{AAT}1") as held:

        async def pause(seconds: float) -> None:
            pauses.append(seconds)
            await held.release()

        monkeypatch.setattr(relabel_module, "asyncio", SimpleNamespace(sleep=pause))
        summary = await asyncio.wait_for(run(db_url, "fr", retry_pause=1.5), 30)

    assert pauses == [1.5]
    assert summary["retried"] == 1 and summary["skipped_locked"] == 0
    assert counts(summary) == {**SEVEN_UNDER_FR, "retried": 1}
    assert await defaults(db_url) == expected


async def test_a_row_deleted_during_the_run_is_gone_not_skipped(
    clean: None, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = await seed_seven(db_url)
    deleted = f"{AAT}1"
    page = relabel_module._page

    async def page_then_delete(conn: Any, after: str, limit: int) -> list[str]:
        uris = await page(conn, after, limit)
        if deleted in uris:
            await fetch(db_url, "DELETE FROM entity_labels WHERE uri = $1", deleted)
        return uris

    monkeypatch.setattr(relabel_module, "_page", page_then_delete)
    summary = await run(db_url, "fr")

    assert (summary["rows"], summary["gone"], summary["skipped_locked"]) == (7, 1, 0)
    assert summary["changed"] == {"local": 1, "wikidata": 1} and adds_up(summary)
    del expected[deleted]
    assert await defaults(db_url) == expected


async def test_a_run_that_fails_midway_keeps_its_batches_and_a_second_run_finishes(
    clean: None, db_url: str, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await import_registry(db_engine)
    uris = [f"{AAT}{n}" for n in range(1, 6)]
    for n, uri in enumerate(uris):
        await put_label(db_url, uri, {"en": f"term {n}", "fr": f"terme {n}"}, f"term {n}")
    vocabularies = await dump(db_url, "vocabularies")
    apply = relabel_module._apply
    calls = 0

    async def failing_apply(conn: Any, changes: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("the database went away")
        await apply(conn, changes)

    monkeypatch.setattr(relabel_module, "_apply", failing_apply)
    with pytest.raises(RuntimeError):
        await run(db_url, "fr", batch=2)

    labels = await defaults(db_url)
    assert [labels[uri] for uri in sorted(uris)] == [
        "terme 0",
        "terme 1",
        "term 2",
        "term 3",
        "term 4",
    ]
    assert await dump(db_url, "vocabularies") == vocabularies

    monkeypatch.setattr(relabel_module, "_apply", apply)
    summary = await run(db_url, "fr", batch=2)
    assert summary["changed_total"] == 3 and summary["unchanged"] == 2
    assert summary["registry_touched"] is True
    assert await defaults(db_url) == {uri: f"terme {n}" for n, uri in enumerate(uris)}
    assert await dump(db_url, "vocabularies") != vocabularies


# --- 13-15: registry, report, summary, log -------------------------------------------------------


async def current_registry_version(engine: AsyncEngine) -> str:
    async with AsyncSession(engine) as session:
        return await registry_version(session)


async def test_the_registry_moves_once_when_a_run_completes(
    clean: None, db_url: str, db_engine: AsyncEngine
) -> None:
    await seed_seven(db_url)
    summary = await run(db_url, "fr")
    assert summary["registry_touched"] is False  # no Value Sets

    await import_registry(db_engine)
    version, rows = await current_registry_version(db_engine), await dump(db_url, "vocabularies")
    dry = await run(db_url, "en", dry_run=True)
    assert dry["registry_touched"] is False and dry["changed_total"] == 2
    assert await current_registry_version(db_engine) == version
    assert await dump(db_url, "vocabularies") == rows

    for language, changed_total in (("en", 2), ("en", 0)):
        summary = await run(db_url, language)
        assert summary["changed_total"] == changed_total
        assert summary["registry_touched"] is True
        moved = await current_registry_version(db_engine)
        assert moved != version
        after = await dump(db_url, "vocabularies")
        old_rows = {json.loads(row)["id"]: json.loads(row) for row in rows}
        new_rows = {json.loads(row)["id"]: json.loads(row) for row in after}
        differing = [key for key in old_rows if old_rows[key] != new_rows[key]]
        assert len(differing) == 1
        assert {k for k, v in old_rows[differing[0]].items() if new_rows[differing[0]][k] != v} == {
            "updated_at"
        }
        version, rows = moved, after


async def test_the_report_lists_each_change(clean: None, db_url: str, tmp_path: Path) -> None:
    await seed_seven(db_url)
    report = tmp_path / "relabel.jsonl"
    summary = await run(db_url, "fr", batch=2, report_path=report)
    assert summary["report"] == str(report)
    assert report_lines(report) == [
        {"uri": f"{AAT}1", "source": "aat", "old": "paintings", "new": "peintures"},
        {"uri": "https://ex.org/a1", "source": "local", "old": "The Bridge", "new": "Le Pont"},
        {"uri": "https://ex.org/w2", "source": "wikidata", "old": "w2", "new": "Peinture"},
    ]
    assert len(report_lines(report)) == summary["changed_total"]
    assert (await run(db_url, "fr"))["report"] is None


async def test_the_summary_adds_up(
    clean: None, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_seven(db_url)
    await put_label(db_url, f"{AAT}9", {"en": "gone soon", "fr": "bientôt"}, "gone soon")
    page = relabel_module._page

    async def page_then_delete(conn: Any, after: str, limit: int) -> list[str]:
        uris = await page(conn, after, limit)
        if f"{AAT}9" in uris:
            await fetch(db_url, "DELETE FROM entity_labels WHERE uri = $1", f"{AAT}9")
        return uris

    monkeypatch.setattr(relabel_module, "_page", page_then_delete)
    async with HeldLock(db_url, f"{AAT}2"):
        summary = await asyncio.wait_for(run(db_url, "fr", batch=3), 30)

    assert set(summary) == SUMMARY_KEYS
    assert summary["command"] == "relabel" and summary["label_language"] == "fr"
    assert summary["dry_run"] is False and summary["batch"] == 3
    assert isinstance(summary["seconds"], float)
    assert counts(summary) == {
        **SEVEN_UNDER_FR,
        "rows": 8,
        "unchanged": 1,
        "skipped_locked": 1,
        "gone": 1,
    }
    assert adds_up(summary)
    for batch in (1, 500):
        assert adds_up(await run(db_url, "de", batch=batch))
        assert adds_up(await run(db_url, "fr", batch=batch, dry_run=True))


def event_named(logs: Sequence[MutableMapping[str, Any]], name: str) -> list[Any]:
    return [entry for entry in logs if entry["event"] == name]


async def test_the_log_names_the_language_and_the_counts_and_no_label_text(
    clean: None, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = await seed_seven(db_url)
    for n in range(4):
        await put_label(db_url, f"{AAT}5{n}", {"en": f"filler {n}"}, f"filler {n}")
    with structlog.testing.capture_logs() as logs:
        summary = await run(db_url, "fr", batch=1)

    [started] = event_named(logs, "relabel_started")
    assert {k: started[k] for k in ("label_language", "dry_run", "batch")} == {
        "label_language": "fr",
        "dry_run": False,
        "batch": 1,
    }
    assert "operator" in started
    [finished] = event_named(logs, "relabel_finished")
    assert {k: finished[k] for k in SUMMARY_KEYS} == summary
    assert finished["operator"] == started["operator"]
    [progress] = event_named(logs, "relabel_progress")  # after the tenth batch of eleven
    assert (progress["rows"], progress["deferred"]) == (10, 0)
    assert not event_named(logs, "relabel_failed")
    texts = {"Le Pont", "The Bridge", "peintures", "paintings", "Peinture", "Ada", "Orphan"}
    texts |= set(expected.values()) - {"w1"}  # "w1" is also part of a URI, which may be logged
    logged = orjson.dumps(logs, default=str).decode()
    assert not [text for text in texts if text in logged]

    async def failing(conn: Any, changes: Any) -> None:
        raise ConnectionError("lost")

    monkeypatch.setattr(relabel_module, "_apply", failing)
    with structlog.testing.capture_logs() as logs, pytest.raises(ConnectionError):
        await run(db_url, "de")
    [failed] = event_named(logs, "relabel_failed")
    assert failed["error"] == "ConnectionError" and failed["log_level"] == "error"
    assert not event_named(logs, "relabel_finished")
