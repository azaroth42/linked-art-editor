# profiles/shapes/

SHACL shapes in Turtle, one file per Linked Art record class. Linked Art publishes no SHACL; these
are written for this project (decision C1, ADR-0008). JSON Schema runs first; a record reaches
SHACL only when it passes the schema. Phase 1 Step 1.9 loads every `*.ttl` here into one shapes
graph at startup and runs it with pyshacl over one record's data graph: its expansion plus one
`rdf:type` triple per referenced entity.

| File | Class | Since |
| :-- | :-- | :-- |
| `HumanMadeObject.ttl` | `HumanMadeObject` (`crm:E22_Human-Made_Object`) | Spike S4 (Phase 0, Step 0.9) |

## Namespaces

| Prefix | IRI | Used for |
| :-- | :-- | :-- |
| `lae:` | `urn:lae:ns:` | The project's annotation vocabulary. Only `lae:code` so far. |
| `laes:` | `urn:lae:shape:` | Shape IRIs, `laes:<Class>-<rule>` (e.g. `laes:HumanMadeObject-made_of`), and the resource that carries the SHACL-SPARQL prefix declarations (`laes:` itself). |

Both are URNs on purpose: the deployment `base_uri` differs between installations, the IRIs are
never dereferenced, and the `linked.art` namespace belongs to Linked Art. Declare both prefixes in
every file.

### `lae:code`

`lae:code` is an `owl:AnnotationProperty` whose value is the diagnostic code reported for every
validation result of the shape it annotates, for example `lae:code "LAE-SHACL-006"`. Step 1.9 reads
it from each result's `sh:sourceShape` and returns it as `Diagnostic.code`; the same string is the
i18n key under the `errors` namespace (conventions §2).

## Rules for shape authors

1. **Every shape that has a target** (`sh:targetClass`, `sh:targetNode`, `sh:targetObjectsOf`,
   `sh:targetSubjectsOf`) carries exactly one `lae:code` of the form `LAE-SHACL-NNN`. Codes are
   unique across all files and never reused after a shape is removed.
2. A result's `sh:sourceShape` is the targeted shape for its own constraints, for `sh:or`,
   `sh:node`, `sh:qualifiedValueShape`, and `sh:sparql`. Shapes without a target that are only
   referenced from those (the anonymous members of an `sh:or` list, for example) never appear as a
   source shape and need no code. Do not nest `sh:property` inside a targeted node shape: the
   nested property shape becomes the source shape. Make it a targeted `sh:PropertyShape` instead.
3. Severity: `sh:Violation` (the default) blocks publishing; `sh:Warning` is publishable after
   acknowledgement; `sh:Info` is guidance (spec §5).
4. Give every shape (or its `sh:sparql` constraint) an English `sh:message` with an `@en` tag.
5. Use SHACL Core and SHACL-SPARQL only, so pyshacl runs with `advanced=False`.
6. Remember that referenced entities are in the data graph with their class. A "must have"
   constraint on a class must exempt references, as `LAE-SHACL-001` does.
7. Performance (measured in `docs/spikes/S4-shacl.md`): express cross-node conditions in
   SHACL-SPARQL, not as `sh:or` over `sh:closed` or qualified shapes. pyshacl builds and discards
   a full result for every failing `sh:or` member, which made one such shape cost 50 ms on a
   1,700-triple record, against 0.6 ms for the SPARQL form on a pyoxigraph data graph.
8. Avoid `xsd:dateTime` checks in SHACL. pyshacl 0.40.2 raises `TypeError` from
   `sh:lessThanOrEquals`, aborting the whole validation, when a bound is not a datetime rdflib can
   parse (an ill-typed value, or any BCE year), and `sh:datatype xsd:dateTime` rejects valid BCE
   values. `LAE-SHACL-009` and `LAE-SHACL-010` exist because the Step 0.9 guide asked S4 to measure
   TimeSpan ordering; the S4 report recommends that Step 1.9 replace them with its Python rule
   `LAE-RULE-001`.

## Codes

| Code | Shape | Severity | Rule (Linked Art model docs) |
| :-- | :-- | :-- | :-- |
| `LAE-SHACL-001` | `laes:HumanMadeObject-name` | Warning | An object has at least one `Name` in `identified_by` (references exempt). |
| `LAE-SHACL-002` | `laes:HumanMadeObject-primary-name` | Warning | If it has Names, one is classified as Primary Name (`aat:300404670`). |
| `LAE-SHACL-003` | `laes:HumanMadeObject-primary-name-per-language` | Warning | At most one Primary Name per language (or without a language). |
| `LAE-SHACL-004` | `laes:HumanMadeObject-produced_by` | Violation | `produced_by` is at most one `Production`. |
| `LAE-SHACL-005` | `laes:HumanMadeObject-production-actor` | Violation | The Production and its `part`s are `carried_out_by` a `Person` or `Group`. |
| `LAE-SHACL-006` | `laes:HumanMadeObject-made_of` | Violation | `made_of` refers to a `Material`. |
| `LAE-SHACL-007` | `laes:Dimension-value` | Violation | A `Dimension` has exactly one numeric `value`. |
| `LAE-SHACL-008` | `laes:Dimension-unit` | Warning | A `Dimension` has one `unit` that is a `MeasurementUnit`. |
| `LAE-SHACL-009` | `laes:TimeSpan-datetime` | Violation | `TimeSpan` bounds are `xsd:dateTime`. |
| `LAE-SHACL-010` | `laes:TimeSpan-order` | Violation | `begin_of_the_begin` is not later than `end_of_the_end`. |

## Tests

`backend/tests/validation/test_hmo_shapes.py` checks the `lae:code` rules above, runs one broken
record per code from `backend/tests/validation/data/hmo_shape_cases.json` against both an rdflib
and a pyoxigraph data graph, and asserts that no golden-corpus object has a violation.
