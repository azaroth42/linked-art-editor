"""The reader template is markup only: no user-facing literal text (conventions §5)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from lae.reader.render import TEMPLATES_DIR

TEMPLATE = (TEMPLATES_DIR / "record.html").read_text()
ATTRIBUTE_MACRO = re.compile(r"\{%-? macro attrs\(.*?endmacro -?%\}", re.DOTALL)
"""The macro that writes ``lang``/``dir`` attributes: markup, not text."""
JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
ANNOUNCED = {"alt", "title", "aria-label", "aria-description", "placeholder", "value"}


class _Texts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.raw = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script"):
            self.raw += 1
        self.texts += [value or "" for name, value in attrs if name in ANNOUNCED]

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script"):
            self.raw -= 1

    def handle_data(self, data: str) -> None:
        if not self.raw:
            self.texts.append(data)


def test_template_has_no_literal_text() -> None:
    parser = _Texts()
    assert ATTRIBUTE_MACRO.search(TEMPLATE)
    parser.feed(JINJA.sub("", ATTRIBUTE_MACRO.sub("", TEMPLATE)))
    literal = [text.strip() for text in parser.texts if re.search(r"[^\W\d_]", text)]
    assert literal == []


def test_template_loads_no_stylesheet_and_one_optional_script() -> None:
    assert '<link rel="stylesheet"' not in TEMPLATE
    scripts = re.findall(r"<script\b[^>]*>", TEMPLATE)
    assert scripts == ['<script type="module" src="{{ page.script_src }}">']
    assert "{% if page.script_src %}" in TEMPLATE
