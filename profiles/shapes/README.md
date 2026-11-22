# profiles/shapes/

SHACL shapes in Turtle. Linked Art publishes no SHACL; these are written for this project
(decision C1, ADR-0008). JSON Schema runs first; a record reaches SHACL only when it passes the
schema. `lae.validation.shacl` (Phase 1 Step 1.9) loads every `*.ttl` here into one shapes graph
at startup and runs it with pyshacl over one record's data graph: the record's expansion plus the
`rdf:type` of every referenced entity (from `entity_labels.type`, else the `type` the reference
declares; rule 10).

| File | Covers | Since |
| :-- | :-- | :-- |
| `common.ttl` | `Name`, `Identifier`, `TimeSpan`; the `lae:code` declaration and the SPARQL prefix declarations all files share | Step 1.9 |
| `HumanMadeObject.ttl` | `HumanMadeObject` (`crm:E22_Human-Made_Object`), `Dimension` | Spike S4 (Phase 0, Step 0.9) |
| `Person.ttl` | `Person` (`crm:E21_Person`) | Step 1.9 |
| `Place.ttl` | `Place` (`crm:E53_Place`) | Step 1.9 |
| `Type.ttl` | `Type` (`crm:E55_Type`) | Step 1.9 |

The files are small on purpose. Phase 2 Step 2.2 compiles Value Set bindings into further shapes.

## Namespaces

| Prefix | IRI | Used for |
| :-- | :-- | :-- |
| `lae:` | `urn:lae:ns:` | The project's annotation vocabulary. Only `lae:code` so far. |
| `laes:` | `urn:lae:shape:` | Shape IRIs, `laes:<Class>-<rule>` (e.g. `laes:HumanMadeObject-made_of`), and the resource that carries the SHACL-SPARQL prefix declarations (`laes:` itself, in `common.ttl`). |

Both are URNs on purpose: the deployment `base_uri` differs between installations, the IRIs are
never dereferenced, and the `linked.art` namespace belongs to Linked Art. Declare both prefixes in
every file.

### `lae:code`

`lae:code` is an `owl:AnnotationProperty` whose value is the diagnostic code reported for every
validation result of the shape it annotates, for example `lae:code "LAE-SHACL-006"`.
`lae.validation.shacl` reads it from each result's `sh:sourceShape` and returns it as
`Diagnostic.code`; the same string is the i18n key under the `errors` namespace (conventions §2).

## Rules for shape authors

