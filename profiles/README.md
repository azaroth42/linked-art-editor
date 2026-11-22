# profiles/

Declarative configuration, versioned with the code and validated in CI.

| Directory | Contents | Schema |
| :-- | :-- | :-- |
| `ui/` | UI Profiles: one `<Class>.yaml` per Linked Art record class (spec §6.1) | `ui/_schema.json` |
| `vocab/` | Value Set definitions and slot bindings (spec §7) | `vocab/_schema.json` |
| `shapes/` | SHACL shapes (Turtle), one file per record class; the `lae:` namespace, `lae:code`, and authoring rules are in [`shapes/README.md`](shapes/README.md) | — |
| `context/` | Pinned Linked Art `@context`, `v<N>.json` | — |
| `schema/` | Linked Art JSON Schema, vendored from `linked-art/json-validator` | — |

Both schemas are JSON Schema Draft 2020-12.

## Validating

```sh
uv run --project backend python scripts/validate_profiles.py
```

Exit code 0 means every file is valid. Each problem is printed as `<file>: <JSON path>: <message>`. The checks live in `lae.profiles.validation.validate_profiles_dir`, which the API also runs at startup from Phase 1:

1. Both `_schema.json` files are valid Draft 2020-12 schemas, and the definitions they share (`LangMap`, `Path`, `RecordClass`, `ValueSetId`) are identical.
2. `ui/` and `vocab/` contain only `*.yaml` files, besides `_schema.json` and dotfiles (`.yml`, `.json` profiles are rejected rather than silently skipped).
3. YAML is parsed with a safe loader that rejects duplicate keys and non-string keys.
4. Every file validates against its schema.
5. UI Profiles: `class` equals the file name stem, section ids are unique, slot ids are unique across the whole profile, and `grid_columns` name existing slots, each at most once. No slot or binding path names `id` or `type`.
6. Value Sets: ids are unique across all files, term URIs are unique within a set, every binding names a defined Value Set, and no `(class, path, value_set)` binding appears twice.
7. Once `vocab/` contains any YAML file, every `value_set` named by a UI Profile slot must be defined there. While `vocab/` is empty the check is skipped and a note is printed.
8. Once `context/` contains a pinned `@context`, every key in a slot or binding path must be a term of that context and every class filter a class term. Without a context the check is skipped and a note is printed.

## LangMap

Every human-readable configuration string (section and slot labels, help text, grid column labels, Value Set labels and scope notes, term labels and scope notes) is a **LangMap**: an object whose keys are BCP-47 language tags and whose values are strings.

```yaml
label:
  en: Type of Work
  fr: Type d'œuvre
  "no": Verkstype
```

- `en` is required. Values must contain at least one non-space character.
- Tags are written in canonical case: a 2–3 letter lowercase language, optional extended language subtags, an optional title-case script (`Hant`), an optional uppercase region or 3-digit area (`BR`, `419`), then lowercase variants, extensions, and `-x-` private use. Examples: `en`, `pt-BR`, `zh-Hant-TW`, `es-419`, `de-CH-1901`, `und`. Rejected: `EN`, `en_GB`, `en-gb`, `english`.
- Quote `"no"` (Norwegian): YAML reads a bare `no` as the boolean false, and the validator rejects it.
- Resolution at runtime follows spec §6.7: user preference, then localStorage, then `Accept-Language`, then `en`.

## Path syntax

Slot `path`s and binding `path`s use one syntax. The Python path operations (`lae.projection.ops`, Step 1.4) and their TypeScript mirror (`frontend/src/crdt/paths.ts`, Step 1.17) implement it. `lae.profiles.paths.parse_path` parses it.

### Grammar

```abnf
path     = segment *( "." segment )
segment  = key [ "[" class "]" ]
key      = %x61-7A *( %x61-7A / DIGIT / "_" )   ; a-z, then a-z 0-9 _
class    = %x41-5A *ALPHA                        ; A-Z, then A-Z a-z
```

The same grammar as a regular expression (used verbatim in both schemas and as `lae.profiles.paths.PATH_PATTERN`):

```
^[a-z][a-z0-9_]*(?:\[[A-Z][A-Za-z]*\])?(?:\.[a-z][a-z0-9_]*(?:\[[A-Z][A-Za-z]*\])?)*$
```

