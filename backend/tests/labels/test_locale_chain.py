"""Tests for ``lae.api.deps.locale_chain`` and ``parse_accept_language``.

They live beside the label tests because the locale chain exists for transclusion and the
Step 1.5 Verify command runs ``tests/labels``.
"""

from __future__ import annotations

from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI

from lae.api.deps import locale_chain, parse_accept_language


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, []),
        ("", []),
        ("fr", ["fr"]),
        ("fr-CH, fr;q=0.9, en;q=0.8, de;q=0.7, *;q=0.5", ["fr-CH", "fr", "en", "de"]),
        ("de;q=0.5, nl, en-GB;q=0.8", ["nl", "en-GB", "de"]),
        ("da, en-gb;q=0.8, en;q=0.7", ["da", "en-GB", "en"]),
        # Equal weights keep header order.
        ("it;q=0.5, es;q=0.5, pt;q=0.5", ["it", "es", "pt"]),
        # q=0 means "not acceptable".
        ("fr, de;q=0, nl;q=0.000", ["fr"]),
        ("en;q=1, fr;q=1.000, nl;q=0.001", ["en", "fr", "nl"]),
        # Whitespace and parameter case are tolerated.
        ("  ja ;  Q=0.4 ,ko", ["ko", "ja"]),
        # Repeats (after case normalization) keep their best position.
        ("en-us, EN-US;q=0.1, fr", ["en-US", "fr"]),
        # Three-letter ISO 639-2 codes become their two-letter BCP-47 form.
        ("ger, fre;q=0.5", ["de", "fr"]),
    ],
)
def test_parse_accept_language(header: str | None, expected: list[str]) -> None:
    assert parse_accept_language(header) == expected


@pytest.mark.parametrize(
    "header",
    [
        "*",
        "en_US",
        "fr;q=2",
        "fr;q=1.5",
        "fr;q=0.1234",
        "fr;q=-1",
        "fr;q=",
        "fr;q=abc",
        ";q=0.5",
        ",,,",
        "english please",
    ],
)
def test_malformed_entries_are_dropped(header: str) -> None:
    assert parse_accept_language(header) == []
    assert parse_accept_language(f"{header}, nl;q=0.5") == ["nl"]


async def _chain(headers: dict[str, str]) -> list[str]:
    app = FastAPI()

    @app.get("/chain")
    async def chain(locales: Annotated[list[str], Depends(locale_chain)]) -> list[str]:
        return locales

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.get("/chain", headers=headers)
    assert response.status_code == 200
    result: list[str] = response.json()
    return result


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("fr-CA, fr;q=0.9", ["fr-CA", "fr", "en"]),
        ("en-US", ["en-US", "en"]),
        ("nl, en;q=0.5", ["nl", "en"]),
        # en is not last: the chain still ends with the en fallback.
        ("en, fr;q=0.5", ["en", "fr", "en"]),
        ("*", ["en"]),
        ("de;q=0", ["en"]),
    ],
)
async def test_locale_chain_dependency_ends_with_en(header: str, expected: list[str]) -> None:
    assert await _chain({"Accept-Language": header}) == expected


async def test_locale_chain_without_header_is_en() -> None:
    assert await _chain({}) == ["en"]
