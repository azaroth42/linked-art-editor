"""EDTF syntax (``lae.validation.edtf``).

Two sources: the examples of the EDTF specification, by level, and the verdict of edtf.js (the
parser the editor uses) on some thousands of strings, which this module must reproduce exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lae.validation.edtf import is_edtf

LEVEL_0 = [
    "1985-04-12",
    "1985-04",
    "1985",
    "1985-04-12T23:20:30",
    "1985-04-12T23:20:30Z",
    "1985-04-12T23:20:30-04",
    "1985-04-12T23:20:30+04:30",
    "1964/2008",
    "2004-06/2006-08",
    "2004-02-01/2005-02-08",
    "2004-02-01/2005-02",
    "2004-02-01/2005",
    "2005/2006-02",
]
LEVEL_1 = [
    "Y170000002",
    "Y-170000002",
    "2001-21",
    "1984?",
    "2004-06~",
    "2004-06-11%",
    "201X",
    "20XX",
    "2004-XX",
    "1985-04-XX",
    "1985-XX-XX",
    "1985-04-12/..",
    "1985-04/..",
    "../1985-04-12",
    "../1985",
    "1985-04-12/",
    "/1985-04-12",
    "-1985",
    "1984?/2004-06~",
    "1880~/1895?",
]
LEVEL_2 = [
    "Y-17E7",
    "1950S2",
    "Y171010000S3",
    "Y3388E2S3",
    "2001-34",
    "[1667,1668,1670..1672]",
    "[..1760-12-03]",
    "[1760-12..]",
    "[1760-01,1760-02,1760-12..]",
    "[1667,1760-12]",
    "[..1984]",
    "{1667,1668,1670..1672}",
    "{1960,1961-12}",
    "{..1984}",
    "2004-06~-11",
    "2004?-06-11",
    "?2004-06-~11",
    "2004-%06-11",
    "156X-12-25",
    "15XX-12-25",
    "XXXX-12-XX",
    "1XXX-XX",
    "1XXX-12",
    "1984-1X",
    "2004-06-~01/2004-06-~20",
    "2004-06-XX/2004-07-03",
]
INVALID = [
    "",
    " 1985",
    "1985 ",
    "ca. 1642",
    "1642-43",
    "1503-1506",
    "between 1915 and 1916",
    "1820 Mar 09",
    "2011-02-03 - 2011-05-01",
    "1985-4",
    "19850412",
    "1985-13",
    "1985-00",
    "1985-04-31",
    "2004-02-30",
    "2005-02-30",
    "1985-21-01",
    "1985-42",
    "1985-04-12T25:00:00",
    "1985-04T10:00:00",
    "1985/1986/1987",
    "//",
    "[]",
    "[1667,]",
    "[1667",
    "{1667]",
    "Y1985",
    "1880~/1895??",
    "1985-2X",
    "1985-04-4X",
    "-0000",
    "1985-04-12T23:20:30-00:00",
    "1985-04-12T23:20:30+14:01",
    "\u0661\u0669\u0668\u0665",
    "\uff11\uff19\uff18\uff15",
    "1985\n",
]
EDTF_JS_ONLY = [
    # Accepted by edtf.js beyond the specification's examples (module docstring).
    "19",
    "198",
    "198~",
    "2005-02-29",
    "1985-04-12T23:20",
    "1985-04-12T23:20:30.5Z",
    "1985-04-12T24:00",
    "1985-04-12T23:20:30\u221204",
    "[1667, 1668]",
    "/",
    "../..",
    "1985/1984",
]
NOT_IN_EDTF_JS = [
    # Rejected by edtf.js at its default level 2, although the specification can be read to
    # allow them.
    "2001-21~",
    "2001-21/2001-22",
    "201X?",
    "1984?/2004-~06",
    "[1670..1672-05]",
    "[Y170000002]",
    "12345",
]
EDTF_JS_CASES = json.loads(
    (Path(__file__).parent / "data" / "edtf_js_cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("value", LEVEL_0 + LEVEL_1 + LEVEL_2 + EDTF_JS_ONLY)
def test_valid(value: str) -> None:
    assert is_edtf(value)


@pytest.mark.parametrize("value", INVALID + NOT_IN_EDTF_JS)
def test_invalid(value: str) -> None:
    assert not is_edtf(value)


def test_agrees_with_edtf_js() -> None:
    """Every string edtf.js accepts is accepted, and every one it rejects is rejected."""
    accepted, rejected = EDTF_JS_CASES["accepted"], EDTF_JS_CASES["rejected"]
    assert len(accepted) > 1000
    assert len(rejected) > 1000
    assert [value for value in accepted if not is_edtf(value)] == []
    assert [value for value in rejected if is_edtf(value)] == []


def test_non_strings_are_not_edtf() -> None:
    assert not is_edtf(None)
    assert not is_edtf(1985)
