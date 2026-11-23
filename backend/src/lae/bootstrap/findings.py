"""QA findings for records a bulk load keeps although they fail JSON Schema (ADR-0030).

"Load and flag": a record that does not validate is loaded as long as it can be written to RDF,
and what is wrong with it goes to ``qa_findings`` (the table of Phase 3 Step 3.10). One row per
record and diagnostic code:

- ``code``: the validation code, ``LAE-SCHEMA-<keyword>`` (``lae.validation.schema``), which is
  also its message key in ``errors.json``;
- ``severity``: ``error``; ``status``: ``open``; ``node_id``: none (the finding is about the
  record; the places are in ``params``);
- ``params``: ``{"source": "bootstrap", "count": n, "diagnostics": [{"path", "params"}, ...]}``
  with the JSON Pointer and parameters of the first ``MAX_DIAGNOSTICS`` occurrences, in
  document order. No message text: clients render the code (conventions §4).
- ``sweep_run_id``: the id of the load run.

Such a record cannot be published again until it validates: publish runs the same schema check.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

import orjson

from lae.validation.diagnostics import Diagnostic

FINDING_SOURCE: Final = "bootstrap"
MAX_DIAGNOSTICS: Final = 10
"""Occurrences of one code kept in a finding's ``params``; ``count`` has the real number."""


def schema_findings(diagnostics: Iterable[Diagnostic]) -> list[tuple[str, str]]:
    """Group a record's schema violations by code: ``[(code, params as JSON text)]``, in order."""
    grouped: dict[str, list[Diagnostic]] = {}
    for item in diagnostics:
        grouped.setdefault(item.code, []).append(item)
    findings: list[tuple[str, str]] = []
    for code, items in grouped.items():
        params: dict[str, Any] = {
            "source": FINDING_SOURCE,
            "count": len(items),
            "diagnostics": [
                {"path": item.path, "params": item.params} for item in items[:MAX_DIAGNOSTICS]
            ],
        }
        findings.append((code, orjson.dumps(params, default=str).decode("utf-8")))
    return findings
