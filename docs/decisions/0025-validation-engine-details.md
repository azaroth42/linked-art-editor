# ADR-0025: Validation engine details the guide left open

**Status:** Proposed · **Date:** 2026-11-22 · **Decided by:** Claude Fable 5.2 as implementer of Phase 1 Step 1.9 (the critical-step reviewer and Rob Sanderson may override) · **Recorded by:** Claude

## Context
Step 1.9 fixes the pipeline (JSON Schema, then Python rules and SHACL), the diagnostic shape `{code, path, severity, params, message_en}` and the code families. Implementing it needed eight choices the guide and spec §5 do not make. Each is small; together they define what Step 1.10 (publish), Step 1.19 (validation drawer) and Phase 3 (agents) see.

## Decision
1. **Expansion failures have codes of their own, area `RDF`.** `LAE-RDF-001` (`BlankNodeError`), `-002` (`OwnershipError`), `-003` (`InvalidIRIError`), `-004` (`InvalidLiteralError`), `-005` (any other `ExpansionError`). All are violations at path `""`, with the exception text in `params.detail`, because the exception does not say where in the document the term is. `detail` is developer text and may quote an internal node IRI; the endpoints that return it are authenticated editor endpoints, not public serializations.
2. **`LAE-RULE-005` is added for an invalid `TimeSpan` bound**; `LAE-RULE-001` stays the ordering rule, as the guide words it. The retired shapes `LAE-SHACL-009` (validity) and `-010` (order) so map to two rule codes with two messages, rather than one code with a `reason` parameter. In practice the schema's `format: date-time` check reports an invalid bound first (`LAE-SCHEMA-format`).
3. **One meaning of "valid date-time" everywhere.** The JSON Schema format check, the rules and RDF expansion accept the lexical form of `lae.rdf.sanitize.VALID_DATETIME` (signed years, optional time zone). The schema check and the rules also require a real date and time. Years are astronomical in the proleptic Gregorian calendar (XSD 1.1: `0000` is 1 BCE); a value without a zone is compared as UTC.
4. **`LAE-RULE-002` (EDTF) checks only strings that claim to be EDTF**: a `Name` or `Identifier` in a `TimeSpan`'s `identified_by` that is `classified_as` `http://id.loc.gov/datatypes/edtf`. It is a warning. A `TimeSpan`'s other names are display labels in free text, and some in the corpus ("1503-1506", "2011-02-03 - 2011-05-01") look like EDTF and are not, so a heuristic would warn on good data. The syntax check is `lae.validation.edtf` (levels 0 to 2), written here because the Python EDTF packages pull in unrelated dependencies.
5. **A referenced entity's class comes from `entity_labels.type`, else from the `type` the reference declares.** Without the fallback every reference to an entity that has no row yet (imports, the CLI, the golden corpus) would fail every `sh:class` constraint. `entity_labels` wins when both exist.
6. **SHACL results about a node the record does not own are dropped.** Of a referenced entity the data graph holds only its class, so any conclusion a shape draws about it is about missing data.
7. **Internal node IRIs do not appear in diagnostics.** A node is addressed by its JSON Pointer; `params.value` is given only for literals and references. A path may name a property that is absent when the absence is the finding (`/dimension/0/unit`).
8. **`message_en` is rendered from `frontend/public/locales/en/errors.json`**, the one place English messages live (decision C3), with `{name}` placeholders filled from `params`. Without an entry it falls back to the validator's own text.

Also: JSON Schema `format` is asserted for `date-time` and `uri` only, `uri` being exactly what expansion accepts, so a malformed `id` is reported with its path by the schema instead of at the root by `LAE-RDF-003`. `anyOf` / `oneOf` diagnostics carry `params.cause`, the first error in the alternative whose `type` the instance has.

## Consequences
- Step 1.10 can treat every refusal by `expand_to_ntriples` as a violation from `validate`, and key accepted warnings by `code` + `path`: both are stable across runs.
- Step 1.18's `EdtfWidget` must classify the EDTF string it stores with the IRI in item 4 if it wants it checked (`docs/questions.md`, Step 1.9).
- A record whose embedded nodes carry ids of their own (ADR-0021) is located correctly by SHACL diagnostics, but the vendored JSON Schema rejects `id` on an embedded `Name` (`object-3.json`), so such a record does not reach SHACL at all (`docs/questions.md`, Step 1.9).
- New shapes need an `errors.json` entry; a test fails otherwise.
