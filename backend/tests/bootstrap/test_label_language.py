"""A bulk load under another label language (Step 1.19a, contract §5 test 18e), and ``lae
relabel`` after a load and after the AAT seed (Step 1.19b, contract §5 tests 20-21).

The load stores what it was given (ADR-0030): the source ``_label`` is kept whatever the
setting. Only the ``entity_labels`` default label follows ``Settings.label_language``, because
it comes from ``labels_from_record``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from lae.bootstrap.aat import AatTerm, aat_row, load_aat_labels
from lae.bootstrap.load import load
from lae.labels.extract import labels_from_record
from lae.labels.relabel import relabel
from lae.projection import strip_internal_iris
from lae.settings import Settings
from tests.bootstrap.conftest import (
    BROWN_TEA_POT,
    NIGHT_WATCH,
    TABLES,
    YCBA_PERSON,
    corpus,
    corpus_path,
    fetch,
)

MONA_LISA = "linkedart/object-0.json"
"""Primary names in English, French and Italian."""

SOURCES = (MONA_LISA, NIGHT_WATCH, BROWN_TEA_POT, YCBA_PERSON)

AAT = "http://vocab.getty.edu/aat/"


async def load_under(settings: Settings, source: Path, tmp_path: Path) -> dict[str, Any]:
    """Load ``source`` on an emptied database; ``{source URI: (data, default_label)}``."""
    await fetch(settings, f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    await load(source, settings=settings, report_path=tmp_path / "report.jsonl", workers=1)
    rows = await fetch(
        settings,
        "SELECT r.uri, r.data::text AS data, a.alias_uri, l.default_label "
        "FROM records r JOIN record_aliases a ON a.record_id = r.id "
        "JOIN entity_labels l ON l.uri = r.uri",
    )
    loaded = {}
    for row in rows:
        data = json.loads(row["data"])
        # Node IRIs and the re-minted URI are new on every load; everything else must match.
        public = strip_internal_iris(data, row["uri"])
        del public["id"]
        loaded[row["alias_uri"]] = (public, row["default_label"])
    return loaded


async def test_the_load_computes_default_labels_in_the_label_language(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    source = tmp_path / "input"
    source.mkdir()
    for name in SOURCES:
        shutil.copy(corpus_path(name), source / Path(name).name)

    english = await load_under(bootstrap_settings, source, tmp_path)
    french_settings = bootstrap_settings.model_copy(update={"label_language": "fr"})
    french = await load_under(french_settings, source, tmp_path)

    assert set(english) == set(french) == {corpus(name)["id"] for name in SOURCES}
    differing = set()
    for name in SOURCES:
        original = corpus(name)
        (en_data, en_default), (fr_data, fr_default) = (
            english[original["id"]],
            french[original["id"]],
        )
        # records.data is the same under both languages, the source _label kept in both.
        assert en_data == fr_data, name
        assert en_data.get("_label") == original.get("_label"), name
        assert en_default == labels_from_record(original, "en")[1], name
        assert fr_default == labels_from_record(original, "fr")[1], name
        if en_default != fr_default:
            differing.add(name)
    # Only where a French primary name exists.
    assert differing == {MONA_LISA}
    assert french[corpus(MONA_LISA)["id"]][1] == "La Joconde"
    assert english[corpus(MONA_LISA)["id"]][1] == "Mona Lisa"


# --- Step 1.19b: relabel after a load, and the AAT seed (contract 1.19b §5 tests 20-21) ---------


async def entity_label_columns(settings: Settings) -> dict[str, dict[str, Any]]:
    """``source URI → every entity_labels column but uri and updated_at`` of the loaded records."""
    rows = await fetch(
        settings,
        "SELECT a.alias_uri, (to_jsonb(l) - 'uri' - 'updated_at')::text AS row "
        "FROM entity_labels l JOIN records r ON r.uri = l.uri "
        "JOIN record_aliases a ON a.record_id = r.id",
    )
    return {row["alias_uri"]: json.loads(row["row"]) for row in rows}


async def test_load_under_en_then_relabel_under_fr_equals_load_under_fr(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    source = tmp_path / "input"
    source.mkdir()
    for name in SOURCES:
        shutil.copy(corpus_path(name), source / Path(name).name)
    french_settings = bootstrap_settings.model_copy(update={"label_language": "fr"})

    await load_under(french_settings, source, tmp_path)
    loaded_under_french = await entity_label_columns(french_settings)

    await load_under(bootstrap_settings, source, tmp_path)
    records_before = await fetch(bootstrap_settings, "SELECT uri, data::text FROM records")
    summary = await relabel(bootstrap_settings.database_url, "fr", retry_pause=0)
    assert summary["changed"] == {"local": 1}  # the Mona Lisa
    assert len(loaded_under_french) == len(SOURCES)
    assert await entity_label_columns(bootstrap_settings) == loaded_under_french
    # The relabel did not touch records.data (the source _label stays).
    assert await fetch(bootstrap_settings, "SELECT uri, data::text FROM records") == records_before


def aat_csv(tmp_path: Path) -> Path:
    path = tmp_path / "aat.csv"
    path.write_text(
        "aat_code,preferred_name,description\n"
        "300033618,paintings,visual works\n"
        "300015050,oil paint,a medium\n",
        encoding="utf-8",
    )
    return path


async def aat_rows(settings: Settings) -> dict[str, tuple[Any, str]]:
    rows = await fetch(
        settings,
        "SELECT uri, labels::text AS labels, default_label FROM entity_labels WHERE source = 'aat'",
    )
    return {row["uri"]: (json.loads(row["labels"]), row["default_label"]) for row in rows}


async def test_the_aat_seed_writes_english_labels_and_the_english_default_under_any_language(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    term = AatTerm(f"{AAT}300033618", "paintings")
    for language in ("en", "fr"):
        assert aat_row(term, language) == (term.uri, '{"en": "paintings"}', "paintings")
        settings = bootstrap_settings.model_copy(update={"label_language": language})
        await fetch(settings, f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        await load_aat_labels(aat_csv(tmp_path), settings=settings, report_path=tmp_path / "r")
        assert await aat_rows(settings) == {
            f"{AAT}300033618": ({"en": "paintings"}, "paintings"),
            f"{AAT}300015050": ({"en": "oil paint"}, "oil paint"),
        }, language


async def test_relabel_after_the_aat_seed_changes_no_aat_row(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    french = bootstrap_settings.model_copy(update={"label_language": "fr"})
    await load_aat_labels(aat_csv(tmp_path), settings=french, report_path=tmp_path / "r")
    before = await aat_rows(french)
    summary = await relabel(french.database_url, "fr", retry_pause=0)
    assert "aat" not in summary["changed"]
    assert summary["unchanged"] >= 2
    assert await aat_rows(french) == before


async def test_an_aat_row_with_a_label_in_the_label_language_gets_it(
    bootstrap_settings: Settings, tmp_path: Path
) -> None:
    csv_path = aat_csv(tmp_path)
    await load_aat_labels(csv_path, settings=bootstrap_settings, report_path=tmp_path / "r")
    await fetch(
        bootstrap_settings,
        'UPDATE entity_labels SET labels = labels || \'{"fr": "peintures"}\' WHERE uri = $1',
        f"{AAT}300033618",
    )
    french = await relabel(bootstrap_settings.database_url, "fr", retry_pause=0)
    assert french["changed"] == {"aat": 1}
    rows = await aat_rows(bootstrap_settings)
    assert rows[f"{AAT}300033618"] == ({"en": "paintings", "fr": "peintures"}, "peintures")
    assert rows[f"{AAT}300015050"] == ({"en": "oil paint"}, "oil paint")
    english = await relabel(bootstrap_settings.database_url, "en", retry_pause=0)
    assert english["changed"] == {"aat": 1}
    assert (await aat_rows(bootstrap_settings))[f"{AAT}300033618"][1] == "paintings"
