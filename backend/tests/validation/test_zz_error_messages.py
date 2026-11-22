"""Every diagnostic code has an English message in ``errors.json`` (Step 1.9, item 8).

Two checks: every code validation *can* emit (``all_codes()``, plus the API's own), and every
code the tests of this directory *did* emit, recorded by ``conftest.record_emitted_codes``. The
file name sorts last so that the second check sees the whole directory's run.
"""

from __future__ import annotations

import json
import re

from lae.api.validation import VALIDATION_API_CODES
from lae.validation import all_codes, validate_document
from lae.validation.diagnostics import ERRORS_JSON, diagnostic, message_en
from tests.validation.conftest import EMITTED_CODES, base

MESSAGES: dict[str, str] = json.loads(ERRORS_JSON.read_text(encoding="utf-8"))
PLACEHOLDER = re.compile(r"\{([^}]*)\}")


def test_every_code_validation_can_emit_has_a_message() -> None:
    codes = all_codes() | set(VALIDATION_API_CODES)
    assert len(codes) > 50
    assert sorted(code for code in codes if not MESSAGES.get(code)) == []


def test_every_code_emitted_by_the_tests_has_a_message() -> None:
    validate_document(base())  # so that the check is not vacuous when this file runs alone
    assert validate_document({"type": "Nonsense"}).items[0].code in EMITTED_CODES
    assert sorted(code for code in EMITTED_CODES if not MESSAGES.get(code)) == []


def test_a_code_without_a_message_is_noticed() -> None:
    item = diagnostic("LAE-TEST-999", "", "info", fallback="validator text")
    assert item.message_en == "validator text"
    assert "LAE-TEST-999" in EMITTED_CODES
    EMITTED_CODES.discard("LAE-TEST-999")
    assert message_en("LAE-TEST-999", {}) == "LAE-TEST-999"


def test_messages_use_simple_placeholders() -> None:
    for code, text in MESSAGES.items():
        for name in PLACEHOLDER.findall(text):
            assert re.fullmatch(r"[a-z_]+", name), f"{code}: placeholder {{{name}}}"


def test_no_message_is_left_for_a_code_that_does_not_exist() -> None:
    known = all_codes() | set(VALIDATION_API_CODES)
    stale = [
        code
        for code in MESSAGES
        if code.startswith(("LAE-SCHEMA-", "LAE-SHACL-", "LAE-RULE-", "LAE-RDF-", "LAE-VALID-"))
        and code not in known
    ]
    assert stale == []
