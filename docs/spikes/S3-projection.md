# Spike S3: JSON-LD ↔ Y.Doc projection round-trip

Phase 0, Step 0.8. Prototype and tests: `scripts/spikes/s3_projection/`. Library: `pycrdt` 0.14.13
(Python 3.12); cross-checked against Yjs 13.6.33 (Node 26).

Implemented by: Claude Fable 5.2. Reviewed by: Claude Fable 5.2 (separate, independent review;
PR #105). The review changed rules R6, R7, R11, R13 and R14 and added R19; section 3 lists what
changed and why.

## Result: GO

The keyed-entry representation with fractional `_order` round-trips **244 of 244** golden corpus
files exactly, and concurrent edits converge without losing inserts, including two clients that
each add the first value of a property that did not exist. The guide's draft rules were not
sufficient on their own: applied literally they fail 17 corpus files (15 through integer/float
drift, 2 through silent deduplication), leave several shapes undefined, and lose one client's
work when two clients create the same array concurrently. The rule list below closes those gaps.
It is the specification for Phase 1 Step 1.4 (Python) and Step 1.17 (TypeScript). The
refinements to register entries B2 and B3 are recorded in ADR-0018 (Accepted).

## 1. Final rule list

Terms: the **record URI** is the root `id`. A **shared** value is a Y type (`Map`, `Text`); a
**plain** value is a JSON value stored inside a `Map` without being a Y type. `plain(v)` is the
encoding of rule R9. **Key text** of a list of strings is its JSON text with no whitespace and
non-ASCII characters verbatim: `JSON.stringify(parts)` in JavaScript,
`json.dumps(parts, ensure_ascii=False, separators=(",", ":"))` in Python. The two produce the
same text for every well-formed Unicode string (checked, section 4.4).

Rules R20–R25, and a refinement of R7, were added by Step 1.4 for the path operations; they
follow R19.

**R1. Root.** The `Doc` has exactly one root type: a `Map` named `record`. The root JSON object is
an object map (R5) whatever its keys. `hydrate(jsonld, record_uri)` takes the record URI
explicitly; the spike defaults it to the root `id` and fails with `LAE-PROJ-004` if there is none.

**R2. Hydrate once.** A document is hydrated once per draft epoch, by the server; every other
replica receives that state through sync. Two independent hydrations of the same JSON-LD must
never be merged: each mints different node IRIs, and each creates its own shared maps under the
same keys, which are last-writer-wins.

**R3. Classification.** Every JSON value is exactly one of:

- a **reference**: an object whose `id` is a string and which has no keys beyond `id`, `type`,
  `_label`;
- a **node**: an object that has the key `type`, and whose `id` is absent or a string (and which is
  not a reference);
- **opaque**: everything else. That is: scalars; objects with no `type` that are not references;
  objects whose `id` is not a string; arrays nested directly inside arrays; and the value of
  `@context` at any depth, whatever its form.

Only the Linked Art aliases `id` and `type` are recognized. An object using `@id` / `@type`, or a
JSON-LD value object (`{"@value": …}`), is opaque and round-trips verbatim.

**R4. Internal node IRIs.** Every node other than the root that has no `id` gets
`<record_uri>/node/<uuidv7>` (canonical lower-case UUID) under the key `id`. This includes
single-valued nodes such as `timespan`. A node that already has an `id` keeps it and is keyed by
it; it is never minted over. References never get one. Nothing inside an opaque value gets one.
`assign_node_iris` returns a copy and does not mutate its input.

**R5. Object maps.** A reference or a node becomes a shared `Map`, its **object map**, holding
its properties as laid out in R6, each value stored by R8. The keys `_order` and `_value` are
reserved: a reference or node (or the root) that already has one is rejected with
`LAE-PROJ-001`. (Inside opaque values they are ordinary keys.) The `id` of an object map is
immutable: to point a reference at a different target, replace the whole map (single-valued
property) or remove the entry and add another (array). Editing `id` in place would let two
concurrent edits combine one client's `id` with another's `_label`.

**R6. Keys of an object map; arrays.** An array is **never a shared type of its own**. The items
of a non-empty array that is the value of property `p` are stored as **entries** directly in the
object map of the object that has `p`, beside that object's other keys. An object map therefore
has two kinds of key:

| Kind | Map key | Value |
| :-- | :-- | :-- |
| **property key**, for a property whose value is not a non-empty array | the JSON key `k` itself; or, if `k` starts with `[`, the key text of `[k]` | R8 |
| **entry key**, one per item of a non-empty array | the key text of `[p, itemKey]`, with `itemKey` from R7 | an entry |

So a map key that starts with `[` is always the JSON text of an array of one string (an escaped
property key) or two strings (an entry key), and it is parsed with a JSON parser; any other map
key is a property key, verbatim. A map key that starts with `[` and is not such an array cannot
be produced by these rules (`LAE-PROJ-005`). Examples:
`["identified_by","https://example.org/id/1/node/0192f0c1-…"]`,
`["classified_as","http://vocab.getty.edu/aat/300404670"]`.

An entry is always a shared `Map`:

- for a reference or node item, its object map (R5);
- for an opaque item, a `Map` with the single key `_value` holding `plain(item)`.

Every entry also carries `_order`, a float equal to the item's array index at hydration (`0.0`,
`1.0`, …). Items are classified one by one, so arrays mixing references, nodes and opaque values
need no special case. The entries of property `p` of an object are the keys of its object map
whose parsed form is `[p, …]`; that set is "the array", and R12–R14 say "the entries of `p`" for
it.

