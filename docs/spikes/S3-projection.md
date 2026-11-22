# Spike S3: JSON-LD ↔ Y.Doc projection round-trip

Phase 0, Step 0.8. Prototype and tests: `scripts/spikes/s3_projection/`. Library: `pycrdt` 0.14.13
(Python 3.12); cross-checked against Yjs 13.6.33 (Node 26).

## Result: GO

The keyed-map representation with fractional `_order` round-trips **244 of 244** golden corpus
files exactly, and concurrent edits converge without losing inserts. The guide's draft rules were
not sufficient on their own: applied literally they fail 17 corpus files (15 through integer/float
drift, 2 through silent deduplication) and leave several shapes undefined. The rule list below
closes those gaps. It is the specification for Phase 1 Step 1.4 (Python) and Step 1.17
(TypeScript). The refinements to register entries B2 and B3 are proposed in ADR-0018 and need a
human acknowledgement (`docs/questions.md`, Step 0.8).

## 1. Final rule list

Terms: the **record URI** is the root `id`. A **shared** value is a Y type (`Map`, `Text`); a
**plain** value is a JSON value stored inside a `Map` without being a Y type. `plain(v)` is the
encoding of rule R9.

**R1. Root.** The `Doc` has exactly one root type: a `Map` named `record`. The root JSON object is
an object map (R5) whatever its keys. `hydrate(jsonld, record_uri)` takes the record URI
explicitly; the spike defaults it to the root `id` and fails with `LAE-PROJ-004` if there is none.

**R2. Hydrate once.** A document is hydrated once per draft epoch, by the server; every other
replica receives that state through sync. Two independent hydrations of the same JSON-LD must
never be merged: each mints different node IRIs, and concurrently created root keys are
last-writer-wins.

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

**R5. Object maps.** A reference or a node becomes a shared `Map` with one key per JSON key, each
value stored by R8. The keys `_order` and `_value` are reserved: a reference or node (or the root)
that already has one is rejected with `LAE-PROJ-001`. (Inside opaque values they are ordinary
keys.) The `id` of an object map is immutable: to point a reference at a different target, replace
the whole map (single-valued property) or remove the entry and add another (array). Editing `id`
in place would let two concurrent edits combine one client's `id` with another's `_label`.

**R6. Arrays.** A non-empty array that is the value of a property becomes a shared `Map`, the
**container**, with one **entry** per item. An entry is always a shared `Map`:

- for a reference or node item, its object map (R5);
- for an opaque item, a `Map` with the single key `_value` holding `plain(item)`.

Every entry also carries `_order`, a float equal to the item's array index (`0.0`, `1.0`, …).
Items are classified one by one, so arrays mixing references, nodes and opaque values need no
special case.

**R7. Entry keys.** The base key of an entry is:

| Item | Base key |
| :-- | :-- |
| reference or node | its `id` (after R4) |
| string `s` | `=s:` + `s` |
| number | `=n:` + its JSON literal (`=n:1`, `=n:1.0`, `=n:33.5`) |
| `true` / `false` | `=b:true` / `=b:false` |
| `null` | `=z` |
| array or object | `=j:` + its JSON text (R9) |

Items are processed in array order. An item takes its base key if no earlier entry of the same
container has it; otherwise the first unused of `<base> #2`, `<base> #3`, … (a space, `#`, and a
decimal counter). **Duplicates are therefore preserved**, each as its own entry. After hydration a
key is only an identity for merging: projection never parses keys, and an entry's own `id` field
is the authoritative identifier. Operations never create duplicates: inserting a reference or
opaque value whose base key is already present is a no-op, which is what makes two clients adding
the same reference converge on one entry. (Nodes added by operations always get a fresh IRI, so
they never collide.) A duplicate that arrives by import is a QA finding, not
a hydration error.

**R8. Property values.** The value `v` of key `k` in an object map is stored by the first rule
that matches:

1. `k` is `@context`: `plain(v)`.
2. `k` is `content`, `v` is a string, and the object's `type` is exactly one of the strings
   `Name`, `Identifier`, `LinguisticObject`: a shared `Text`. (A `type` that is an array never
   matches. `content` on the root counts.)
3. `v` is a reference or node: an object map, with no `_order`.
4. `v` is a non-empty array: a container (R6).
5. Otherwise (scalar, empty array, opaque object): `plain(v)`.

**R9. Plain values.** `pycrdt` and Yjs carry numbers as IEEE doubles: an integer written to a
`Map` reads back as a float, also inside nested plain objects. So:

