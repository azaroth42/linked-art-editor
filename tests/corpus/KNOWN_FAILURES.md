# Corpus files that do not validate against the vendored JSON Schema

`backend/tests/test_corpus_schema.py` validates every corpus file against the entry-point schema
for its `type` (`lae.validation.schema_map`), ignoring the `id` of embedded nodes and nested
repeats of the pinned `@context`, as `lae.validation` does (ADR-0028). Each file in the table below fails and is a strict
expected failure: if one starts to validate, the test fails until its row is removed. A file not
listed here must validate. Corpus files are never edited or deleted to make them pass.

The schema is `profiles/schema/` (json-validator `43277cc` plus uncommitted upstream changes; see
`profiles/schema/SOURCE.md`); the examples are linked.art `7650a86` (2026-11-03). Most failures
are drift between the two, not broken records.

**60 of 244 files fail: 57 linked.art examples, 3 institutional records.**

## Patterns

1. **Upstream schema defect (4 files).** `provenance.json` refers to `core.json#/$defs/LegalRight`,
   which does not exist (`core.json` defines `Right`). Validation of a `RightAcquisition` stops
   with an unresolvable `$ref`; it is reported as `(schema) unresolvable $ref`.
2. **References without `id` (about 25 files).** Examples use unidentified stubs such as
   `{"type": "Person", "_label": "Owner"}` or `{"type": "Place", "_label": "..."}`; the schema's
   reference definitions require `id`, so these fail as `required: 'id'` or as `anyOf` (no
   alternative matched).
3. **Top-level `_label` missing (9 files).** Small examples (`object-3.json`, `object-7.json`, ...)
   omit `_label`, which every entry-point schema requires.
4. **Cardinality differences (6 files).** `part_of`, `held_or_supported_by` and
   `current_permanent_custodian` are arrays in one and single objects in the other.
5. **Properties or shapes the schema does not allow yet.** For example `members_exemplified_by`,
   `shown_by`, `part` on a `HumanMadeObject`, `representation` on a `VisualItem`, `classified_as`
   on a place reference, `referred_to_by` on a `TimeSpan`, `currency` on a `Dimension` in a `Set`,
   and provenance `part`s (`Payment`, `Transfer`, promise `Activity`) that match no alternative.
6. **Institutional data quirks (3 files).** Yale Library records embed full entities (with
   `created_by`, `identified_by`) inside `about` (they also repeat the pinned `@context` on
   nested nodes, which is ignored, ADR-0028); one YUAG object has `used_for` as an object instead of an array.

## Files

Each summary line is `<JSON Pointer> <keyword>: <message>`, as printed by the test (at most six
per file).