- A **key** is a Linked Art JSON-LD property name exactly as it appears in a record: `identified_by`, `produced_by`, `begin_of_the_begin`. Paths never name `id` or `type` (the validator rejects them), and the grammar excludes `_label` and other `_`-prefixed keys.
- A **class filter** `[Class]` keeps only entries whose `type` is exactly `Class`. It is a string comparison: `[Type]` does not match a `Material`.
- There are no positional indexes (`[0]`), wildcards, brace sets (`{a,b}`), entry keys, quoting, or whitespace.

### Reading

Evaluate a path against a record as follows:

1. Start with the record itself as the only current value.
2. For each segment, replace every current value with what its `key` holds: nothing if the value is not an object or the key is absent; each element, in order, if the key holds an array; otherwise the single object or scalar it holds.
3. If the segment has a class filter, drop every value that is not an object whose `type` equals the class.
4. The values left after the last segment are what the path addresses, in document order, concatenated across parents.

In the Y.Doc (decisions B2, B3; ADR-0018) the items of an array are entries in the `Map` of the object that has the property, under the entry key `JSON.stringify([property, item key])`, where the item key is the internal node IRI or target URI; each entry carries `_order`. Evaluation visits the entries sorted by `_order`, then entry key, which is the order the projected JSON-LD array has.

Examples against the linked.art Mona Lisa example (`docs/example/object/0.json`):

| Path | Addresses |
| :-- | :-- |
| `identified_by[Name]` | the three `Name` nodes |
| `identified_by[Name].content` | `"Mona Lisa"`, `"La Joconde"`, `"La Gioconda"` |
| `identified_by[Identifier].content` | `"INV. 779"` |
| `classified_as` | the two `Type` references (Painting, Artwork) |
| `dimension.unit` | two `MeasurementUnit` references |
| `produced_by.timespan.begin_of_the_begin` | one `xsd:dateTime` string |
| `identified_by[Name].part[Name].classified_as` | name-part types on a `Person` (spec §7.2) |

### Writing

- A write goes through a single chain of nodes: every segment before the last must address at most one node. A path such as `identified_by[Name].content` is ambiguous for writing on a record with two Names. One entry of a multi-valued collection is identified by its entry key, passed next to the path and never encoded in it, as in `remove_entry(doc, path, key)`; to write inside one entry, pass its map as the operation's `base` and a path relative to it. The exact behaviour of every operation is rules R20–R25 of `docs/spikes/S3-projection.md`; they reject a write path that names `id` or `type`.
- A missing intermediate node is created with `type` set to the segment's class filter if it has one, otherwise to the class the Linked Art model gives the property's range (`produced_by` → `Production`, `timespan` → `TimeSpan`; the full list is `lae.projection.rules.SINGLE_VALUED_CLASSES`). Those properties are created single-valued; any other missing intermediate is created as an array entry. A profile path that would need to create an intermediate with no such class (for example `part`) carries a filter: `produced_by.part[Production]`.
- A new entry added through a filtered last segment takes the filter as its `type`: adding to `identified_by[Identifier]` creates `{"type": "Identifier", ...}`.

## UI Profiles

```yaml
class: HumanMadeObject        # a Linked Art record class; equals the file name stem
version: 1                    # integer, bump on slot changes
sections:
  - id: identification        # lower snake_case, unique in the profile
    label: {en: Identification}
    advanced: false           # optional, default false: whole section behind "Add details"
    slots:
      - id: title             # lower snake_case, unique across the whole profile
        label: {en: Title}
        help_text: {en: ...}  # optional
        path: identified_by[Name]
        widget: langtext
        value_set: vocab:work_title_types   # optional; required for classification
        multiple: true        # ordered collection rather than at most one value
        required: true        # UI hint; publish-blocking rules live in SHACL
        advanced: false       # slot behind "Add details"
grid_columns:                 # optional; default is every non-advanced slot in form order
  - slot: title
    label: {en: Title}        # optional; defaults to the slot label
```

`entity_ref` slots must list `allowed_classes` (record classes a reference may point at; autocomplete `types` and drag-and-drop targeting use it). No other widget takes `allowed_classes`.

### What a slot path points at, per widget