| JSON value | Stored as |
| :-- | :-- |
| string, `true`, `false`, `null` | itself |
| integer with \|n\| ≤ 2^53 | a Y number (the double `float(n)`) |
| finite float with a fractional part | a Y number |
| any other number: a float with an integral value (`33.0`, `-0.0`, `1e21`), an integer beyond ±2^53 | `{"_json": "<literal>"}` |
| array or object | `{"_json": "<JSON text>"}` |

The wrapper is a plain (not shared) object with the single key `_json`. Its text is JSON with keys
sorted, no whitespace, non-ASCII characters verbatim, and shortest-round-trip number formatting
(Python `json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).

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
| `LAE-PROJ-005` | (projection) a value in the document that these rules cannot have produced |

No corpus file triggers any of them.

**R11. Projection.** `project(doc)` walks the `record` map:

- A `Text` projects to its string.
- A shared `Map` is a **container** if every one of its values is a shared `Map` that has a
  numeric `_order` (vacuously true for an empty map); otherwise it is an object map.
- An object map projects to an object: every key except `_order`, values projected recursively. A
  key whose value is an **empty container is omitted**.
- A non-empty container projects to an array of its entries in R13 order. An entry that has
  `_value` projects to the decoded value; any other entry projects as an object map.
- A plain value is decoded by R9.
- Internal node IRIs are kept. `_order`, `_value`, `_json` and entry keys never appear in output.

The container test cannot misfire on hydrated data: after R4 every object map except the root has
a string `id`, which is not a `Map`, and the root is never tested. An empty source array is stored
as `plain([])` (R8.5), not as an empty container, so `[]` round-trips while a container emptied by
edits disappears from the JSON-LD, which is what Linked Art wants.

**R12. `_order` arithmetic.** All values are IEEE 754 doubles; Python and JavaScript compute
identical results from these expressions.

- Hydration: `_order = index` (`0.0`, `1.0`, …).
- To place an entry between neighbours `a` (before) and `b` (after), in projected order:

  | Case | Candidate | Valid when |
  | :-- | :-- | :-- |
  | container empty | `0.0` | always |
  | no `a` (new first) | `b - 1.0` | candidate `< b` |
  | no `b` (append) | `a + 1.0` | candidate `> a` |
  | both | `(a + b) / 2.0` | `a < candidate < b` |

  The candidate must also be finite.
- **Rebalance.** If the candidate is not valid (the gap is exhausted, or `a == b` because two
  clients took the same midpoint), set `_order` of every entry of the container to its current
  projected index (`0.0`, `1.0`, …; write only the entries whose value changes), then compute the
  candidate again. Rebalance and insert happen in one transaction.
- `move_entry` sets the moved entry's `_order` to the candidate for its new neighbours (computed
  with the moved entry left out). `remove_entry` deletes the key and touches nothing else.
- Precision limits, measured: inserting repeatedly just after the first entry halves towards
  `0.0` and needs its first rebalance at the 1075th insert; inserting repeatedly just before the
  last entry halves towards the upper neighbour and needs one after 53 inserts, then after about
  `52 - log2(index)` more each time (47, 46, 45 in the test). A rebalance is therefore rare and
  costs one small write per entry.
- A rebalance concurrent with another client's insert converges (every `_order` is a
  last-writer-wins register) and loses nothing, but the other client's entry may land at a
  different position than intended, because its `_order` was computed on the old scale. Accepted.

**R13. Sort order.** Entries sort by `_order` ascending, then by entry key in **Unicode code-point
order**. An entry without a numeric `_order` cannot exist in a container (R11). Python `str`
comparison is code-point order. JavaScript `<` and `Array.prototype.sort` compare UTF-16 code
units, which differs for characters above U+FFFF, so the TypeScript mirror must compare by code
point (`scripts/spikes/s3_projection/js_interop/interop.mjs` has a comparator). Keys are
usually IRIs, but they include arbitrary source strings (R7), so this is not theoretical.

**R14. Creating a container.** A property that is absent has no container. If two clients each
create it concurrently, the two `Map`s are last-writer-wins and **one client's first insert is
lost** (reproduced in `test_concurrent_first_insert_into_an_absent_property_loses_one`). The
fix, shown to work in `test_precreated_empty_container_keeps_both_first_inserts`, is that the
container exists before clients diverge: empty containers are invisible in the projection (R11),
so creating them changes no round-trip. Recommended for Step 1.4: hydration pre-creates an empty
container for every array property the record's UI Profile declares. A first insert into a
property that has no container even then (an unprofiled property) keeps the last-writer-wins
risk; that residue is listed in `docs/questions.md`. Operations must never delete an emptied
container.

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
| the same reference added twice | one entry |
| remove an entry / edit inside it | the removal wins |
| two edits to one `Text` | merged character-wise |
| move / insert in the same container | both applied |
| two scalar writes to one key, or two moves of one entry | last writer wins |

## 2. Decisions on the cases the draft rules left open

| Case | Decision | Corpus example |
| :-- | :-- | :-- |
| Embedded node that already has an `id` | Keep it; key the entry by it; never mint over it (R4). 208 such nodes in the corpus. | `linkedart/object-0.json` `classified_as` |
| Same reference twice in one array | Preserved: second occurrence keyed `<id> #2` (R7). Making it a validation error is not an option: the corpus does it. | `institutional/yuag-lin-176060.json` and `yuag-lin-176699.json`, `classified_as` has `aat:300027221` twice |
| Same scalar, or same node `id`, twice | Same suffix rule. Not in the corpus; covered by the property test (33% of generated documents). | n/a |
| Array mixing references and embedded nodes | Per-item classification; no special rule (R6). 10 such arrays. | `linkedart/object-0.json` `member_of` |
| Array of scalars | Entries `{_value, _order}` keyed by a type-tagged key, so `1`, `1.0`, `"1"` and `true` stay distinct (R7). Not in the corpus; property test. | n/a |
| Plain object without `type`, in an array or single-valued | Opaque; stored as exact JSON text (R9). Not in the corpus; property test. | n/a |
| `@context` as string | Plain string. All 244 files, plus 3 nested `@context` keys. | `linkedart/object-0.json` |
| `@context` as array or object | Opaque JSON text, order exact; never a container (R3, R8.1). Not in the corpus; unit and property tests. | n/a |
| Numbers | Exact, via R9. Corpus has 21 integers and 25 floats, 5 of them integral (`33.0`). | `linkedart/digital-0.json` `dimension[0].value` = `2550`; `institutional/ycba-object-5497f1dd-….json` `dimension[3].value` = `33.0` |
| Booleans, `null` | Stored as themselves; never confused with `1`/`0`. Not in the corpus; unit and property tests. | n/a |
| `_label` on embedded nodes | Preserved verbatim (R17). 399 in the corpus. | `institutional/ycba-activity-2a201e0b-….json` `referred_to_by[0].classified_as[0]` |
| Empty array | Round-trips as `plain([])`; an emptied container projects to no key (R11). Not in the corpus; unit and property tests. | n/a |
| Key order / array order | Key order irrelevant, array order exact (R16). | every file |
| Single-valued embedded node | Object map with an internal IRI and no `_order` (R4, R8.3). 147 in the corpus. | `institutional/ycba-activity-2a201e0b-….json` `timespan` |

## 3. Differences from the guide's draft rules

1. **Numbers are not all "plain values".** `pycrdt` returns every integer as a float (`33` →
   `33.0`), so the literal rule fails 15 corpus files. R9 keeps integers and floats apart.
2. **Arrays are not keyed by bare `id` or bare string value.** That silently drops duplicates (2
   corpus files). R7 adds a deterministic suffix, and tags scalar keys with their type.
3. **Scalar entries need a wrapper** (`_value`), because an entry must be a `Map` to carry
   `_order`. `_value` joins `_order` as a reserved key.
4. **Untyped objects, nested arrays, empty arrays and `@context` are stored as JSON text**
   (`_json`), not as plain JSON values: plain values lose the integer/float distinction too.
5. **An empty container projects to an absent key**, which makes pre-created containers possible
   (R14); the draft did not say how concurrent first inserts are kept.
6. **The tie-break is code-point order and there is a rebalance rule** (R12, R13); the draft said
   only "midpoint".
7. **`is_reference` requires a string `id`**, and `strip_internal_iris` needs the record URI and
   matches UUIDv7 exactly rather than any `/node/` path.

## 4. Results

```
cd backend
uv run pytest -c pyproject.toml ../scripts/spikes/s3_projection -q      # 326 passed
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
| Arrays (all become containers) | 1004 | 686 |
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
| Hydrate / project, mean per file | 0.09 / 0.07 ms | 0.22 / 0.30 ms |
| Y update size ÷ canonical JSON size | 2.25 | 2.52 |

Reproduce with `uv run python ../scripts/spikes/s3_projection/report.py`.

### 4.2 Property tests

`test_property.py` generates Linked-Art-shaped records (nodes nested three deep, references,
scalars, opaque objects, every array mixture, deliberate duplicates, ids drawn from a small pool
that includes strings such as `x #2`, `=s:x`, `_order` and the empty string) and checks the same
five properties as the corpus test.

| Run | Examples | Passing | Failing | Time |
| :-- | --: | --: | --: | --: |
| Default (every pytest run) | 300 | 300 | 0 | 3.5 s |
| Extended (`S3_MAX_EXAMPLES=5000`) | 5000 | 5000 | 0 | 51.9 s |

Hypothesis discarded a further 521 generated cases in the extended run before the test body ran
(34 in the default run). Share of generated cases in the extended run containing each shape:

| Shape | Share |
| :-- | --: |
| float with an integral value | 42.1% |
| empty array | 40.0% |
| array of opaque values only | 34.3% |
| array with an exact duplicate item | 32.6% |
| embedded node with its own `id` | 31.0% |
| array of nodes only / references only | 18.8% / 17.9% |
| array repeating an `id` | 16.1% |
| array mixing nodes and references | 7.6% |
| array mixing nodes and opaque values / references and opaque values / all three | 6.3% / 3.6% / 2.3% |
| `@context` as string / array / object / `null` | 44.5% / 17.2% / 13.9% / 7.2% |
| integer beyond 2^53 as a direct property value | 3.0% |

`test_random_concurrent_ops_converge` runs up to 30 random operations (insert at a position,
move, remove, `Text` edit, add reference, pairwise sync) over three replicas, delivers everything
in a random replica order, and checks that all three project identically, that no insert is lost
unless some client removed it, that removal always wins, and that no reference is duplicated.

| Run | Examples | Passing | Failing | Time |
| :-- | --: | --: | --: | --: |
| Default | 300 | 300 | 0 | 0.3 s |
| Extended (`S3_MAX_EXAMPLES=5000`) | 5000 | 5000 | 0 | 4.8 s |

In the extended run 38.8% of generated cases included at least one mid-run sync; Hypothesis
discarded a further 627 generated cases before the test body ran.

The tests were checked by mutation: sorting by `_order` without the key tie-break, keying
duplicates without the suffix, and making the repeated insert of a reference replace the entry
each make at least one test fail.

### 4.3 Concurrency

The guide's test (`test_two_clients_insert_a_name_at_position_1`): both replicas project
`[n0, A|B, B|A, n1, n2]` identically; both inserts hold `_order` 0.5 and the node IRI breaks the
tie; no other entry moves. The same holds on `linkedart/person-rembrandt-4.json` in both delivery
orders. The other cases are in R18 and R14.

### 4.4 Cross-check against Yjs

`js_interop/` loads the `pycrdt` state of all 244 corpus files plus one synthetic record of edge
cases into Yjs 13.6.33 under Node (manual run, steps in `js_interop/check_interop.py`; it needs `npm install yjs`, so it is not in pytest):

| Check | Result |
| :-- | --: |
| A JavaScript mirror of `project()` returns the same document as Python (numbers compared as doubles) | 245 / 245 |
| State re-encoded by Yjs still round-trips exactly in Python (numbers and `_json` survive) | 245 / 245 |
| A `Name` inserted by JavaScript at position 1 with the R12 midpoint lands at position 1 in Python, and its integral number projects as an integer | 31 / 31 (records with two or more names) |
| Code-point comparator gives the Python order | yes |

Not verified: a live browser session over `y-websocket` (that is Spike S5 and Step 1.17).

## 5. What Step 1.4 and Step 1.17 must take from this

- Implement R1–R17 as written; move `test_rules.py`, `test_property.py`, `test_concurrency.py`
  and `strategies.py` into `backend/tests/projection/` and keep every assertion.
- `rules.py` needs `VALUE_KEY = "_value"` and `JSON_KEY = "_json"` beside the constants the guide
  lists, and `is_reference` must require a string `id`.
- `strip_internal_iris` needs the record URI (argument, or the root `id`).
- `add_node` / `add_reference` use R12 for the position, are no-ops for an existing reference
  (R7), and run inside one transaction with any rebalance. Container creation follows R14.
- `canonical_json` returns `bytes` in Step 1.4; the spike returns `str` as the Step 0.8 text says.
- TypeScript: compare keys by code point (R13); never write a `BigInt`, a plain array or a plain
  object other than a `_json` wrapper; treat `_json` wrappers as read-only unless replacing the
  whole value. JavaScript cannot show `33.0` versus `33` or integers beyond 2^53 exactly, which
  affects only the raw inspector's display, never the stored value.
- The semantic diff (Step 1.14, register B10) matches entries by key, not by `id`, so that
  duplicated references (R7) diff correctly.