Why arrays are not nested maps: a nested map has to be created by some client before its first
entry can be added, and two maps created concurrently under one key are last-writer-wins, so one
client's map and every entry in it is lost (`test_a_nested_container_created_concurrently_loses_one`).
With entries stored in the parent's map, adding the first item of a property is the same
operation as adding any other item: one new key in a map that already exists on every replica.
No rule here ever creates a shared map whose only purpose is to hold entries.

**R7. Item keys.** The base item key of an array item is:

| Item | Base item key |
| :-- | :-- |
| reference or node | its `id` (after R4) |
| string `s` | `=s:` + `s` |
| number | `=n:` + its JSON literal (`=n:1`, `=n:1.0`, `=n:33.5`) |
| `true` / `false` | `=b:true` / `=b:false` |
| `null` | `=z` |
| array or object | `=j:` + its JSON text (R9) |

Items are processed in array order. An item takes its base item key if no earlier item of the
same array has it; otherwise the first unused of `<base> #2`, `<base> #3`, … (a space, `#`, and a
decimal counter). **Duplicates are therefore preserved**, each as its own entry, at its original
position. After hydration a key is only an identity for merging: projection reads the property
name from an entry key and nothing else, and an entry's own `id` field is the authoritative
identifier.

Operations never create duplicates. Inserting a reference or opaque value into an array that
already holds that item under its base item key (an entry with the same `id`, or with an equal
`_value`) is a no-op, which is what makes two clients adding the same reference converge on one
entry. If the base item key is held by a different item (possible only when an `id` or string
itself ends in ` #2` and so on), the insert takes the next unused suffix. Nodes added by
operations get a fresh IRI, so they never collide. A duplicate that arrives by import is a QA
finding, not a hydration error.

Portability: the `=n:` and `=j:` keys embed Python's number formatting, which JavaScript cannot
reproduce (`1e-07` against `1e-7`, `1.0` against `1`). Hydration is server-side Python, so
round-tripping is unaffected. A TypeScript client that inserts a number or opaque value into an
array uses its own formatting; the only consequence is that the same non-integral number added
concurrently by a Python and a TypeScript client becomes two entries. Linked Art has no arrays
of scalars (none in the corpus), so Step 1.17 need not offer that operation at all.

**R8. Property values.** The value `v` of key `k` in an object is stored by the first rule that
matches:

1. `k` is `@context`: `plain(v)` under the property key.
2. `k` is `content`, `v` is a string, and the object's `type` is exactly one of the strings
   `Name`, `Identifier`, `LinguisticObject`: a shared `Text` under the property key. (A `type`
   that is an array never matches. `content` on the root counts.)
3. `v` is a reference or node: an object map under the property key, with no `_order`.
4. `v` is a non-empty array: one entry per item (R6); there is no property key for `k`.
5. Otherwise (scalar, empty array, opaque object): `plain(v)` under the property key.

**R9. Plain values.** `pycrdt` and Yjs carry numbers as IEEE doubles: an integer written to a
`Map` reads back as a float, also inside nested plain objects. So:

| JSON value | Stored as |
| :-- | :-- |
| string, `true`, `false`, `null` | itself |
| integer with \|n\| ≤ 2^53 | a Y number (the double `float(n)`) |
| finite float with a fractional part | a Y number |
| any other number: a float with an integral value (`33.0`, `-0.0`, `1e21`), an integer beyond ±2^53 | `{"_json": "<literal>"}` |
| array or object | `{"_json": "<JSON text>"}` |

