"""``lae vocab <action>`` (Step 2.1): the Verify round trip through the CLI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lae.cli import main
from lae.settings import get_settings
from lae.vocab.loader import VOCAB_DIR
from tests.vocab.test_loader import SEED_SETS, same_tree


@pytest.fixture
def lae_env(db_url: str, empty: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``LAE_DATABASE_URL`` is the test database; no QLever."""
    monkeypatch.setenv("LAE_DATABASE_URL", db_url)
    monkeypatch.delenv("LAE_QLEVER_URL", raising=False)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, Any], str]:
    code = main(["vocab", *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if code == 0 else {}, captured.err


def test_import_export_round_trip(
    lae_env: None, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, summary, _ = run(capsys, "import")
    assert code == 0 and set(summary["created"]) == SEED_SETS
    code, summary, _ = run(capsys, "export", str(tmp_path / "out"))
    assert code == 0 and summary["files"] == len(SEED_SETS)
    assert same_tree(VOCAB_DIR, tmp_path / "out")

    code, summary, _ = run(capsys, "refresh-membership", "vocab:gender", "vocab:measurement_units")
    assert code == 0 and summary["qlever"] is False
    assert summary["members"] == {"vocab:gender": 2, "vocab:measurement_units": 22}
    code, summary, _ = run(capsys, "harvest-metatypes")
    assert code == 0 and isinstance(summary["rows_changed"], int)


def test_failures_exit_1(lae_env: None, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("value_sets: []\n")
    code, _, err = run(capsys, "import", "--dir", str(tmp_path))
    assert code == 1 and "bad.yaml: $.value_sets" in err
    code, _, err = run(capsys, "refresh-usage")
    assert code == 1 and "LAE_QLEVER_URL" in err
    code, _, err = run(capsys, "refresh-membership", "vocab:nowhere")
    assert code == 1 and "vocab:nowhere" in err
