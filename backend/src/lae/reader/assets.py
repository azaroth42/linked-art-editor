"""The Public Reader's script, served by ``api`` under ``/reader/assets/``.

The reader page loads no SPA code (decision D8). When a record has a IIIF resource it loads one
small module, ``reader.js``, built from ``frontend/src/reader/`` by ``corepack pnpm build:reader``
into ``frontend/dist-reader/`` (``LAE_READER_ASSETS_DIR`` overrides the directory); that module
imports the Canvas Panel chunk only then (Spike S5, section 8). ``api`` serves the directory
itself, so the page works wherever ``/id/`` is served. Without a build the page still renders;
only the viewer is missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import anyio
from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from lae.settings import Settings

ASSETS_PATH: Final = "/reader/assets"
"""URL path the built reader script is served under."""

SCRIPT_SRC: Final = f"{ASSETS_PATH}/reader.js"
"""URL of the reader script's entry module."""

DEFAULT_ASSETS_DIR: Final = Path(__file__).resolve().parents[4] / "frontend" / "dist-reader"
"""Where ``corepack pnpm build:reader`` writes the script."""


def assets_dir(settings: Settings) -> Path:
    """The directory the reader script is served from."""
    return settings.reader_assets_dir or DEFAULT_ASSETS_DIR


class OptionalStaticFiles(StaticFiles):
    """``StaticFiles`` that answers 404, rather than failing, while its directory is missing."""

    async def check_config(self) -> None:
        """Check the directory only once it exists (Starlette raises on a missing one)."""
        if self.directory is not None and await anyio.Path(self.directory).is_dir():
            await super().check_config()


def mount_assets(app: FastAPI, settings: Settings) -> None:
    """Serve the built reader script under ``ASSETS_PATH`` (404s until it is built)."""
    app.mount(
        ASSETS_PATH,
        OptionalStaticFiles(directory=assets_dir(settings), check_dir=False),
        name="reader-assets",
    )
