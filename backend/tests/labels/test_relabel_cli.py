"""``lae relabel`` on the command line (Step 1.19b, contract §2.5 and §5 test 27).

``main`` runs ``asyncio.run``, so the tests that also hold a row lock call it in a worker thread
(``asyncio.to_thread``) while the lock is held from the test's own event loop.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

import lae.cli
import lae.labels.relabel as relabel_module
from lae.cli import MAX_RELABEL_BATCH, build_parser, main
from lae.labels.relabel import RELABEL_BATCH
from lae.settings import get_settings
from tests.labels.relabel_support import (
    AAT,
    defaults,
    empty_tables,
    put_label,
    report_lines,
)
from tests.labels.test_relabel import HeldLock


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Clear the cached settings around a test that sets ``LAE_*`` variables; keep the log lines
    out of stdout, where the summary is printed."""
    get_settings.cache_clear()
    monkeypatch.setattr(lae.cli, "configure_logging", lambda level: None)
    with structlog.testing.capture_logs():
        yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
async def seeded(
    db_url: str, db_engine: AsyncEngine, settings_env: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, str]]:
    """Two AAT rows with a French label, the environment pointing at the test database."""
    await empty_tables(db_url, db_engine)
    await put_label(db_url, f"{AAT}1", {"en": "paintings", "fr": "peintures"}, "paintings")
    await put_label(db_url, f"{AAT}2", {"en": "drawings", "fr": "dessins"}, "drawings")
    settings_env.setenv("LAE_DATABASE_URL", db_url)
    try:
        yield {f"{AAT}1": "peintures", f"{AAT}2": "dessins"}
    finally:
        await empty_tables(db_url, db_engine)


async def test_the_command_relabels_under_the_configured_language(
    seeded: dict[str, str],
    settings_env: pytest.MonkeyPatch,
    db_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings_env.setenv("LAE_LABEL_LANGUAGE", "fr")
    report = tmp_path / "dry.jsonl"
    before = await defaults(db_url)
    assert await asyncio.to_thread(main, ["relabel", "--dry-run", "--report", str(report)]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert (dry["dry_run"], dry["changed_total"], dry["report"]) == (True, 2, str(report))
    assert len(report_lines(report)) == 2
    assert await defaults(db_url) == before

    assert await asyncio.to_thread(main, ["relabel", "--batch", "1"]) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["command"] == "relabel" and summary["label_language"] == "fr"
    assert (summary["batch"], summary["changed"], summary["dry_run"]) == (1, {"aat": 2}, False)
    assert captured.err == ""
    assert await defaults(db_url) == seeded


async def test_there_is_no_language_option(
    seeded: dict[str, str], db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    before = await defaults(db_url)
    for argv in (["relabel", "--language", "fr"], ["relabel", "fr"], ["relabel", "--lang=fr"]):
        with pytest.raises(SystemExit) as exit_info:
            await asyncio.to_thread(main, argv)
        assert exit_info.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert await defaults(db_url) == before


async def test_exit_code_1_when_rows_were_skipped(
    seeded: dict[str, str],
    settings_env: pytest.MonkeyPatch,
    db_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings_env.setenv("LAE_LABEL_LANGUAGE", "fr")
    settings_env.setattr(
        relabel_module, "relabel", functools.partial(relabel_module.relabel, retry_pause=0)
    )
    async with HeldLock(db_url, f"{AAT}1"):
        code = await asyncio.wait_for(asyncio.to_thread(main, ["relabel"]), 30)
    assert code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["skipped_locked"] == 1
    assert captured.err.strip() == (
        "lae relabel: 1 rows were locked by other transactions; run the command again"
    )
    assert await asyncio.to_thread(main, ["relabel"]) == 0
    assert await defaults(db_url) == seeded


async def test_a_failing_run_exits_1_and_keeps_its_batches(
    seeded: dict[str, str],
    settings_env: pytest.MonkeyPatch,
    db_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings_env.setenv("LAE_LABEL_LANGUAGE", "fr")
    apply = relabel_module._apply
    calls = 0

    async def failing(conn: Any, changes: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncpg.PostgresConnectionError("connection lost")
        await apply(conn, changes)

    settings_env.setattr(relabel_module, "_apply", failing)
    assert await asyncio.to_thread(main, ["relabel", "--batch", "1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("lae relabel: PostgresConnectionError: connection lost")
    assert (await defaults(db_url))[f"{AAT}1"] == "peintures"
    assert (await defaults(db_url))[f"{AAT}2"] == "drawings"


@pytest.mark.parametrize("value", ["0", "-1", str(MAX_RELABEL_BATCH + 1), "x", "1.5"])
def test_batch_bounds(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["relabel", "--batch", value])
    assert exit_info.value.code == 2
    assert "--batch" in capsys.readouterr().err


def test_batch_accepts_1_to_10000_and_defaults_to_relabel_batch() -> None:
    parse = build_parser().parse_args
    assert MAX_RELABEL_BATCH == 10_000
    assert parse(["relabel"]).batch == RELABEL_BATCH == 500
    assert parse(["relabel", "--batch", "1"]).batch == 1
    assert parse(["relabel", "--batch", "10000"]).batch == 10_000
    args = parse(["relabel", "--dry-run", "--report", "out.jsonl"])
    assert (args.dry_run, args.report) == (True, Path("out.jsonl"))
    assert parse(["relabel"]).dry_run is False and parse(["relabel"]).report is None


def test_a_bad_label_language_stops_before_the_database(
    settings_env: pytest.MonkeyPatch,
) -> None:
    connects: list[Any] = []

    async def connect(*args: Any, **kwargs: Any) -> None:
        connects.append(args)
        raise AssertionError("the database must not be opened")

    settings_env.setattr(asyncpg, "connect", connect)
    settings_env.setenv("LAE_LABEL_LANGUAGE", "xx")
    with pytest.raises(ValidationError) as error:
        main(["relabel"])
    assert "label_language" in str(error.value)
    assert connects == []