| File | Type | Schema | Errors | Error summary |
| :-- | :-- | :-- | --: | :-- |
| `institutional/yuag-obj-31619.json` | HumanMadeObject | `object.json` | 1 | `/used_for type: is not of type 'array'` |
| `institutional/yul-node-07e71913-f2be-479c-a388-65025173b4fb.json` | LinguisticObject | `text.json` | 9 | `/about/0 additionalProperties: Additional properties are not allowed ('created_by', 'identified_by' were unexpected)`<br>`/about/0 required: 'id' is a required property`<br>`/about/1 additionalProperties: Additional properties are not allowed ('created_by', 'identified_by' were unexpected)`<br>`/about/1 required: 'id' is a required property`<br>`/about/2 additionalProperties: Additional properties are not allowed ('created_by', 'identified_by' were unexpected)`<br>`/about/2 required: 'id' is a required property`<br>`... and 3 more` |
| `institutional/yul-node-14a3551f-6e93-4501-9b18-1b107f0274aa.json` | VisualItem | `image.json` | 11 | `/ additionalProperties: Additional properties are not allowed ('representation' was unexpected)`<br>`/about/0 additionalProperties: Additional properties are not allowed ('created_by', 'identified_by' were unexpected)`<br>`/about/0 required: 'id' is a required property`<br>`/about/1 additionalProperties: Additional properties are not allowed ('created_by', 'identified_by' were unexpected)`<br>`/about/1 required: 'id' is a required property`<br>`/about/2 additionalProperties: Additional properties are not allowed ('created_by', 'identified_by' were unexpected)`<br>`... and 5 more` |
| `linkedart/activity-exha-1.json` | Activity | `event.json` | 2 | `/carried_out_by/0 anyOf: matches none of the allowed shapes`<br>`/took_place_at/0 additionalProperties: Additional properties are not allowed ('classified_as' was unexpected)` |
| `linkedart/activity-exhb-1.json` | Activity | `event.json` | 3 | `/carried_out_by/0 anyOf: matches none of the allowed shapes`<br>`/part_of anyOf: matches none of the allowed shapes`<br>`/took_place_at/0 additionalProperties: Additional properties are not allowed ('classified_as' was unexpected)` |
| `linkedart/concept-1.json` | Type | `concept.json` | 4 | `/broader/0 required: 'id' is a required property`<br>`/created_by/influenced_by/0 required: 'id' is a required property`<br>`/created_by/influenced_by/1 required: 'id' is a required property`<br>`/member_of/0 required: 'id' is a required property` |
| `linkedart/digital-0.json` | DigitalObject | `digital.json` | 2 | `/digitally_shows/0 required: 'id' is a required property`<br>`/part_of type: is not of type 'object'` |
| `linkedart/event-0.json` | Activity | `event.json` | 1 | `/subject_of/0 additionalProperties: Additional properties are not allowed ('id' was unexpected)` |
| `linkedart/event-1.json` | Activity | `event.json` | 2 | `/ required: '_label' is a required property`<br>`/timespan additionalProperties: Additional properties are not allowed ('referred_to_by' was unexpected)` |
| `linkedart/event-christies_24477-1.json` | Activity | `event.json` | 2 | `/carried_out_by/0 anyOf: matches none of the allowed shapes`<br>`/took_place_at/0 additionalProperties: Additional properties are not allowed ('classified_as' was unexpected)` |
| `linkedart/event-christies_24477_16-1.json` | Activity | `event.json` | 1 | `/part_of anyOf: matches none of the allowed shapes` |
| `linkedart/event-early_roman-1.json` | Period | `event.json` | 1 | `/part_of anyOf: matches none of the allowed shapes` |
| `linkedart/group-0.json` | Group | `group.json` | 3 | `/dissolved_by/took_place_at/0 required: 'id' is a required property`<br>`/formed_by/took_place_at/0 required: 'id' is a required property`<br>`/residence/0 required: 'id' is a required property` |
| `linkedart/group-14thc_italian-1.json` | Group | `group.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('members_exemplified_by' was unexpected)` |
| `linkedart/object-0.json` | HumanMadeObject | `object.json` | 1 | `/member_of/0 required: 'id' is a required property` |
| `linkedart/object-1.json` | HumanMadeObject | `object.json` | 2 | `/current_location required: 'id' is a required property`<br>`/produced_by/took_place_at/0 required: 'id' is a required property` |
| `linkedart/object-10.json` | HumanMadeObject | `object.json` | 1 | `/ required: '_label' is a required property` |
| `linkedart/object-11.json` | HumanMadeObject | `object.json` | 5 | `/current_location required: 'id' is a required property`<br>`/current_owner/0 anyOf: matches none of the allowed shapes`<br>`/member_of/0 required: 'id' is a required property`<br>`/referred_to_by/0 required: 'content' is a required property`<br>`/shows/0 required: 'id' is a required property` |
| `linkedart/object-12.json` | HumanMadeObject | `object.json` | 2 | `/ required: '_label' is a required property`<br>`/referred_to_by/0 additionalProperties: Additional properties are not allowed ('subject_to' was unexpected)` |
| `linkedart/object-13.json` | HumanMadeObject | `object.json` | 1 | `/ required: '_label' is a required property` |
| `linkedart/object-2.json` | HumanMadeObject | `object.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('part' was unexpected)` |
| `linkedart/object-3.json` | HumanMadeObject | `object.json` | 1 | `/ required: '_label' is a required property` |
| `linkedart/object-7.json` | HumanMadeObject | `object.json` | 1 | `/ required: '_label' is a required property` |
| `linkedart/object-8.json` | HumanMadeObject | `object.json` | 2 | `/ required: '_label' is a required property`<br>`/dimension/0/assigned_by/0/carried_out_by/0 anyOf: matches none of the allowed shapes` |
| `linkedart/object-9.json` | HumanMadeObject | `object.json` | 2 | `/ required: '_label' is a required property`<br>`/identified_by/0 anyOf: matches none of the allowed shapes` |
| `linkedart/object-letter-3.json` | HumanMadeObject | `object.json` | 1 | `/held_or_supported_by type: is not of type 'object'` |
| `linkedart/object-nightwatch-17.json` | HumanMadeObject | `object.json` | 1 | `/current_permanent_custodian type: is not of type 'array'` |
| `linkedart/object-nightwatch-18.json` | HumanMadeObject | `object.json` | 1 | `/ required: '_label' is a required property` |
| `linkedart/object-nightwatch-25.json` | HumanMadeObject | `object.json` | 1 | `/attributed_by/0/part_of anyOf: matches none of the allowed shapes` |
| `linkedart/object-nightwatch-26.json` | HumanMadeObject | `object.json` | 1 | `/modified_by/0/part_of anyOf: matches none of the allowed shapes` |
| `linkedart/object-nightwatch-43.json` | HumanMadeObject | `object.json` | 1 | `/produced_by/timespan/identified_by/0 anyOf: matches none of the allowed shapes` |
| `linkedart/object-nightwatch-70.json` | HumanMadeObject | `object.json` | 1 | `/referred_to_by/0 additionalProperties: Additional properties are not allowed ('created_by', 'subject_to' were unexpected)` |
| `linkedart/object-nightwatch-9.json` | HumanMadeObject | `object.json` | 1 | `/dimension/0/assigned_by/0/part_of anyOf: matches none of the allowed shapes` |
| `linkedart/object-nightwatch-support.json` | HumanMadeObject | `object.json` | 1 | `/part_of type: is not of type 'object'` |
| `linkedart/object-spring-15.json` | HumanMadeObject | `object.json` | 2 | `/changed_ownership_through/0/part_of anyOf: matches none of the allowed shapes`<br>`/changed_ownership_through/1/part_of anyOf: matches none of the allowed shapes` |
| `linkedart/object-spring-back.json` | HumanMadeObject | `object.json` | 1 | `/part_of type: is not of type 'object'` |
| `linkedart/object-spring-support.json` | HumanMadeObject | `object.json` | 1 | `/part_of type: is not of type 'object'` |
| `linkedart/object-yiadom-boakye-1.json` | HumanMadeObject | `object.json` | 2 | `/identified_by/0 anyOf: matches none of the allowed shapes`<br>`/identified_by/1 anyOf: matches none of the allowed shapes` |
| `linkedart/person-0.json` | Person | `person.json` | 4 | `/born/took_place_at/0 required: 'id' is a required property`<br>`/carried_out/0/took_place_at/0 required: 'id' is a required property`<br>`/died/took_place_at/0 required: 'id' is a required property`<br>`/residence/0 required: 'id' is a required property` |
| `linkedart/person-rembrandt-10.json` | Person | `person.json` | 1 | `/identified_by/0 anyOf: matches none of the allowed shapes` |
| `linkedart/person-rembrandt-4.json` | Person | `person.json` | 1 | `/identified_by/0 anyOf: matches none of the allowed shapes` |
| `linkedart/place-0.json` | Place | `place.json` | 2 | `/member_of/0 required: 'id' is a required property`<br>`/part_of/0 required: 'id' is a required property` |
| `linkedart/place-amsterdam-1.json` | Place | `place.json` | 1 | `/part_of/0 additionalProperties: Additional properties are not allowed ('classified_as' was unexpected)` |
| `linkedart/provenance-1.json` | Activity | `provenance.json` | 1 | `/part/1 anyOf: matches none of the allowed shapes` |
| `linkedart/provenance-2.json` | Activity | `provenance.json` | 2 | `(schema) unresolvable $ref: /$defs/LegalRight`<br>`/part/0 anyOf: matches none of the allowed shapes` |
| `linkedart/provenance-3.json` | Activity | `provenance.json` | 1 | `(schema) unresolvable $ref: /$defs/LegalRight` |
| `linkedart/provenance-4.json` | Activity | `provenance.json` | 1 | `(schema) unresolvable $ref: /$defs/LegalRight` |
| `linkedart/provenance-5.json` | Activity | `provenance.json` | 1 | `(schema) unresolvable $ref: /$defs/LegalRight` |
| `linkedart/provenance-coffin_promise-1.json` | Activity | `provenance.json` | 1 | `/part/0 anyOf: matches none of the allowed shapes` |
| `linkedart/provenance-fixme-1.json` | Activity | `provenance.json` | 2 | `/part/0 anyOf: matches none of the allowed shapes`<br>`/part/1 anyOf: matches none of the allowed shapes` |
| `linkedart/provenance-nuveen_commission-1.json` | Activity | `provenance.json` | 2 | `/part/0 anyOf: matches none of the allowed shapes`<br>`/part/1 anyOf: matches none of the allowed shapes` |
| `linkedart/provenance-sharp_sale-1.json` | Activity | `provenance.json` | 1 | `/part/1 anyOf: matches none of the allowed shapes` |
| `linkedart/provenance-transfer-1.json` | Activity | `provenance.json` | 1 | `/part/0 anyOf: matches none of the allowed shapes` |
| `linkedart/provenance-unknown_faure-1.json` | Activity | `provenance.json` | 2 | `/after/0 anyOf: matches none of the allowed shapes`<br>`/before/0 anyOf: matches none of the allowed shapes` |
| `linkedart/provenance-yuag_stora-1.json` | Activity | `provenance.json` | 1 | `/part/1 anyOf: matches none of the allowed shapes` |
| `linkedart/set-2.json` | Set | `set.json` | 3 | `/dimension/0 additionalProperties: Additional properties are not allowed ('currency', 'referred_to_by' were unexpected)`<br>`/dimension/0 required: 'unit' is a required property`<br>`/dimension/0/type const: 'Dimension' was expected` |
| `linkedart/set-24477_16_objects-2.json` | Set | `set.json` | 3 | `/dimension/0 additionalProperties: Additional properties are not allowed ('currency' was unexpected)`<br>`/dimension/0 required: 'unit' is a required property`<br>`/dimension/0/type const: 'Dimension' was expected` |
| `linkedart/text-0.json` | LinguisticObject | `text.json` | 3 | `/created_by/carried_out_by/0 anyOf: matches none of the allowed shapes`<br>`/used_for/0/carried_out_by/0 anyOf: matches none of the allowed shapes`<br>`/used_for/0/took_place_at/0 required: 'id' is a required property` |
| `linkedart/text-koot_nightwatch-1.json` | LinguisticObject | `text.json` | 1 | `/part_of/0 anyOf: matches none of the allowed shapes` |
| `linkedart/visual-0.json` | VisualItem | `image.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('shown_by' was unexpected)` |
