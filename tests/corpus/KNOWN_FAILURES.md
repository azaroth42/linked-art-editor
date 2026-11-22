# Corpus files that do not validate against the vendored JSON Schema

`backend/tests/test_corpus_schema.py` validates every corpus file against the entry-point schema
for its `type` (`lae.validation.schema_map`), ignoring the `id` of embedded nodes and nested
repeats of the pinned `@context`, as `lae.validation` does (ADR-0028). Each file in the table below fails and is a strict
expected failure: if one starts to validate, the test fails until its row is removed. A file not
listed here must validate. Corpus files are never edited or deleted to make them pass.

The schema is `profiles/schema/` (json-validator `43277cc` plus uncommitted upstream changes; see
`profiles/schema/SOURCE.md`); the examples are linked.art `7650a86` (2026-11-03). Most failures
are drift between the two, not broken records.

**92 of 276 files fail: 57 linked.art examples, 3 institutional records, 32 LUX records.**

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
7. **LUX API additions (all 32 LUX records).** Every record served by
   `lux.collections.yale.edu/data/...` carries `_links` (HAL links to related searches), which no
   entry-point schema allows. Other underscore-prefixed LUX keys: `_content_html` on statements
   (10 files), `_seconds_since_epoch_begin_of_the_begin` / `..._end_of_the_end` on time-spans
   (9 files), `_audioguide_url` (1 file). Single files also have `see_also` on an object,
   `representation` on a `VisualItem`, a `Period` whose `equivalent` is typed `Period` (the schema
   wants `Activity`), and two identifiers classified by a `Type` without an `id`. Their nested
   repeats of the pinned `@context` are ignored (ADR-0028).

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
| `lux/activity-2fcb9f1b-0de3-4bb9-966c-11d160ab00a8.json` | Period | `event.json` | 2 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/equivalent/0/type const: 'Activity' was expected` |
| `lux/activity-efeb56f7-9a98-449d-9234-375555814114.json` | Activity | `event.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/concept-2d7ee50e-14c2-4477-b076-e22a3fd600a1.json` | Type | `concept.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/concept-710b7648-91ce-438a-865d-c39588b7e2c8.json` | MeasurementUnit | `concept.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/concept-8da5c4f7-2f76-4861-b58e-7f15ffd6ef18.json` | Material | `concept.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/concept-a7a11bba-fd6b-4c35-97d7-3c3e421e95d9.json` | Type | `concept.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/concept-b3e797cd-7e04-45d5-9c44-64a62d4e95a4.json` | Currency | `concept.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/concept-d06d30df-b3fd-4f09-8b15-b1927843b612.json` | Type | `concept.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/concept-ddd29579-6862-417a-9ffe-5d0798be7e24.json` | Language | `concept.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/concept-e7ac1a41-319a-4e1f-bb30-d12f19a42902.json` | Type | `concept.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/concept-f7ef5bb4-e7fb-443d-9c6b-371a23e717ec.json` | Type | `concept.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/concept-f989a0fc-f3dd-4d82-8580-fcc9bcb5be89.json` | Language | `concept.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/digital-6302dd42-df1d-5b06-980c-b672bfae6cdc.json` | DigitalObject | `digital.json` | 2 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/referred_to_by/0 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)` |
| `lux/group-41310ca5-8137-45fe-ac2c-a6a04e2235f1.json` | Group | `group.json` | 4 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/carried_out/0/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin' was unexpected)`<br>`/formed_by/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/representation/0/digitally_shown_by/0/referred_to_by/1 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)` |
| `lux/group-91803a28-6135-4564-ae16-c6658000d088.json` | Group | `group.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/object-6a76f826-54e7-4b94-8fee-f043b29bb2b8.json` | HumanMadeObject | `object.json` | 71 | `/ additionalProperties: Additional properties are not allowed ('_links', 'see_also' were unexpected)`<br>`/identified_by/11 anyOf: matches none of the allowed shapes`<br>`/identified_by/12 anyOf: matches none of the allowed shapes`<br>`/produced_by/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/referred_to_by/1 additionalProperties: Additional properties are not allowed ('_audioguide_url', '_content_html' were unexpected)`<br>`/referred_to_by/10 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)`<br>`... and 65 more` |
| `lux/object-9e77d0f8-b0ac-402c-bfe3-3b8121ae9f58.json` | HumanMadeObject | `object.json` | 4 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/produced_by/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/referred_to_by/11 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)`<br>`/referred_to_by/3 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)` |
| `lux/object-b7285efb-e100-43e1-ba55-6dde6ea4b556.json` | HumanMadeObject | `object.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/object-c1b6648b-caf8-4a1f-b28d-43ce8c83a852.json` | HumanMadeObject | `object.json` | 5 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/produced_by/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/referred_to_by/0 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)`<br>`/referred_to_by/1 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)`<br>`/referred_to_by/10 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)` |
| `lux/person-07cd63b8-4bd2-460a-b6cb-c8d5bd1d0b4f.json` | Person | `person.json` | 4 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/born/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/died/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/representation/0/digitally_shown_by/0/referred_to_by/1 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)` |
| `lux/person-2107305b-7a4c-4af8-9d02-7a75bb1b2a4e.json` | Person | `person.json` | 5 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/born/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/carried_out/0/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/died/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/representation/0/digitally_shown_by/0/referred_to_by/0 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)` |
| `lux/person-252d23b3-b84f-4cc5-94ca-9e98928bea48.json` | Person | `person.json` | 6 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/born/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/carried_out/0/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/died/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/representation/0/digitally_shown_by/0/referred_to_by/1 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)`<br>`/representation/1/digitally_shown_by/0/referred_to_by/1 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)` |
| `lux/person-3c22919c-57e0-4cf0-84c3-0b4cd8a78572.json` | Person | `person.json` | 5 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/born/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/died/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)`<br>`/representation/0/digitally_shown_by/0/referred_to_by/1 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)`<br>`/representation/1/digitally_shown_by/0/referred_to_by/1 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)` |
| `lux/place-1d61ce08-b737-4326-97ba-85e5e6f9cff7.json` | Place | `place.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/place-a5b17437-4725-48ec-8a9f-b014e3187b64.json` | Place | `place.json` | 2 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/representation/0/digitally_shown_by/0/referred_to_by/1 additionalProperties: Additional properties are not allowed ('_content_html' was unexpected)` |
| `lux/place-bb47b983-248b-5a9f-93c9-eb400993b94a.json` | Place | `place.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/place-f10c9789-68d5-4543-a782-82f151babc5a.json` | Place | `place.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/place-fff6ea51-e555-4b4e-92b1-8b378d0f913b.json` | Place | `place.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/set-703438a0-4a84-4bc8-8c9c-28e8d3e944f8.json` | Set | `set.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/set-92e30c6b-6b10-46f9-b460-9c2ec2914e16.json` | Set | `set.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)` |
| `lux/text-a76b91ea-b120-466f-bb38-973e1aa0221d.json` | LinguisticObject | `text.json` | 2 | `/ additionalProperties: Additional properties are not allowed ('_links' was unexpected)`<br>`/used_for/0/timespan additionalProperties: Additional properties are not allowed ('_seconds_since_epoch_begin_of_the_begin', '_seconds_since_epoch_end_of_the_end' were unexpected)` |
| `lux/visual-cd439eb0-d7a5-41dd-bf2f-f5d95e7d1bc1.json` | VisualItem | `image.json` | 1 | `/ additionalProperties: Additional properties are not allowed ('_links', 'representation' were unexpected)` |