The wrapper is a plain (not shared) object with the single key `_json`. At hydration its text is
JSON with keys sorted, no whitespace, non-ASCII characters verbatim, and shortest-round-trip
number formatting (Python `json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).
That canonical form matters only for the `=j:` item key (R7); a decoder accepts any JSON text.

Decoding: a `_json` wrapper is parsed with a parser that keeps integers and floats apart (Python
`json.loads`); a Y number with an integral value and magnitude ≤ 2^53 is an integer; any other Y
number is a float. Consequences: the only plain objects in a document are `_json` wrappers; plain
arrays never appear; and a number typed in the browser (always a double) projects as an integer
exactly when it is integral.

**R10. Rejected input.** Hydration fails, without creating a document, for:

| Code (proposed) | Condition |
| :-- | :-- |
| `LAE-PROJ-001` | reserved key `_order` or `_value` on the root, a node, or a reference |
| `LAE-PROJ-002` | a number that is NaN or infinite, or an integer outside −2^63 … 2^64−1 (the range `orjson` can serialize) |
| `LAE-PROJ-003` | a string or key that is not valid Unicode (a lone surrogate); `pycrdt` panics on these |
| `LAE-PROJ-004` | no record URI |
| `LAE-PROJ-005` | (projection) a value or map key in the document that these rules cannot have produced |
| `LAE-PROJ-006` | (operation) an insert into a property that holds a single value (R14) |

No corpus file triggers any of them. No JSON key is rejected for its spelling other than the two
reserved ones: keys that start with `[` are escaped (R6), not refused.

**R11. Projection.** `project(doc)` walks the `record` map. An object map projects to an object:

- Each map key is parsed (R6) into a property name and, for an entry key, an item key.
- A property key projects to that property: a `Text` to its string, a shared `Map` to an object
  (recursively), a plain value decoded by R9. The keys `_order` and `_value` are skipped.
- The entries of each property `p` project to an array under `p`, in R13 order. An entry that has
  `_value` projects to the decoded value; any other entry projects as an object map.
- If a map has both a property key for `p` and entries of `p`, **the entries win** and the single
  value is ignored. Hydration never produces that state; concurrent edits can (R14), and this
  makes every replica project it the same way.
- Internal node IRIs are kept. `_order`, `_value`, `_json` and map keys never appear in output.

A property with no property key and no entries is absent. An empty source array is stored as
`plain([])` under the property key (R8.5), so `[]` round-trips, while an array whose last entry
is removed disappears from the JSON-LD, which is what Linked Art wants.

Whether a map key is an entry is decided by its text alone, never by inspecting values, so the
decision cannot change as values are edited.

**R12. `_order` arithmetic.** All values are IEEE 754 doubles; Python and JavaScript compute
identical results from these expressions.

- Hydration: `_order = index` (`0.0`, `1.0`, …).
- To place an entry between neighbours `a` (before) and `b` (after), in the projected order of
  the entries of one property:

  | Case | Candidate | Valid when |
  | :-- | :-- | :-- |
  | no entries | `0.0` | always |
  | no `a` (new first) | `b - 1.0` | candidate `< b` |
  | no `b` (append) | `a + 1.0` | candidate `> a` |
  | both | `(a + b) / 2.0` | `a < candidate < b` |

  The candidate must also be finite.
- **Rebalance.** If the candidate is not valid (the gap is exhausted, or `a == b` because two
  clients took the same midpoint), set `_order` of every entry of that property (and of no other
  property of the map) to its current projected index (`0.0`, `1.0`, …; write only the entries
  whose value changes), then compute the candidate again. Rebalance and insert happen in one
  transaction.
- `move_entry` sets the moved entry's `_order` to the candidate for its new neighbours (computed
  with the moved entry left out). `remove_entry` deletes the key and touches nothing else.
- Precision limits, measured: inserting repeatedly just after the first entry halves towards
  `0.0` and needs its first rebalance at the 1075th insert; inserting repeatedly just before the
  last entry halves towards the upper neighbour and needs one after 53 inserts, then after about
  `52 - log2(index)` more each time (47, 46, 45 in the test). The common trigger is the tie: two
  clients insert at the same position, and a later insert between those two entries rebalances.
- Every `_order` is its own last-writer-wins register. A rebalance concurrent with another
  client's insert, move or rebalance of the same property therefore converges and loses no
  entry, but the merged order can differ from what either client saw: the other client's entry
  may land at a different position than intended, and if two clients rebalance concurrently
  while holding different sets of entries, entries that neither of them moved can change places.
  Accepted: it needs two concurrent edits at an exhausted or tied position, nothing is lost, and
  the result is the same on every replica.

**R13. Sort order.** The entries of a property sort by `_order` ascending, then by the **whole
entry key** (the map key, R6) in **Unicode code-point order**. An entry without a numeric
`_order` cannot be produced (`LAE-PROJ-005`). Python `str` comparison is code-point order.
JavaScript `<` and `Array.prototype.sort` compare UTF-16 code units, which differs for
characters above U+FFFF, so the TypeScript mirror must compare by code point
(`scripts/spikes/s3_projection/js_interop/interop.mjs` has a comparator). Keys are usually IRIs,
but they include arbitrary source strings (R7), so this is not theoretical.

**R14. Adding the first item, removing the last, and single values.** There is no "create the
array" step, for any property, profiled or not:

- `insert` into property `p` of an object writes one entry key into that object's map (R6, R12).
  When `p` has no entries yet the candidate `_order` is `0.0`. Two or more clients doing this
  concurrently write different keys of the same existing map, so **every first insert is kept**
  (`test_concurrent_first_inserts_into_an_absent_property_are_all_kept`: root, entry,
  single-valued node and nested entry; two and three clients; every delivery order).
- If the map has a property key for `p`: when it holds the empty-array marker `plain([])`, or
  when `p` already has entries (so the value is already ignored by R11), the insert deletes that
  key in the same transaction. Otherwise `p` holds a single value and the insert is rejected
  with `LAE-PROJ-006`; changing a property from single-valued to an array is an explicit,
  separate operation (Step 1.4 or the normalization in Step 1.5), never a side effect.
- Removing an entry deletes its key. Removing the last one leaves the property absent. A client
  that removes every entry while another inserts one ends with that one entry.
- Implementations must never store an array as a nested shared map, and must never pre-create
  anything for a property that has no values.

A **single-valued** property (a `Text`, an object map with no `_order`, or a plain value under a
property key) is a last-writer-wins register. Two clients that each set it concurrently, for
instance each creating `timespan` on an activity that had none, converge on one client's whole
value; the other client's value, with everything inside it, is replaced. The result is never a
mixture of the two (`test_concurrent_creation_of_a_single_valued_node_keeps_one_whole_value`).
That is what "single-valued" means and it is kept; `docs/questions.md` (Step 0.8) records it for
the owner. A client that writes a single value for `p` while another inserts an entry of `p`
converges on the array (R11); the next insert removes the ignored value.

**R15. Stripping.** An internal node IRI is exactly `<record_uri>/node/` followed by a canonical
lower-case UUID of version 7. `strip_internal_iris(jsonld, record_uri)` removes every `id` whose
value matches, at any depth except inside `@context`, and nothing else; the root `id` is never
removed. The namespace `<record_uri>/node/` is reserved: an imported `id` that matches the pattern
is treated as internal. Corpus records whose ids merely contain `/node/` (39 ids, e.g. every Yale
Library record URI) are unaffected.

**R16. Canonical form.** `canonical_json` is `orjson.dumps(d, option=OPT_SORT_KEYS)`. Object key
order is never stored and never significant; array order is always significant and preserved
exactly.

**R17. `_label`.** `_label` is an ordinary string value on references and on embedded nodes
alike. Projection never adds, moves or removes it. (Removing labels that are resolvable is Step
1.5's job, before hydration.)

**R18. Concurrency semantics that follow.** Verified in `test_concurrency.py`:

| Concurrent edits | Outcome |
| :-- | :-- |
| two inserts at the same position | both kept, adjacent, ordered by key; nothing else moves |
| first inserts into a property that did not exist (any property, any depth) | all kept |
| first inserts into an empty source array (`[]`) | all kept; the marker is removed |
| the same reference added twice to one array | one entry |
| remove an entry / edit or insert inside it | the removal wins |
| remove every entry / insert one | the inserted entry remains |
| two edits to one `Text` | merged character-wise |
| move / insert in the same array | both applied |
| two scalar writes to one key, two moves of one entry, or two values for one single-valued property | last writer wins, whole value |
| a single value written / an entry inserted, same property | the array (R11) |
| two rebalances of one property | converge, nothing lost, order as in R12 |

**R19. `Text` positions.** Positions passed to operations that edit a `Text` are counted in
**Unicode code points**. Each implementation converts to its library's unit: `pycrdt` counts
UTF-8 bytes (`len(s[:i].encode("utf-8"))`), Yjs counts UTF-16 code units. `pycrdt` does not
reject an offset that falls inside a character; the text silently lands elsewhere
(`test_pycrdt_text_offsets_are_utf8_bytes`), so the conversion is not optional.

### Rules added by Step 1.4 (path operations)

The spike specified the document and the entry primitives; Step 1.4 had to fix how the path
operations of `lae.projection.ops` behave, because the TypeScript mirror (Step 1.17) must leave
the same document behind. These rules are part of the specification. The shared cases are
`backend/tests/projection/fixtures/*.json` (format: `backend/tests/projection/fixture_runner.py`).

**R7, refined.** An insert is a no-op when *any* entry of the property already holds the item:
the entry under the base item key if it is that item, otherwise the first entry in R13 order that
is (a ` #n` duplicate left by an import, after the base entry was removed). The key of that entry
is returned. As first written, R7 looked only under the base item key, so adding a reference that
survived only as `<id> #2` created a second copy, against "operations never create duplicates".

**R20. Write paths.** Paths follow `profiles/README.md`. A write follows a single chain of
nodes: each segment before the last must address at most one value, and that value must be an
object map. More than one, or a value that is not an object map, is rejected with
`LAE-PROJ-007`. Every operation takes an optional `base` object map from which the path starts
(default: the root), which is how one entry of an array is edited: the entry is found by its
entry key (`get_entry`, `list_entries`) and passed as `base`; keys are never written into paths.
Entries are named by their **entry key** (the map key of R6); `add_node` and `add_reference`
return it. `get_path` follows the README's reading rules on the live document (entries in R13
order, entries before a single value as in R11) and continues into opaque values as plain JSON,
so it always equals evaluating the path on the projection.

**R21. Missing intermediate nodes.** Operations that write (`set_scalar`, `set_object`,
`add_node`, `add_reference`) create the nodes missing from the chain; operations that read or
remove never create anything. A missing node for segment `k[C]` is created as follows:

- `k` is one of `timespan`, `produced_by`, `created_by`, `destroyed_by`, `born`, `died`,
  `formed_by`, `dissolved_by`, `duration`, `paid_amount` (`rules.SINGLE_VALUED_CLASSES`): a
  single-valued node `{id, type}` under the property key, with `type` = `C` if the segment has a
  filter, else the class the model gives (`TimeSpan`, `Production`, `Creation`, `Destruction`,
  `Birth`, `Death`, `Formation`, `Dissolution`, `Dimension`, `MonetaryAmount`). If the property
  already holds anything (a value the segment's filter did not match, or entries) the write is
  rejected with `LAE-PROJ-007`: a path never overwrites a value it does not address.
- any other `k`: the segment must have a filter (`LAE-PROJ-007` otherwise), and the node
  `{id, type: C}` is appended as an entry of `k` by R12/R14 (so `LAE-PROJ-006` if `k` holds a
  single value).

Two clients that create the same single-valued intermediate concurrently converge on one whole
node (R14); two that create an array intermediate both keep theirs, after which the path
addresses two nodes and further writes through it need a `base`.

**R22. Class filter on the last segment.** For `add_node`, `add_reference` and `set_object` the
object takes the filter as its `type` when it has none, and is rejected (`LAE-PROJ-007`) when it
has a different one. `add_node` needs a node (R3: a `type`, and a string `id` if any).
`set_scalar` and `splice_text` reject a filter on the last segment. For `move_entry` and
`remove_entry` the filter is ignored: positions are among all entries of the property.

**R23. `set_scalar`.** The value is a string, number, boolean or `null`. If the key is
`content`, the value is a string and the parent's `type` is one of the three R8.2 types, the
target is a `Text`: an existing `Text` is edited with the single splice
`(index, delete, insert)` where `index` is the length of the longest common prefix of the old
and new strings and the longest common suffix of the remainders is kept, all counted in code
points (R19), delete first, then insert at the same position; equal strings write nothing; if
there is no `Text` yet, one is created. Every other value is stored as `plain(v)` under the
property key and is always written, also when equal to the stored value. Rejected with
`LAE-PROJ-007`: a property that has entries, and a property that holds an object map (use
`set_object` or `remove_value`). `splice_text(path, index, delete, insert)` is the same splice
given explicitly; a range outside the text is `LAE-PROJ-008`.

**R24. Single-valued objects.** `set_object(path, obj)` stores a node or reference under the
property key, replacing whatever single value was there (R5, R14); `remove_value(path)` deletes
the property key (with a filter on the last segment: only if the value is an object of that
class). Both reject a property that has entries (`LAE-PROJ-007`).

**R25. Order of effects.** Every operation is one transaction, and is checked completely before
it writes or mints: a rejected operation changes nothing and mints nothing, and so does a
no-op (R7; `move_entry` to the position the entry already has; `remove_entry` of an absent key,
which returns false). Internal node IRIs are minted in this order: the nodes of the object
being added, each nested node before the node that contains it and siblings in the object's key
order; then the missing intermediate nodes, outermost first. `move_entry(path, key, before_key)`
places the entry just before `before_key`, or last when it is null; an unknown key is
`LAE-PROJ-008`. `add_node` and `add_reference` take the same optional `before_key`.

Two codes join R10: `LAE-PROJ-007` (a path that cannot be written, or a value that does not fit
its target) and `LAE-PROJ-008` (an entry key or text range that does not exist).

Checked against Yjs 13.6.33 by hand (Node 26, not in CI): with the fixtures' client ids, two
concurrent writes of one key, a write against a removal, two concurrent single-valued nodes and
the concurrent `Text` splices of `text-code-points.json` resolve in Yjs exactly as the fixtures
record for `pycrdt`, and Yjs reads every fixture's `initial_update` to the recorded
`initial_state`.

## 2. Decisions on the cases the draft rules left open

| Case | Decision | Corpus example |
| :-- | :-- | :-- |
| Embedded node that already has an `id` | Keep it; key the entry by it; never mint over it (R4). 208 such nodes in the corpus. | `linkedart/object-0.json` `classified_as` |
| Same reference twice in one array | Preserved: second occurrence keyed `<id> #2` (R7). Making it a validation error is not an option: the corpus does it. | `institutional/yuag-lin-176060.json` and `yuag-lin-176699.json`, `classified_as` has `aat:300027221` twice |
| Same scalar, or same node `id`, twice | Same suffix rule. Not in the corpus; covered by the property test (30% of generated documents). | n/a |
| Array mixing references and embedded nodes | Per-item classification; no special rule (R6). 10 such arrays. | `linkedart/object-0.json` `member_of` |
| Array of scalars | Entries `{_value, _order}` keyed by a type-tagged key, so `1`, `1.0`, `"1"` and `true` stay distinct (R7). Not in the corpus; property test. | n/a |
| Plain object without `type`, in an array or single-valued | Opaque; stored as exact JSON text (R9). Not in the corpus; property test. | n/a |
| `@context` as string | Plain string. All 244 files, plus 3 nested `@context` keys. | `linkedart/object-0.json` |
| `@context` as array or object | Opaque JSON text, order exact; never entries (R3, R8.1). Not in the corpus; unit and property tests. | n/a |
| Numbers | Exact, via R9. Corpus has 21 integers and 25 floats, 5 of them integral (`33.0`). | `linkedart/digital-0.json` `dimension[0].value` = `2550`; `institutional/ycba-object-5497f1dd-….json` `dimension[3].value` = `33.0` |
| Booleans, `null` | Stored as themselves; never confused with `1`/`0`. Not in the corpus; unit and property tests. | n/a |
| `_label` on embedded nodes | Preserved verbatim (R17). 399 in the corpus. | `institutional/ycba-activity-2a201e0b-….json` `referred_to_by[0].classified_as[0]` |
| Empty array | Round-trips as `plain([])`; an array emptied by edits projects to no key (R11). Not in the corpus; unit and property tests. | n/a |
| Key order / array order | Key order irrelevant, array order exact (R16). | every file |
| Single-valued embedded node | Object map with an internal IRI and no `_order` (R4, R8.3). 147 in the corpus. | `institutional/ycba-activity-2a201e0b-….json` `timespan` |
| First value of a property that does not exist, added by two clients at once | Both kept: entries are keys of the parent's map, so nothing is created concurrently (R6, R14). | any record; tested on all 244 from JavaScript and Python together |
| JSON key that starts with `[` | Stored as the key text of `[key]` (R6); round-trips. Not in the corpus; unit and property tests. | n/a |

## 3. Differences from the guide's draft rules

1. **Numbers are not all "plain values".** `pycrdt` returns every integer as a float (`33` →
   `33.0`), so the literal rule fails 15 corpus files. R9 keeps integers and floats apart.
2. **Arrays are not keyed by bare `id` or bare string value.** That silently drops duplicates (2
   corpus files). R7 adds a deterministic suffix, and tags scalar keys with their type.
3. **Scalar entries need a wrapper** (`_value`), because an entry must be a `Map` to carry
   `_order`. `_value` joins `_order` as a reserved key.
4. **Untyped objects, nested arrays, empty arrays and `@context` are stored as JSON text**
   (`_json`), not as plain JSON values: plain values lose the integer/float distinction too.
5. **An array is not a `Map` of its own.** Its entries are keys of the parent object's map, named
   by property and item key (R6). The draft's nested `Map` per array loses one client's work when
   two clients add the first value of the same property concurrently; the draft did not address
   it.
6. **The tie-break is code-point order and there is a rebalance rule** (R12, R13); the draft said
   only "midpoint".
7. **`is_reference` requires a string `id`**, and `strip_internal_iris` needs the record URI and
   matches UUIDv7 exactly rather than any `/node/` path.

### What the review changed

The first version of this spike kept one nested `Map` per array (a "container") and proposed
pre-creating empty containers for every array property in the record's UI Profile. That closed
the concurrent-first-insert hole only for profiled properties at hydration time; properties
outside the profile, and every array inside a node created later, kept it. The review replaced
it with R6: no container exists, so the hole does not exist for any property at any depth. Other
changes made in review:

- Whether a map is an array is no longer inferred from its values ("every value is a `Map` with
  a numeric `_order`"); it is read from the key (R11).
- The set-semantics no-op compares the item, not only the key, so inserting a string that
  happens to equal another item's suffixed key is not silently dropped (R7).
- R12 states exactly what concurrent rebalances can do to order.
- R19 (`Text` positions) is new: the two libraries count in different units.
- Portability notes on number formatting in keys (R7) and on `_json` text (R9).
- The Yjs cross-check now also runs concurrent first inserts from JavaScript and Python.

A fully flat layout (every node in one root-level map keyed by IRI, with separate membership
entries) was considered and not chosen: it needs more than the one root `Map` that B2 fixes; an
embedded node that carries its own `id` can occur in several places of one record with
different content, so nodes could not be keyed by IRI alone; and removing a node would leave
its descendants behind as unreachable entries, where the nested layout deletes the subtree in
one operation and lets removal win over concurrent edits inside it.

## 4. Results

```
cd backend
uv run pytest -c pyproject.toml ../scripts/spikes/s3_projection -q      # 384 passed
uv run python ../scripts/spikes/s3_projection/report.py                 # the tables in 4.1
```

### 4.1 Corpus round-trip

`canonical_json(strip_internal_iris(project(hydrate(x)))) == canonical_json(x)`, plus, for each
file: the input is not mutated; every embedded node has an `id` after `project`; no source `id` is
mistaken for an internal IRI; a second replica built from the update projects identically; and
re-hydrating the projection mints nothing new.

| Subset | Files | Pass | Without R9 (plain values) | Without R7 suffix |
| :-- | --: | --: | --: | --: |
| `linkedart/` | 211 | **211** | 196 | 211 |
| `institutional/` | 33 | **33** | 33 | 31 |
| Total | 244 | **244** | 229 | 242 |

The 60 files in `tests/corpus/KNOWN_FAILURES.md` (JSON Schema mismatches) all round-trip.

| | `linkedart/` | `institutional/` |
| :-- | --: | --: |
| Arrays (properties with entries) | 1004 | 686 |
| … of references only | 582 | 427 |
| … of nodes only | 413 | 258 |
| … mixing both | 9 | 1 |
| … repeating an `id` | 0 | 2 |
| Array entries | 1072 | 877 |
| Embedded nodes | 571 | 443 |
| … with their own `id` | 143 | 65 |
| … with `_label` | 257 | 142 |
| Internal node IRIs minted | 428 | 378 |
| `Text` values | 167 | 209 |
| Integers / floats / integral floats | 21 / 11 / 2 | 0 / 14 / 3 |
| `_json`-encoded values | 2 | 3 |
| Hydrate / project, mean per file | 0.08 / 0.06 ms | 0.26 / 0.32 ms |
| Y update size ÷ canonical JSON size | 2.22 | 2.51 |

Reproduce with `uv run python ../scripts/spikes/s3_projection/report.py`.

### 4.2 Property tests

`test_property.py` generates Linked-Art-shaped records (nodes nested three deep, references,
scalars, opaque objects, every array mixture, deliberate duplicates, ids drawn from a small pool
that includes strings such as `x #2`, `=s:x`, `[`, `["a","b"]`, `_order` and the empty string,
and property names that look like entry keys) and checks the same five properties as the corpus
test.

| Run | Examples | Passing | Failing | Time |
| :-- | --: | --: | --: | --: |
| Default (every pytest run) | 300 | 300 | 0 | 2.4 s |
| Extended (`S3_MAX_EXAMPLES=5000`) | 5000 | 5000 | 0 | 48.4 s |

Hypothesis discarded a further 473 generated cases in the extended run before the test body ran
(26 in the default run). Share of generated cases in the extended run containing each shape:

| Shape | Share |
| :-- | --: |
| float with an integral value | 41.6% |
| empty array | 37.6% |
| array of opaque values only | 32.7% |
| array with an exact duplicate item | 29.6% |
| embedded node with its own `id` | 28.4% |
| array of nodes only / references only | 17.3% / 18.1% |
| array repeating an `id` | 16.4% |
| array mixing nodes and references | 7.8% |
| array mixing nodes and opaque values / references and opaque values / all three | 5.4% / 3.4% / 1.4% |
| `@context` as string / array / object / `null` | 46.0% / 15.5% / 15.2% / 7.5% |
| integer beyond 2^53 as a direct property value | 2.3% |

`test_random_concurrent_ops_converge` runs up to 30 random operations (insert at a position,
insert into one of two properties that no replica has at the start, insert inside an existing
entry, move, remove, `Text` edit, add reference, pairwise sync) over three replicas, delivers
everything in a random replica order, and checks that all three project identically, that no
insert is lost unless some client removed it or its parent entry, that removal always wins, and
that no reference is duplicated.

| Run | Examples | Passing | Failing | Time |
| :-- | --: | --: | --: | --: |
| Default | 300 | 300 | 0 | 0.3 s |
| Extended (`S3_MAX_EXAMPLES=5000`) | 5000 | 5000 | 0 | 6.1 s |

In the extended run 32.5% of generated cases included at least one mid-run sync, and in 15.2%
two or more clients made the first insert into the same absent property; Hypothesis discarded a
further 575 generated cases before the test body ran.

The tests were checked by mutation. Each of these makes at least one test fail: sorting by
`_order` without the key tie-break; keying duplicates without the suffix; making the repeated
insert of a reference replace the entry; not escaping property keys that start with `[`;
leaving the property name out of the entry key; rebalancing every entry of the map instead of
one property's.

### 4.3 Concurrency

The guide's test (`test_two_clients_insert_a_name_at_position_1`): both replicas project
`[n0, A|B, B|A, n1, n2]` identically; both inserts hold `_order` 0.5 and the entry key (which
ends in the node IRI) breaks the tie; no other entry moves. The same holds on
`linkedart/person-rembrandt-4.json` in both delivery orders.

Concurrent first inserts (`test_concurrent_first_inserts_into_an_absent_property_are_all_kept`,
32 parameter combinations, every delivery order of two and of three clients): each client adds a
new node, a reference shared with the other clients and a reference of its own to a property
that does not exist, at the root, inside an array entry, inside a single-valued node and inside
an entry of that node, including property names `[` and `["a","b"]`. Every replica projects the
same array; every node and every distinct reference is present once. The nested-map layout
fails the equivalent case (`test_a_nested_container_created_concurrently_loses_one`). The other
cases are in R18.

### 4.4 Cross-check against Yjs

`js_interop/` loads the `pycrdt` state of all 244 corpus files plus one synthetic record of edge
cases (including the keys `[`, `["a","b"]`, `__proto__` and `constructor`) into Yjs 13.6.33
under Node (manual run, steps in `js_interop/check_interop.py`; it needs `npm install yjs`, so it
is not in pytest):

| Check | Result |
| :-- | --: |
| A JavaScript mirror of `project()` returns the same document as Python (numbers compared as doubles) | 245 / 245 |
| State re-encoded by Yjs still round-trips exactly in Python (numbers and `_json` survive) | 245 / 245 |
| A `Name` inserted by JavaScript at position 1 with the R12 midpoint lands at position 1 in Python, and its integral number projects as an integer | 31 / 31 (records with two or more names) |
| JavaScript and Python each make the first insert into the same absent property: both entries present in both delivery orders, and the JavaScript and Python projections of the merged state are equal | 245 / 245 |
| Entry keys built with `JSON.stringify` equal the Python key text (8 pairs with quotes, backslashes, control characters, U+2028, non-BMP characters, empty strings) | yes |
| Code-point comparator gives the Python order | yes |

Not verified: a live browser session over `y-websocket` (that is Spike S5 and Step 1.17).

## 5. What Step 1.4 and Step 1.17 must take from this

- Implement R1–R19 as written; move `test_rules.py`, `test_property.py`, `test_concurrency.py`
  and `strategies.py` into `backend/tests/projection/` and keep every assertion.
- The Step 1.4 guide text predates this report. Where it says "keyed `Map`s for node arrays and
  reference arrays", read R6: entries in the parent's map. `rules.py` needs `VALUE_KEY = "_value"`
  and `JSON_KEY = "_json"` beside the constants the guide lists, the key functions
  (`property_key`, `entry_key`, `parse_key`), and `is_reference` must require a string `id`.
- `strip_internal_iris` needs the record URI (argument, or the root `id`).
- `ops`: `get_path` resolves an array step by listing the entries of the property (R6), not by
  fetching a child map. `add_node` / `add_reference` use R12 for the position, are no-ops for an
  item already present (R7), follow R14 for the property key, and run inside one transaction
  with any rebalance. `remove_entry` and `move_entry` take the entry key. `set_scalar` on a
  `Text` converts positions by R19.
- Nothing is pre-created at hydration for properties without values, and the UI Profile is not
  an input to `hydrate`.
- `project` must be total over every state the rules can reach, including the R11 "entries win"
  case. `LAE-PROJ-005` states can only come from a faulty or hostile client; Step 1.8 decides
  whether the server checks updates before persisting them.
- `canonical_json` returns `bytes` in Step 1.4; the spike returns `str` as the Step 0.8 text says.
- TypeScript: build keys with `JSON.stringify` and parse them with `JSON.parse` (R6); compare
  keys by code point (R13); never write a `BigInt`, a plain array or a plain object other than a
  `_json` wrapper; treat `_json` wrappers as read-only unless replacing the whole value; convert
  `Text` positions (R19). When projecting into plain objects, define keys with
  `Object.defineProperty` (or use a `Map`): assigning the key `__proto__` sets the prototype
  instead of storing the value. JavaScript cannot show `33.0` versus `33` or integers beyond
  2^53 exactly, which affects only the raw inspector's display, never the stored value.
- Observers: an object map's change event now covers both its own properties and its array
  entries; `useYPath` filters changed keys by parsed property name.
- The semantic diff (Step 1.14, register B10) matches entries by entry key, not by `id`, so that
  duplicated references (R7) diff correctly.
