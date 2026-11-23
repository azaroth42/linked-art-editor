"""Tests for ``lae.bootstrap.index`` (``build-index``).

The end-to-end test builds a real index with QLever and starts it, so it needs the ``qlever``
command (qlever-control) and a free port named by ``LAE_TEST_QLEVER_PORT`` (your slot's QLever
port, docs/agent-workflow.md); it is skipped otherwise. The CI runner does not install QLever yet.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from lae.bootstrap.export import meta_path
from lae.bootstrap.index import (
    IndexBuildError,
    build_index,
    port_of,
    stop_command,
)
from lae.bootstrap.records import prepare
from lae.bootstrap.source import SourceItem
from lae.ids import new_uuid7
from lae.projection import assign_node_iris
from lae.rdf import expand_to_ntriples
from lae.settings import Settings
from lae.sparql.updates import build_update
from tests.bootstrap.conftest import BASE_URI, BROWN_TEA_POT, YCBA_PERSON, corpus_path, fetch

QLEVER_PORT_ENV = "LAE_TEST_QLEVER_PORT"
TOKEN = "bootstrap-test-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def test_stop_command_matches_only_this_index_and_port() -> None:
    regex = re.compile(stop_command("lae_i25", 7025)[-1])
    line = "qlever-server -i lae_i25 -j 4 -p 7025 -m 4G -c 2G -a t --persist-updates"
    assert regex.search(line)
    assert regex.search("/opt/homebrew/bin/" + line)
    assert not regex.search(line.replace("-p 7025", "-p 70250"))
    assert not regex.search(line.replace("lae_i25", "lae_i11"))
    assert not regex.search("qlever stop --cmdline-regex " + line)


def test_port_of() -> None:
    assert port_of("http://localhost:7025") == 7025
    assert port_of("http://localhost") is None
    assert port_of(None) is None


def test_a_watermark_is_required(tmp_path: Path) -> None:
    nt = tmp_path / "x.nt"
    nt.write_text("")
    with pytest.raises(IndexBuildError, match="--watermark"):
        build_index(nt, settings=Settings(), index_dir=tmp_path / "index", port=free_port())


def test_the_index_directory_must_be_fresh(tmp_path: Path) -> None:
    nt = tmp_path / "x.nt"
    nt.write_text("")
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (index_dir / "lae.meta-data.json").write_text("{}")
    with pytest.raises(IndexBuildError, match="not empty"):
        build_index(nt, settings=Settings(), index_dir=index_dir, port=free_port(), watermark=0)


def test_a_busy_port_is_refused(tmp_path: Path) -> None:
    nt = tmp_path / "x.nt"
    nt.write_text("")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        with pytest.raises(IndexBuildError, match="already in use"):
            build_index(nt, settings=Settings(), index_dir=tmp_path / "i", port=port, watermark=0)
    assert not (tmp_path / "i").exists()


@pytest.fixture
def qlever_port() -> Iterator[int]:
    """The slot's QLever test port, or skip."""
    if shutil.which("qlever") is None or shutil.which("qlever-index") is None:
        pytest.skip("QLever (qlever-control and qlever-index) is not installed")
    raw = os.environ.get(QLEVER_PORT_ENV)
    if not raw:
        pytest.skip(f"{QLEVER_PORT_ENV} is not set (use your slot's QLever port)")
    yield int(raw)


def sparql(port: int, query: str) -> list[dict[str, str]]:
    response = httpx.post(
        f"http://localhost:{port}",
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=30,
    )
    response.raise_for_status()
    return [
        {key: value["value"] for key, value in binding.items()}
        for binding in response.json()["results"]["bindings"]
    ]


def test_build_serve_and_update_a_bootstrapped_index(
    bootstrap_settings: Settings, qlever_port: int, tmp_path: Path
) -> None:
    """Build from an export, serve it, and apply the update a first publish sends (Step 1.11)."""
    lines: set[str] = set()
    for name in (BROWN_TEA_POT, YCBA_PERSON):
        record, _ = prepare(SourceItem(name, corpus_path(name).read_bytes()), BASE_URI, 1)
        assert record is not None
        lines |= expand_to_ntriples(json.loads(record.data), 1, record_uri=record.uri)
    painting = "http://vocab.getty.edu/aat/300033618"
    graph = "<urn:graph:sidecar:aat>"
    sidecar = [
        f"<{painting}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        f"<http://www.cidoc-crm.org/cidoc-crm/E55_Type> {graph} .",
        f'<{painting}> <http://www.w3.org/2000/01/rdf-schema#label> "paintings"@en {graph} .',
    ]
    nt = tmp_path / "export.nt"
    nt.write_text("".join(f"{line}\n" for line in [*sorted(lines), *sidecar]))
    meta_path(nt).write_text(
        json.dumps({"watermark": 7, "context_version": 1, "expander_version": 1})
    )
    settings = bootstrap_settings.model_copy(
        update={
            "qlever_url": f"http://localhost:{qlever_port}",
            "qlever_access_token": SecretStr(TOKEN),
        }
    )
    name = f"lae_test_{qlever_port}"
    index_dir = tmp_path / "index"
    try:
        summary = build_index(nt, settings=settings, index_dir=index_dir, name=name)
        assert summary["port"] == qlever_port
        assert summary["triples_served"] == len(lines) + len(sidecar)
        assert (index_dir / "export.nt").is_symlink()
        state = asyncio.run(fetch(settings, "SELECT key, value::text FROM sync_state"))
        assert {row["key"]: json.loads(row["value"]) for row in state} == {
            "qlever_index_watermark": 7,
            "qlever_index_watermark_time": None,
            "bootstrap_context_version": 1,
            "bootstrap_expander_version": 1,
        }

        uri = f"{BASE_URI}{new_uuid7()}"
        new_record = {
            "@context": "https://linked.art/ns/v1/linked-art.json",
            "id": uri,
            "type": "HumanMadeObject",
            "classified_as": [{"id": painting, "type": "Type"}],
            "identified_by": [{"type": "Name", "content": "Published after the bootstrap"}],
        }
        new_lines = expand_to_ntriples(assign_node_iris(new_record, uri), 1)
        response = httpx.post(
            f"http://localhost:{qlever_port}",
            data={"update": build_update([], new_lines)},
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=30,
        )
        response.raise_for_status()
        assert sparql(
            qlever_port,
            "PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/> "
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
            f"SELECT ?name ?type WHERE {{ <{uri}> "
            "crm:P1_is_identified_by/crm:P190_has_symbolic_content ?name ; crm:P2_has_type ?t . "
            f"GRAPH {graph} {{ ?t rdfs:label ?type }} }}",
        ) == [{"name": "Published after the bootstrap", "type": "paintings"}]
    finally:
        subprocess.run(
            stop_command(name, qlever_port), cwd=tmp_path, capture_output=True, check=False
        )
