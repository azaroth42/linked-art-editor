"""A bulk load under another label language (Step 1.19a, contract §5 test 18e).

The load stores what it was given (ADR-0030): the source ``_label`` is kept whatever the
setting. Only the ``entity_labels`` default label follows ``Settings.label_language``, because
it comes from ``labels_from_record``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from lae.bootstrap.load import load
from lae.labels.extract import labels_from_record
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