1. **Every shape that has a target** (`sh:targetClass`, `sh:targetNode`, `sh:targetObjectsOf`,
   `sh:targetSubjectsOf`) is an IRI and carries exactly one `lae:code` of the form
   `LAE-SHACL-NNN`. Codes are unique across all files and never reused after a shape is removed.
   `LAE-SHACL-000` is reserved (the validator's own "could not be completed"). The loader refuses
   to start with shapes that break this rule (`ShapesError`).
2. A result's `sh:sourceShape` is the targeted shape for its own constraints, for `sh:or`,
   `sh:node`, `sh:qualifiedValueShape`, and `sh:sparql`. Shapes without a target that are only
   referenced from those (the anonymous members of an `sh:or` list, for example) never appear as a
   source shape and need no code. Do not nest `sh:property` inside a targeted node shape: the
   nested property shape becomes the source shape, and its results are reported as
   `LAE-SHACL-000`. Make it a targeted `sh:PropertyShape` instead.
3. Severity: `sh:Violation` (the default) blocks publishing; `sh:Warning` is publishable after
   acknowledgement; `sh:Info` is guidance (spec §5).
4. Give every shape (or its `sh:sparql` constraint) an English `sh:message` with an `@en` tag, and
   add the code with its message to `frontend/public/locales/en/errors.json`. The catalogue is
   what `Diagnostic.message_en` and the editor show; `sh:message` is for RDF consumers.
5. Use SHACL Core and SHACL-SPARQL only, so pyshacl runs with `advanced=False`.
6. Referenced entities are in the data graph with their class, so `sh:targetClass` selects them
   too. The validator drops every result whose focus node is not a node of the record, so a shape
   cannot fail on a reference. **So a constraint on what a property may refer to must be a
   property shape whose focus node is the node that has the property** (`sh:targetClass` of the
   owner, `sh:path` the property, `sh:class` the expected class, as `LAE-SHACL-006` does). Written
   the other way round, as a node shape that targets the referenced entities
   (`sh:targetObjectsOf crm:P45_consists_of ; sh:class crm:E57_Material`), its results would all
   be dropped and the rule would never fire. A "must have" shape should still exempt a node that
   has nothing but a type and a label (a new draft), as `LAE-SHACL-001` does.
7. Performance (measured in `docs/spikes/S4-shacl.md`): express cross-node conditions in
   SHACL-SPARQL, not as `sh:or` over `sh:closed` or qualified shapes. pyshacl builds and discards
   a full result for every failing `sh:or` member, which made one such shape cost 50 ms on a
   1,700-triple record, against 0.6 ms for the SPARQL form on a pyoxigraph data graph.
8. No `xsd:dateTime` checks in SHACL. pyshacl raises `TypeError` from `sh:lessThanOrEquals`,
   aborting the whole validation, when a bound is not a datetime rdflib can parse (an ill-typed
   value, or any BCE year), and `sh:datatype xsd:dateTime` rejects valid BCE values. `TimeSpan`
   bounds are checked by the Python rules `LAE-RULE-005` (validity) and `LAE-RULE-001` (order) in
   `lae.validation.rules`. A test fails if a shape uses `sh:lessThan`, `sh:lessThanOrEquals` or
   `sh:datatype xsd:dateTime`.
9. **Paths.** A diagnostic's `path` is a JSON Pointer worked out from the result: the shape's
   `sh:path` is walked through the data graph from the focus node to `sh:value`. Predicate,
   sequence, alternative, `sh:zeroOrMorePath`, `sh:oneOrMorePath` and `sh:zeroOrOnePath` are
   followed; a result of a shape with `sh:inversePath` is placed at its focus node. A
   SHACL-SPARQL constraint on a node shape should bind `?path` to the property it is about
   (`SELECT $this (crm:P1_is_identified_by AS ?path)`), or its results are placed at the node.
   When a value is reached from the focus node in several places, there is one diagnostic for
   each.
10. **Classes.** The data graph has no `rdfs:subClassOf` triples and nothing is inferred, so
   `sh:class crm:E39_Actor` matches nothing: name the classes Linked Art uses, with `sh:or`
   where several are allowed (`LAE-SHACL-005`). The one exception is `Type`: a reference whose
   class is `Material`, `Language`, `MeasurementUnit` or `Currency` is a `crm:E55_Type` too, and
   an entity that `entity_labels` knows only as a `Type` (every seeded AAT concept) also has the
   subclass of `Type` that the reference declares. So `sh:class crm:E57_Material` on `made_of`
   holds for an AAT concept that the record calls a `Material`, and fails for one it calls a
   `Type`.

## Codes

| Code | Shape | Severity | Rule (Linked Art model docs) |
| :-- | :-- | :-- | :-- |
| `LAE-SHACL-000` | (none) | Violation | Reserved: SHACL validation could not be completed. |
| `LAE-SHACL-001` | `laes:HumanMadeObject-name` | Warning | An object has at least one `Name` in `identified_by`. |
| `LAE-SHACL-002` | `laes:HumanMadeObject-primary-name` | Warning | If it has Names, one is classified as Primary Name (`aat:300404670`). |
| `LAE-SHACL-003` | `laes:HumanMadeObject-primary-name-per-language` | Warning | At most one Primary Name per language (or without a language). |
| `LAE-SHACL-004` | `laes:HumanMadeObject-produced_by` | Violation | `produced_by` is at most one `Production`. |
| `LAE-SHACL-005` | `laes:HumanMadeObject-production-actor` | Violation | The Production and its `part`s are `carried_out_by` a `Person` or `Group`. |
| `LAE-SHACL-006` | `laes:HumanMadeObject-made_of` | Violation | `made_of` refers to a `Material`. |
| `LAE-SHACL-007` | `laes:Dimension-value` | Violation | A `Dimension` has exactly one numeric `value`. |
| `LAE-SHACL-008` | `laes:Dimension-unit` | Warning | A `Dimension` has one `unit` that is a `MeasurementUnit`. |
| `LAE-SHACL-009` | retired | | Was `laes:TimeSpan-datetime`; now the Python rule `LAE-RULE-005`. |
| `LAE-SHACL-010` | retired | | Was `laes:TimeSpan-order`; now the Python rule `LAE-RULE-001`. |
| `LAE-SHACL-011` | `laes:Name-content` | Violation | A `Name` has exactly one `content`. |
| `LAE-SHACL-012` | `laes:Name-language` | Violation | The `language` of a `Name` is a `Language`. |
| `LAE-SHACL-013` | `laes:Identifier-content` | Violation | An `Identifier` has exactly one `content`. |
| `LAE-SHACL-014` | `laes:TimeSpan-bounds` | Warning | A `TimeSpan` has `begin_of_the_begin` and `end_of_the_end`. |
| `LAE-SHACL-015` | `laes:Person-name` | Warning | A person has at least one `Name`. |
| `LAE-SHACL-016` | `laes:Person-born` | Violation | `born` is at most one `Birth`. |
| `LAE-SHACL-017` | `laes:Person-died` | Violation | `died` is at most one `Death`. |
| `LAE-SHACL-018` | `laes:Place-name` | Warning | A place has at least one `Name`. |
| `LAE-SHACL-019` | `laes:Place-part_of` | Violation | `part_of` of a place refers to a `Place`. |
| `LAE-SHACL-020` | `laes:Type-name` | Warning | A concept has at least one `Name`. |
| `LAE-SHACL-021` | `laes:Type-broader` | Violation | `broader` refers to a concept class. |

The next free code is `LAE-SHACL-022`.

## Tests

`backend/tests/validation/test_shacl.py` checks the rules above and runs every case in
`backend/tests/validation/data/shape_cases.json`: a broken record per constraint, with the code,
severity and JSON Pointer it must produce. A test fails when a code has no case.
`test_engine.py` asserts that no schema-valid golden-corpus record has a violation and pins the
corpus warning counts; `test_zz_error_messages.py` that every code has an English message.
