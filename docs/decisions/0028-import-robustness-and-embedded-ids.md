# ADR-0028: Import robustness in RDF, and schema validation ignores the ids of embedded nodes

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (item 3) and the orchestrator (items 1 and 2, reversible), `docs/questions.md`, "Owner decisions, 2026-11-22 (import and validation)", items 3 and 4 · **Recorded by:** Claude Fable 5.2 · **Amends:** ADR-0021 (nested `@context`), ADR-0025 (the `uri` format, the `LAE-RDF-*` codes)

## Context
Three kinds of real data could be neither loaded nor published, although nothing in them is ambiguous.

1. **A nested `@context` that repeats the pinned one.** Seven of the 32 LUX records sampled for the corpus (PR #130) carry `"@context": "https://linked.art/ns/v1/linked-art.json"` on each `DigitalObject` under `representation`, and one more has it on two references under `about`: 11 nested contexts in all. `lae.rdf` refused every nested `@context`, because one could rename `@id` or a property (ADR-0021).
2. **IRIs with characters an IRI may not contain.** The same seven records point at Wikimedia Commons files whose names contain spaces (`.../Special:Filepath/Rembrandt van Rijn - Self-Portrait - Google Art Project.jpg`, 9 such ids). `expand_to_ntriples` and `to_turtle` raised `InvalidIRIError`, and the JSON Schema `uri` format reported the same value as a violation.
3. **Embedded nodes with an id of their own.** ADR-0021 makes such a node part of the record and keeps its `id` in `records.data`. The vendored Linked Art JSON Schema allows no `id` on a `Name`, `Identifier`, `TimeSpan` or `Dimension`, so a record that ADR-0021 indexes correctly failed the first gate (`docs/questions.md`, Step 1.9, item 2).

## Decision

### 1. A nested `@context` equal to the record's pinned context is ignored
In `lae.rdf`, a node below the root may carry `"@context"` whose value is the pinned context URL of the record's context version, as a string or as the one-element array of that string. It is dropped before expansion. Any other nested `@context` (another URL, an inline object, an array with anything else in it, an empty array, `null`) raises the same `ExpansionError` as before. `expand_to_ntriples` and `to_turtle` apply the rule through the one function they share (`rdf_triples`); before this, `to_turtle` let pyld process a nested inline context, and now refuses it.

`strip_stub_attributes(jsonld, record_uri, *, context_version=None)` takes the pinned URL from `context_version`, or without one from the root's own `@context`.

### 2. In RDF only, an `id` is percent-encoded where it has to be
`lae.rdf.repair_iri(value)` returns a writable IRI unchanged. Otherwise it replaces each character that `lae.rdf` refuses in an IRI with the percent-encoding of its UTF-8 bytes in upper-case hex, and returns the result if that is now a writable absolute IRI, else `None`. The characters are: U+0000 to U+0020, `<`, `>`, `"`, `{`, `}`, `|`, `\`, `^`, the backtick, and every other character Python's `\s` matches (U+0085, U+00A0, U+2028, …; pyld drops a triple whose IRI has one). Nothing else is touched:

- An existing `%` is never encoded, so the function is idempotent, and a correctly encoded IRI is never changed.
- Nothing is trimmed or deleted. A leading space leaves no scheme, so that id is not repairable. (`lae.rdf.sanitize.clean_iri`, vendored from LUX, trims and deletes line breaks; it is not used here, so that the repair is exactly "encode these characters".)
- An id that is relative, has no scheme, or is a bare `https:/` is not repairable and raises `InvalidIRIError` as before.

The repair applies to node identifiers: every `id` / `@id` in the document that expansion writes, and the original id in the `la:equivalent` triple of ADR-0021. The derived node IRI is still computed from the id as stored. Not repaired, and still an error: the record URI itself, a class (`type`) or property IRI, and an IRI given as a bare string where an object is expected.

The stored JSON-LD is not changed, and neither are the API bodies. `EXPANDER_VERSION` stays 1: no input that expanded before gives different triples now; inputs that raised now expand.

**Validation.** The schema's `uri` format accepts what `repair_iri` accepts. For each `id` that expansion writes in encoded form, `validate` adds the warning **`LAE-RDF-006`** at the JSON Pointer of that `id`, with `params.original` and `params.encoded` (`lae.validation.expansion.iri_repair_warnings`, from `lae.rdf.repaired_iris`). Like every warning it needs acknowledgement before publishing (C2). It is emitted with the rules and SHACL, so not for a record that fails the schema. An id inside a reference is not reported, because expansion drops what is inside a reference. An id that cannot be repaired is still `LAE-SCHEMA-format`.

In the SHACL data graph the `rdf:type` of a reference is attached to the encoded IRI, while `entity_labels` is asked for the id as stored.

### 3. JSON Schema validation ignores the `id` of an embedded node
`lae.rdf.strip_embedded_ids(jsonld, record_uri)` returns a copy in which every object below the root that has a foreign `id` and that ADR-0021's rule (`_is_embedded`, the object alone) classifies as an embedded node has lost its `id`. References, the root, and nodes with internal node IRIs keep theirs (the latter are stripped by `strip_internal_iris`, as before). `validate_schema` applies both before the schema runs. So do `lae bootstrap load` (`lae.bootstrap.records`), the corpus schema test and `scripts/report_schema_mismatches.py`, so that one definition of "passes the schema" holds everywhere.

Each object is judged by itself, wherever it is, also inside a reference. Paths in diagnostics are unchanged, because only a key is removed. Storage, RDF, the rules and SHACL still see the ids.

## Alternatives considered
- **Reject such records** (the state before): leaves real institutional data out for reasons a curator cannot act on.
- **Repair the stored JSON** (percent-encode on import): loses what the source said, and breaks the lossless round trip that is a hard gate.
- **Encode with `urllib.parse.quote` or by RFC 3987 component:** would also re-encode characters that are legal today (`%`, non-ASCII, `[`), changing triples of records that already expand, and needs a decision about what each scheme allows where.
- **Accept any nested `@context` the pinned context could have produced** (for example the same context inline): not decidable without interpreting it; only identity is safe.
- **Change the schema upstream to allow `id` on these classes:** still worth doing (`docs/upstream/linked-art-schema-mismatches.md` lists the three examples), but not something the editor can wait for.

## Consequences
- **LUX sample (32 records, PR #130):** all 32 expand (7 948 triples, every subject owned, no blank nodes) and serialize as Turtle; before, 7 raised. As fetched, all 32 fail the schema, because each has LUX's `_links`. Without LUX's own `_`-prefixed keys 28 are publishable (8 `LAE-RDF-006` warnings among them) and 4 have other schema violations. The BCE-dated tetradrachm validates: the pipeline compares dates in Python, and the Spike S4 test that crashed on it in pyshacl was replaced by Step 1.9.
- **Golden corpus:** the number of known schema failures does not change (60 of 244). The three files with an id on a `Name` each fail for another reason as well (no top-level `_label`; a `Name` without `content`, twice). `object-3.json` goes from two errors to one.
- **Not injective:** `a b` and `a%20b` are the same IRI in RDF. Two references that differ only in this way become one node in QLever, which is what they denote.
- **A nested pinned `@context` is ignored by RDF only.** Where the JSON Schema does not allow `@context` on a node (a `Name`, a reference under `about`), the schema still reports it; under "load and flag" (ADR-0030) such a record loads with a QA finding.
- **Bulk load:** `lae.bootstrap.sanitize.clean_ids` still rewrites ids in the stored document (and trims them) before validating. The bootstrap rework (ADR-0030) should stop doing that and rely on this ADR, so that load and publish treat an id the same way.
- **Pre-existing, not changed:** `to_turtle` raises `ValueError` (from pyoxigraph) for an IRI with a malformed percent escape such as `100%wool`, which N-Triples expansion writes as it is.
- Tests on the LUX records skip until `tests/corpus/lux/` is on main (PR #130), and then assert the numbers above.