| Widget | The slot path addresses | The widget edits, relative to each addressed value | `value_set` governs |
| :-- | :-- | :-- | :-- |
| `text` | string literals | the string | — |
| `langtext` | `Name` or `LinguisticObject` nodes, e.g. `identified_by[Name]` | `content` (a `Y.Text`), `language`, `classified_as` | the nodes' `classified_as` |
| `edtf` | one `TimeSpan` node, e.g. `produced_by.timespan` | `begin_of_the_begin`, `end_of_the_begin`, `begin_of_the_end`, `end_of_the_end`, and the display label `identified_by[Name].content` | — |
| `entity_ref` | references to records of `allowed_classes`, e.g. `produced_by.carried_out_by` | the reference set (keyed by target URI) | optional; narrows the picker |
| `classification` | `Type` (or subclass) references, e.g. `classified_as`, `made_of` | the references the Value Set claims | the references themselves |
| `dimension` | `Dimension` nodes, e.g. `dimension` | `value`, `unit`, `classified_as` | the nodes' `classified_as` |
| `identifier` | `Identifier` nodes, e.g. `identified_by[Identifier]` | `content`, `classified_as` | the nodes' `classified_as` |
| `statement` | `LinguisticObject` nodes, e.g. `referred_to_by[LinguisticObject]` | `content`, `language`, `classified_as` | the nodes' `classified_as` |
| `wkt` | one WKT string, e.g. `defined_by` | the string, as one last-writer-wins value | — |
| `production` | `Production` nodes, e.g. `produced_by` or `produced_by.part[Production]` | `carried_out_by`, `timespan`, `took_place_at`, `technique`, `classified_as` | — |

Several `classification` slots may share one path; they partition its values by Value Set (Type of Work, Style, and Shape all live in `classified_as`). Values that no slot's Value Set claims are shown in the Other Classifications bucket and are never dropped (decision C5). Sub-fields that need their own Value Set, such as a dimension's `unit`, find it through the binding for `<slot path>.unit`.

## Value Sets and bindings

Any YAML file in `vocab/` may hold `value_sets`, `bindings`, or both.

```yaml
value_sets:
  - id: vocab:identifier_types          # vocab:<snake_case>, unique across all files
    version: 1                          # integer, bump on every change
    label: {en: Identifier types}
    scope_note: {en: ...}               # optional
    mode: enumerated                    # enumerated | rule
    enforcement: strict                 # strict | extensible | advisory
    terms:                              # required for enumerated; list order is position
      - uri: http://vocab.getty.edu/aat/300312355
        label: {en: Accession Number}   # optional curated label
        scope_note: {en: ...}           # optional
        is_default: true                # optional, default false
        deprecated: false               # optional, default false; a deprecated term is never default
        flags: {unique: true}           # optional; unique identifier types are cleared on clone
  - id: vocab:type_of_work
    version: 1
    label: {en: Type of Work}
    mode: rule
    enforcement: extensible
    rule:                               # required for rule; all four keys, [] for none
      metatype_uris: [http://vocab.getty.edu/aat/300435443]
      broader_roots: []
      exclusions: []
      classes: []                       # Type | Language | Material | MeasurementUnit | Currency
    # terms: optional for rule sets; when present it is the pinned shortlist
bindings:
  - class: HumanMadeObject
    path: classified_as
    value_set: vocab:type_of_work
    enforcement: advisory               # optional; overrides the set's level for this binding
```

When `terms` is present it must be non-empty. URIs are absolute `http(s)` IRIs; prefixed names such as `aat:300404670` are rejected. A rule set needs at least one non-empty `metatype_uris`, `broader_roots`, or `classes`.

Enforcement compiles into validation (spec §7.3): `strict` is a violation that blocks publish, `extensible` a warning, `advisory` info.

A binding's `path` addresses the URI values being checked. For a `classification` slot it equals the slot path. For node widgets it is the slot path plus `.classified_as` (`identified_by[Name].classified_as`, `dimension.classified_as`) or another reference key (`dimension.unit`). Several Value Sets may bind to the same `(class, path)`: Gender, Occupation, and Nationality all bind to `Person` `classified_as` (spec §7.2). See `docs/questions.md`, Step 0.12, for how that interacts with Step 2.1's table constraint.

### Mapping to the vocabulary tables (Phase 2, Step 2.1)

| YAML | Table column |
| :-- | :-- |
| `value_sets[].id`, `label`, `scope_note`, `mode`, `enforcement`, `version` | `vocabularies` columns of the same names |
| `value_sets[].rule` | `vocabularies.rule` (JSONB with the same four keys) |
| `terms[]` list order | `vocabulary_terms.position` |
| `terms[].uri`, `is_default`, `deprecated`, `scope_note` | `vocabulary_terms` columns of the same names |
| `terms[].label` | `vocabulary_terms.label_override` |
| `terms[].flags` | `vocabulary_terms.flags` (Step 2.10) |
| `bindings[].class`, `path`, `value_set`, `enforcement` | `slot_vocabulary_bindings.entity_class`, `property_path`, `vocabulary_id`, `enforcement` (absent = null = inherit) |
