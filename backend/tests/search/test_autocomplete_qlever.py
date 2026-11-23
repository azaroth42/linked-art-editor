"""Autocomplete against a real QLever, after a real bootstrap (Phase 1 Step 1.13, Verify).

The module bootstraps the golden corpus and the AAT labels into the test database and a fresh
index with the ``lae bootstrap`` commands (``load``, ``aat-labels``, ``export-nt``,
``build-index``), then calls ``GET /api/search/autocomplete`` in-process with the application
talking to that QLever over HTTP. It measures the latency of ``q=rem``-style requests (p95 must
be under 100 ms) and shows the ``entity_labels`` overlay returning an entity published after the
index was built, which QLever's own prefix search cannot see (Spike S1).

Needs the ``qlever`` command (qlever-control) and ``qlever-index``, a free port named by
``LAE_TEST_QLEVER_PORT`` (your slot's QLever port, docs/agent-workflow.md), and the AAT CSV
(``LAE_TEST_AAT_CSV``, default the linkedart-parser copy); skipped otherwise. CI has no QLever.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.app import create_app
from lae.bootstrap.common import asyncpg_dsn
from lae.bootstrap.index import stop_command
from lae.corpus import CORPUS_DIR
from lae.db.models import Record, RecordStatus
from lae.ids import new_uuid7, record_uri
from lae.labels.upsert import upsert_entity_labels_for_record
from lae.projection import assign_node_iris
from lae.rdf import expand_to_ntriples
from lae.search.autocomplete import (
    build_search_query,
    class_iris,
    overlay_rows,
    query_words,
    watermark_time,
)
from lae.sparql.updates import build_update
from lae.sync.qlever_client import QLeverClient
from tests.api.conftest import auth_headers
from tests.auth.conftest import API_BASE_URL, make_settings

pytestmark = pytest.mark.integration

QLEVER_PORT_ENV = "LAE_TEST_QLEVER_PORT"
AAT_CSV_ENV = "LAE_TEST_AAT_CSV"
DEFAULT_AAT_CSV = Path("/Users/rs2668/Development/linked-art/linkedart-parser/data/aat_dataset.csv")
TOKEN = "autocomplete-test-token"
BASE_URI = "https://data.test/id/"
PATH = "/api/search/autocomplete"
TABLES = "records, entity_labels, sync_state, outbox_events"
P95_LIMIT_MS = 100.0

# q=rem-style requests: prefixes of names in the corpus and of AAT terms, as a cataloguer types
# them, for the classes the editor's reference slots ask for.
PREFIXES = [
    "rem",
    "remb",
    "rembrandt",
    "rem van",
    "rembrandt van r",
    "nig",
    "night w",
    "pai",
    "paint",
    "port",
    "gil",
    "have",
    "sas",
    "amst",
    "oil",
    "can",
    "wat",
    "sil",
    "brit",
    "coi",
]
TYPES = [None, "Person,Group", "Type", "Place,Type"]


@dataclass(frozen=True)
class Bootstrapped:
    """The bootstrapped test database and the QLever serving its index."""

    db_url: str
    port: int
    summaries: dict[str, dict[str, Any]]

    @property
    def qlever_url(self) -> str:
        return f"http://localhost:{self.port}"


async def _truncate(db_url: str) -> None:
    conn = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        await conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    finally:
        await conn.close()


def _lae(env: dict[str, str], *args: str) -> dict[str, Any]:
    """Run ``lae <args>`` (the installed CLI) and return the JSON summary it prints."""
    done = subprocess.run(
        [sys.executable, "-m", "lae.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert done.returncode == 0, f"lae {' '.join(args)} failed:\n{done.stderr[-3000:]}"
    summary: dict[str, Any] = json.loads(done.stdout)
    return summary


@pytest.fixture(scope="module")
def bootstrapped(db_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Bootstrapped]:
    """Corpus and AAT labels loaded, exported and indexed; QLever stopped afterwards."""
    if shutil.which("qlever") is None or shutil.which("qlever-index") is None:
        pytest.skip("QLever (qlever-control and qlever-index) is not installed")
    raw_port = os.environ.get(QLEVER_PORT_ENV)
    if not raw_port:
        pytest.skip(f"{QLEVER_PORT_ENV} is not set (use your slot's QLever port)")
    aat_csv = Path(os.environ.get(AAT_CSV_ENV) or DEFAULT_AAT_CSV)
    if not aat_csv.is_file():
        pytest.skip(f"no AAT CSV at {aat_csv} (set {AAT_CSV_ENV})")
    port = int(raw_port)
    work = tmp_path_factory.mktemp("autocomplete")
    name = f"lae_test_{port}"
    env = {
        **os.environ,
        "LAE_DATABASE_URL": db_url,
        "LAE_BASE_URI": BASE_URI,
        "LAE_QLEVER_URL": f"http://localhost:{port}",
        "LAE_QLEVER_UPDATE_URL": f"http://localhost:{port}",
        "LAE_QLEVER_ACCESS_TOKEN": TOKEN,
    }
    asyncio.run(_truncate(db_url))
    try:
        summaries = {
            "load": _lae(
                env, "bootstrap", "load", "--input", str(CORPUS_DIR), "--report", str(work / "l")
            ),
            "aat-labels": _lae(
                env, "bootstrap", "aat-labels", "--csv", str(aat_csv), "--report", str(work / "a")
            ),
            "export-nt": _lae(env, "bootstrap", "export-nt", "--out", str(work / "export.nt")),
            "build-index": _lae(
                env,
                "bootstrap",
                "build-index",
                "--nt",
                str(work / "export.nt"),
                "--dir",
                str(work / "index"),
                "--name",
                name,
            ),
        }
        yield Bootstrapped(db_url, port, summaries)
    finally:
        subprocess.run(stop_command(name, port), cwd=work, capture_output=True, check=False)
        asyncio.run(_truncate(db_url))


@pytest.fixture
async def client(bootstrapped: Bootstrapped) -> AsyncIterator[httpx.AsyncClient]:
    """The application, talking to the bootstrapped QLever over HTTP."""
    settings = make_settings(
        bootstrapped.db_url,
        qlever_url=bootstrapped.qlever_url,
        qlever_update_url=bootstrapped.qlever_url,
        qlever_access_token=SecretStr(TOKEN),
        base_uri=BASE_URI,
    )
    app = create_app(settings, http_transport=httpx.AsyncHTTPTransport())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as http:
            yield http


@pytest.fixture
async def headers(db_engine: AsyncEngine, bootstrapped: Bootstrapped) -> dict[str, str]:
    return await auth_headers(db_engine, "Collaborator")


def qlever(bootstrapped: Bootstrapped) -> QLeverClient:
    return QLeverClient(bootstrapped.qlever_url, bootstrapped.qlever_url, TOKEN)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def test_the_bootstrap_loaded_the_corpus_and_aat_labels(bootstrapped: Bootstrapped) -> None:
    summaries = bootstrapped.summaries
    assert summaries["load"]["records_written"] > 150
    assert summaries["aat-labels"]["aat_rows_total"] > 50_000
    assert summaries["build-index"]["triples_served"] > 100_000
    assert (
        summaries["build-index"]["sync_state"]["qlever_index_watermark_time"]
        == (summaries["export-nt"]["snapshot_at"])
    )


async def test_bootstrapped_labels_are_left_to_qlever(
    bootstrapped: Bootstrapped, db_engine: AsyncEngine
) -> None:
    """Every row the bootstrap wrote predates the snapshot, so the overlay starts empty."""
    async with AsyncSession(db_engine) as session:
        since = await watermark_time(session)
        assert since == datetime.fromisoformat(bootstrapped.summaries["export-nt"]["snapshot_at"])
        assert await overlay_rows(session, "rem", ["Person", "Type"], 20, since=since) == []
        assert len(await overlay_rows(session, "rem", ["Person", "Type"], 20, since=None)) == 20


async def test_q_rem_finds_rembrandt_under_100_ms(
    bootstrapped: Bootstrapped, client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    """``q=rem`` returns Rembrandt-like entities; p95 of q=rem-style requests is under 100 ms."""
    response = await client.get(PATH, params={"q": "rem", "types": "Person"}, headers=headers)
    assert response.status_code == 200, response.text
    results = response.json()
    assert results and all(r["type"] == "Person" for r in results)
    assert all(r["label"].lower().startswith("rem") for r in results[:5])
    assert any("Rembrandt" in r["label"] for r in results)
    assert any(r["context"] == "1606–1669" for r in results), results
    concepts = await client.get(PATH, params={"q": "paintin", "types": "Type"}, headers=headers)
    paintings = [r for r in concepts.json() if r["label"] == "paintings"]
    assert paintings and paintings[0]["source"] == "aat"
    assert paintings[0]["id"] == "http://vocab.getty.edu/aat/300033618"

    # Measure with QLever's cache emptied, each request a query it has not seen.
    async with httpx.AsyncClient() as http:
        cleared = await http.get(
            bootstrapped.qlever_url, params={"cmd": "clear-cache-complete", "access-token": TOKEN}
        )
        cleared.raise_for_status()
    timings: list[float] = []
    for prefix in PREFIXES:
        for types in TYPES:
            params = {"q": prefix} if types is None else {"q": prefix, "types": types}
            started = time.perf_counter()
            response = await client.get(PATH, params=params, headers=headers)
            timings.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 200, (params, response.text)
    p50, p95 = statistics.median(timings), percentile(timings, 0.95)
    report = (
        f"autocomplete latency over {len(timings)} q=rem-style requests (in-process API, "
        f"QLever cache cleared first): p50 {p50:.1f} ms, p95 {p95:.1f} ms, "
        f"max {max(timings):.1f} ms"
    )
    print(report)
    assert p95 < P95_LIMIT_MS, report


async def test_the_overlay_returns_an_entity_published_after_the_build(
    bootstrapped: Bootstrapped,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    db_engine: AsyncEngine,
) -> None:
    """Publish a person after the bootstrap the way Steps 1.10/1.11 will: triples into QLever by
    SPARQL Update, its row into ``entity_labels``. QLever's prefix search does not find it;
    autocomplete does, with its life dates read from QLever."""
    uri = record_uri(new_uuid7(), base_uri=BASE_URI)
    data = assign_node_iris(
        {
            "@context": "https://linked.art/ns/v1/linked-art.json",
            "id": uri,
            "type": "Person",
            "_label": "Remarkable Overlayperson",
            "identified_by": [
                {
                    "type": "Name",
                    "content": "Remarkable Overlayperson",
                    "classified_as": [
                        {"id": "http://vocab.getty.edu/aat/300404670", "type": "Type"}
                    ],
                }
            ],
            "born": {
                "type": "Birth",
                "timespan": {
                    "type": "TimeSpan",
                    "begin_of_the_begin": "1901-02-03T00:00:00Z",
                    "end_of_the_end": "1901-02-03T23:59:59Z",
                },
            },
            "died": {
                "type": "Death",
                "timespan": {
                    "type": "TimeSpan",
                    "begin_of_the_begin": "1977-01-01T00:00:00Z",
                    "end_of_the_end": "1977-12-31T23:59:59Z",
                },
            },
        },
        uri,
    )
    async with qlever(bootstrapped) as index:
        await index.update(build_update([], expand_to_ntriples(data, 1)))
        record = Record(
            id=new_uuid7(), uri=uri, type="Person", status=RecordStatus.ACTIVE, data=data
        )
        async with AsyncSession(db_engine) as session:
            await upsert_entity_labels_for_record(session, record)
            await session.commit()

        # QLever has the triples but its text index cannot see the new name (Spike S1).
        by_type = class_iris(1)
        search = build_search_query(
            query_words("remarkable overl"), "remarkable overl", [by_type["Person"]], 20
        )
        assert await index.query(search) == []
        names = await index.query(
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
            f"SELECT ?label WHERE {{ <{uri}> rdfs:label ?label }}"
        )
        assert names == [{"label": "Remarkable Overlayperson"}]

    for q in ("remarkable overl", "overlayp"):
        response = await client.get(PATH, params={"q": q, "types": "Person"}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == [
            {
                "id": uri,
                "type": "Person",
                "label": "Remarkable Overlayperson",
                "context": "1901–1977",
                "source": "local",
            }
        ]
    # Ranked among the indexed Rembrandts for q=rem. The bulk load keeps the corpus records that
    # fail JSON Schema (ADR-0030), so more than a default page of people match "rem".
    params = {"q": "rem", "types": "Person", "limit": "100"}
    response = await client.get(PATH, params=params, headers=headers)
    assert uri in [r["id"] for r in response.json()]


@pytest.mark.parametrize(
    "q", ["\\u0041rem", "rem\\U0001F600", '"rem" } ; DROP ALL ; #', "rem\\", "o'kee", "18th–19th"]
)
async def test_typed_text_cannot_break_the_query(
    bootstrapped: Bootstrapped, client: httpx.AsyncClient, headers: dict[str, str], q: str
) -> None:
    """Quotes, backslashes and text that looks like a codepoint escape reach QLever intact."""
    response = await client.get(PATH, params={"q": q}, headers=headers)
    assert response.status_code == 200, response.text
