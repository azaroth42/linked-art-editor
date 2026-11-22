"""Pinned Linked Art JSON-LD contexts and the offline pyld document loader.

Every ``@context`` the editor expands against is a file under ``profiles/context/``
(``v1.json`` is ``linked.art/docs/ns/v1/linked-art.json``, copied by ``scripts/sync_corpus.py``).
The pinned v1 context is self-contained: it has no ``@import`` and no remote scoped contexts, so
nothing else needs vendoring. ``document_loader`` serves only those files, keyed by the public URL
records use, and raises for any other URL, so JSON-LD processing never touches the network
(conventions §4). Spike S4 (Phase 0, Step 0.9) introduced it; Phase 1 Step 1.3 builds the RDF
layer on it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

CONTEXT_DIR: Final = Path(__file__).resolve().parents[4] / "profiles" / "context"

#: Public context URL -> pinned context version (``profiles/context/v{version}.json``).
CONTEXT_URLS: Final[Mapping[str, int]] = MappingProxyType(
    {"https://linked.art/ns/v1/linked-art.json": 1}
)

CONTENT_TYPE: Final = "application/ld+json"


class UnpinnedContextError(LookupError):
    """A JSON-LD document asked for a context URL that is not pinned under ``profiles/context``."""


def context_path(version: int) -> Path:
    """Return the path of the pinned context file for ``version``."""
    return CONTEXT_DIR / f"v{version}.json"


@cache
def _context_bytes(version: int) -> bytes:
    """Read and cache the raw bytes of one pinned context file."""
    path = context_path(version)
    if not path.is_file():
        raise UnpinnedContextError(f"no pinned context version {version} at {path}")
    return path.read_bytes()


def load_context(version: int) -> dict[str, Any]:
    """Return a fresh parsed copy of ``profiles/context/v{version}.json``."""
    data = json.loads(_context_bytes(version))
    if not isinstance(data, dict) or "@context" not in data:
        raise ValueError(f"{context_path(version)} is not a JSON-LD context document")
    return data


def document_loader(url: str, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """pyld document loader that serves only pinned contexts and raises for any other URL."""
    version = CONTEXT_URLS.get(url)
    if version is None:
        raise UnpinnedContextError(
            f"refusing to load {url!r}: only pinned contexts under profiles/context are served"
        )
    return {
        "contentType": CONTENT_TYPE,
        "contextUrl": None,
        "documentUrl": url,
        "document": load_context(version),
    }
