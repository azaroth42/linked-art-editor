"""Rendering the Public Reader page with Jinja2, and the page's cache validator.

``render_page`` fills ``templates/record.html`` (autoescaped, undefined names are errors) with a
``RecordPage`` and the catalogue's ``t``. The template holds markup only: every word on the page
comes from the reader catalogue or from the record.

``fingerprint`` is a hash of everything besides the record that the HTML depends on (this
package's code and template, and every reader catalogue). It is part of the HTML ``ETag``
(``lae.api.public``), so a deployment that changes the page invalidates cached copies.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Final

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from lae.reader.i18n import LOCALES_DIR, NAMESPACE, Catalogue, available_languages, load_messages
from lae.reader.view import RecordPage

PACKAGE_DIR: Final = Path(__file__).resolve().parent
TEMPLATES_DIR: Final = PACKAGE_DIR / "templates"
TEMPLATE: Final = "record.html"

VIEWER_HEIGHT: Final = 480
"""Height of the Canvas Panel element in CSS pixels; Canvas Panel needs it as an attribute."""


@lru_cache(maxsize=1)
def environment() -> Environment:
    """The Jinja2 environment of the reader templates."""
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(default=True, default_for_string=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def render_page(page: RecordPage, catalogue: Catalogue) -> str:
    """Return the HTML of the Public Reader page."""
    template = environment().get_template(TEMPLATE)
    return template.render(page=page, t=catalogue.t, viewer_height=VIEWER_HEIGHT)


@lru_cache(maxsize=1)
def fingerprint() -> str:
    """A short hash of the reader's code, template and catalogues (see the module docstring)."""
    digest = hashlib.blake2b(digest_size=8)
    files = sorted(PACKAGE_DIR.rglob("*.py")) + sorted(TEMPLATES_DIR.rglob("*.html"))
    files += sorted(LOCALES_DIR.glob(f"*/{NAMESPACE}.json"))
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def warm_up() -> None:
    """Load the template and every catalogue now (``lae api`` startup), so that a missing or
    broken file stops the start instead of failing the first page (``CatalogueError``)."""
    environment().get_template(TEMPLATE)
    for language in available_languages():
        load_messages(language)
    fingerprint()
