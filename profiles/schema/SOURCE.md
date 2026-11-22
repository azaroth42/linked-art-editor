# Linked Art JSON Schema (vendored)

The `*.json` files in this directory are copied byte-for-byte from the Linked Art JSON Schema
(Phase 0, Step 0.4). Do not edit them here; re-vendor from upstream and update this file.

| | |
| :-- | :-- |
| Repository | `linked-art/json-validator` (https://github.com/linked-art/json-validator), local checkout `/Users/rs2668/Development/linked-art/json-validator` |
| Base commit | `43277cc20e088de0611868bb34ea2cf4d279016e` (2024-12-18) |
| Source path | `schema/*.json` (14 files) |
| Vendored on | 2026-11-22 |

## Uncommitted upstream changes included

At vendoring time the upstream working tree had uncommitted changes to three schema files, dated
2026-11-03. They were vendored as found, because the committed `abstract.json` declares the
`$id` of `text.json` (the two would collide in a registry keyed by `$id`) and the changes add
properties the current linked.art examples use. The bytes here therefore differ from
`43277cc` for these three files; the hashes below are authoritative. See `docs/questions.md`,
Step 0.4.

| File | Change from `43277cc` |
| :-- | :-- |
| `abstract.json` | `$id` corrected from `.../text.json` to `.../abstract.json`. |
| `linked_art.json` | `$id` corrected to `.../linked_art.json`; adds `abstract.json` to the `anyOf`. |
| `core.json` | Adds the `motivated_byProp` definition and a `motivated_by` property on `AttributeAssignment`; `Name` gains `assigned_by` and loses `referred_to_by`; `LanguageRef` gains `notation`; `TypeRef` gains `classified_as`; whitespace. |

## SHA-256

| File | SHA-256 |
| :-- | :-- |
| `abstract.json` | `8a7641bf959d3a4c57a8e954e27f02b0468672fe91d5500336229bd9605fb4bd` |
| `concept.json` | `e8770577a35dd0f3f108e0df3a5e68234c2e0113d4946e7b5957027a883b7329` |
| `core.json` | `8312326b89aca1f716f41032d7bddd395fb1d9cba3f6a7baa0f02d8c9b5dd38b` |
| `digital.json` | `dc5ae51f8c92e3fa697968e26acdbe065202fbcf4d0e5f529916def2df9e67ef` |
| `event.json` | `8e465e91c276e507587622d5cbc470aad1ddcec0b630578f123c62b0f37effcf` |
| `group.json` | `87ebd65c0c6f8d10bbd3b2cc9d028a17dc2e022dda97004fb6fba62e27dcffdc` |
| `image.json` | `0b777196ed77a35af99ca5b58338a6b00015f1a3f9717dccff53a2aec053616a` |
| `linked_art.json` | `d743ffab9c3e2a52faf21cec1069be5a119fa5f0e15444a4e2e12aa6963fde97` |
| `object.json` | `b2de09e39ff0201bcbbb2bf8616996feafd32645c577d5d04621bd294675affb` |
| `person.json` | `6a23a254e355dbc57aa3ec114b6b38f6dd07f99d33e22d177c3bfd76df9710ee` |
| `place.json` | `2a8916bff823db5194f1d15f7fa2f094daaaa00f24649e410d1ebada7a13ccb2` |
| `provenance.json` | `07f5209a8a893e06f13d128220ec13665f2451596b6e9692f330ee584843b48f` |
| `set.json` | `ceb14602d75e34379cc830660fa7fcc5382ba8a5d7e15044da7421d909681138` |
| `text.json` | `3c471adeb4b06b98b39219cbcf8a7709c5ea2681494fd5daf411caf2041a8f79` |

## How the editor uses these files

- **Dialect.** The files declare `$schema` 2020-12 and keep definitions under `$defs`, but they
  use only keywords that mean the same in Draft 7 (`$ref` never has sibling assertions). As the
  guide and upstream `schema-test.py` do, the editor validates with `Draft7Validator`.
- **References.** Every file has `$id` `https://linked.art/api/1.0/schema/<file>` and refers to
  shared definitions as `core.json#/$defs/...`. `lae.validation.schema_map.load_registry()` builds
  a `referencing` registry keyed by `$id`; nothing is fetched from the network.
- **Entry points.** `core.json` (shared definitions) and `linked_art.json` (`anyOf` of all entry
  points) are not used directly. A record is validated against the schema for its `type`
  (`lae.validation.schema_map.SCHEMA_BY_TYPE`, checked against each schema's `type` constraint by
  `backend/tests/validation/test_schema_map.py`):

| Record `type` | Schema |
| :-- | :-- |
| `HumanMadeObject` | `object.json` |
| `Person` | `person.json` |
| `Group` | `group.json` |
| `Place` | `place.json` |
| `Type`, `Currency`, `Language`, `Material`, `MeasurementUnit` | `concept.json` |
| `Set` | `set.json` |
| `DigitalObject` | `digital.json` |
| `LinguisticObject` | `text.json` |
| `VisualItem` | `image.json` |
| `PropositionalObject` | `abstract.json` |
| `Activity` classified as AAT `300055863` (provenance activity) | `provenance.json` |
| any other `Activity`, `Event`, `Period` | `event.json` |

## Known defects

- `provenance.json` refers to `core.json#/$defs/LegalRight` twice (`RightAcquisition` →
  `establishes` and `invalidates`), but `core.json` has no `LegalRight` (it defines `Right`). This
  is in the committed upstream file too. Validating a record that reaches those properties raises
  `referencing.exceptions.Unresolvable`; the corpus test reports it as an error, and four
  linked.art provenance examples are listed in `tests/corpus/KNOWN_FAILURES.md` for it.
