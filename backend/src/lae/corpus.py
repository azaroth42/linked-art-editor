"""The golden corpus: Linked Art records under ``tests/corpus/`` (Phase 0, Step 0.4).

``linkedart/`` holds every linked.art documentation example, ``institutional/`` holds real
records from the Yale units that publish Linked Art for LUX, and ``lux/`` holds merged records
from LUX itself; ``tests/corpus/SOURCE.md`` records where each came from. The corpus is the
oracle for round-trip and projection tests, so files are read exactly as stored.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

CORPUS_DIR: Final = Path(__file__).resolve().parents[3] / "tests" / "corpus"
SUBSETS: Final = ("linkedart", "institutional", "lux")


def corpus_files(root: Path = CORPUS_DIR) -> list[Path]:
    """Return every corpus JSON file in the known subsets, sorted by path."""
    return sorted(path for subset in SUBSETS for path in (root / subset).glob("*.json"))


def load_record(path: Path) -> dict[str, Any]:
    """Parse one corpus file; raise ``ValueError`` if it is not a JSON object."""
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def iter_corpus(root: Path = CORPUS_DIR) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield ``(path, record)`` for every corpus file, in a stable order."""
    for path in corpus_files(root):
        yield path, load_record(path)
