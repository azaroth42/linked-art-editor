"""``lae bootstrap aat-labels``: seed ``entity_labels`` with Getty AAT terms.

This is the Phase 1 stand-in for the AAT sidecar dataset (decision A17); the Phase 2 sidecar
manager replaces it with the full AAT as Linked Art. The input is the ``aat_dataset.csv`` of
``linkedart-parser`` (columns ``aat_code``, ``preferred_name``, ``description``). Each row
becomes ``uri = http://vocab.getty.edu/aat/<aat_code>``, ``type = 'Type'``,
``labels = {"en": preferred_name}``, ``default_label = preferred_name``, ``source = 'aat'``,
``status = 'published'``. Running it again updates those columns in place and leaves
``metatype_uris`` and ``vocab_ids`` alone (Phase 2 maintains them). A row without a numeric code
or a name, or one that repeats a code, is reported and skipped.

``lae bootstrap export-nt`` writes these rows to QLever graph ``urn:graph:sidecar:aat``.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any, Final, NamedTuple

import asyncpg

from lae.bootstrap.common import CSV_ROW_INVALID, Failure, Report, asyncpg_dsn
from lae.db.models import EntityLabelStatus
from lae.settings import Settings

STAGE: Final = "aat-labels"
AAT_PREFIX: Final = "http://vocab.getty.edu/aat/"
AAT_SOURCE: Final = "aat"
AAT_TYPE: Final = "Type"
AAT_LANGUAGE: Final = "en"

_UPSERT: Final = """
INSERT INTO entity_labels AS e (uri, type, labels, default_label, source, status)
SELECT uri, $1, labels, default_label, $2, $3::entity_labels_status FROM aat_labels_stage
ON CONFLICT (uri) DO UPDATE SET
    type = EXCLUDED.type,
    labels = EXCLUDED.labels,
    default_label = EXCLUDED.default_label,
    source = EXCLUDED.source,
    status = EXCLUDED.status,
    updated_at = now()
RETURNING (xmax = 0) AS inserted
"""


class AatTerm(NamedTuple):
    """One AAT concept from the CSV."""

    uri: str
    label: str


def read_aat_csv(path: Path) -> tuple[list[AatTerm], list[Failure]]:
    """Read the CSV; return the usable terms (first occurrence of each code) and the rest."""
    terms: list[AatTerm] = []
    failures: list[Failure] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            source = f"{path.name}:{reader.line_num}"
            code = (row.get("aat_code") or "").strip()
            label = (row.get("preferred_name") or "").strip()
            uri = AAT_PREFIX + code if code else None
            problem = None
            if not code.isascii() or not code.isdigit():
                problem = f"aat_code {code!r} is not a number"
            elif not label:
                problem = "preferred_name is empty"
            elif code in seen:
                problem = f"aat_code {code} repeats an earlier row"
            if problem is not None:
                failures.append(Failure(STAGE, source, CSV_ROW_INVALID, uri, [problem]))
                continue
            seen.add(code)
            terms.append(AatTerm(AAT_PREFIX + code, label))
    return terms, failures


async def load_aat_labels(
    csv_path: Path, *, settings: Settings, report_path: Path
) -> dict[str, Any]:
    """Upsert one ``entity_labels`` row per AAT term in ``csv_path``; return a summary."""
    started = time.perf_counter()
    terms, failures = read_aat_csv(csv_path)
    with Report(report_path) as report:
        report.write(failures)
    conn = await asyncpg.connect(asyncpg_dsn(settings.database_url))
    try:
        async with conn.transaction():
            await conn.execute(
                "CREATE TEMPORARY TABLE aat_labels_stage "
                "(uri text, labels jsonb, default_label text) ON COMMIT DROP"
            )
            await conn.copy_records_to_table(
                "aat_labels_stage",
                records=[
                    (term.uri, json.dumps({AAT_LANGUAGE: term.label}), term.label) for term in terms
                ],
                columns=("uri", "labels", "default_label"),
            )
            rows = await conn.fetch(
                _UPSERT, AAT_TYPE, AAT_SOURCE, EntityLabelStatus.PUBLISHED.value
            )
        total = await conn.fetchval(
            "SELECT count(*) FROM entity_labels WHERE source = $1", AAT_SOURCE
        )
    finally:
        await conn.close()
    inserted = sum(1 for row in rows if row["inserted"])
    return {
        "stage": STAGE,
        "csv": str(csv_path),
        "terms": len(terms),
        "inserted": inserted,
        "updated": len(rows) - inserted,
        "rows_skipped": len(failures),
        "report": str(report_path),
        "aat_rows_total": total,
        "seconds": round(time.perf_counter() - started, 2),
    }
