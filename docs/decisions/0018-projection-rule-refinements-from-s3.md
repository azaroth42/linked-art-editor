# ADR-0018: Projection rule refinements found by Spike S3

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Claude Fable 5.2 as the critical-step reviewer, under the owner's delegation while away (Rob Sanderson may override) · **Recorded by:** Claude · **Reviewed by:** Claude Fable 5.2

## Context
Spike S3 (Phase 0 Step 0.8) tested register entries B2 and B3 against the golden corpus. Taken literally they fail 17 of 244 files: `pycrdt` and Yjs carry numbers as doubles, so integers stored as "plain values" come back as floats (15 files), and a map keyed by target URI silently drops a reference that occurs twice in one array (2 files). They also leave arrays of scalars, objects without `type`, empty arrays, `@context` arrays, the sort tie-break and the behaviour at float precision limits undefined.

They also lose data under concurrency. If each array is its own nested `Map`, that `Map` has to be created before the first entry is added, and two `Map`s created concurrently under one key are last-writer-wins: when two clients each add the first value of a property that did not exist, one client's `Map` and its entries disappear. Pre-creating empty `Map`s at hydration covers only properties known in advance at the root of the record.

## Decision
Keep the intent of B2 and B3 (spec §4.2: repeated properties are keyed map entries, not ordered arrays; concurrent additions merge; order is carried by `_order`) and refine the wording as rules R1–R19 of `docs/spikes/S3-projection.md`:

- **B2, "arrays … become `Map`s keyed by internal node IRI / target URI":** an array is not a `Map` of its own. Its items are entries in the `Map` of the object that has the property, under the key `JSON.stringify([property, itemKey])`; a JSON key that itself starts with `[` is stored as `JSON.stringify([key])` (R6). The item key is still the internal node IRI or the target URI, with ` #2`, ` #3`, … appended for repeats within one array so that duplicates survive; scalar items are wrapped as `{_value, _order}` under a type-tagged key (R7). `_value` is reserved alongside `_order`. Adding the first value of a property is therefore one more key in a map that every replica already has, and no concurrent first insert is lost, for any property at any depth (R14).
- **B2, "other scalars are plain values":** integers within ±2^53 and non-integral floats are Y numbers; every other number, and every array or object that is not a node, reference or property-level array, is stored as exact JSON text in a plain `{"_json": …}` wrapper (R9).
- **B3, "sorts by `_order` then key":** the key is the whole entry key, compared by Unicode code point, and an exhausted gap or a tie triggers a rebalance of that property's entries to `0.0, 1.0, …` (R12, R13).
- **New:** `Text` positions are counted in code points and converted per library (R19).

Not changed: one root `Map` named `record`; nodes and references are nested shared `Map`s; `content` on the three types is `Text`; a single-valued property is a last-writer-wins register; unknown keys are preserved verbatim; `_order` never appears in JSON-LD.

## Alternatives considered
- **Pre-created empty containers per UI Profile property** (the spike's first proposal): leaves the loss in place for unprofiled properties and for arrays inside nodes created after hydration.
- **A fully flat document** (every node in one root-level map keyed by IRI, memberships in another): needs more than the one root `Map` that B2 fixes; embedded nodes that carry their own `id` can occur more than once in a record with different content, so IRI alone is not a key; removing a node leaves its descendants behind as unreachable entries, where the nested layout deletes the subtree in one operation.

## Consequences
Step 1.4 and Step 1.17 implement R1–R19 instead of the shorter lists in their guide text. Path resolution and change observers list the entries of a property by parsing keys instead of fetching a child map. The TypeScript mirror needs a code-point comparator, must build keys with `JSON.stringify`, and must never write `BigInt` or plain arrays. The semantic diff (B10) matches entries by entry key. Register entries B2 and B3 cite this ADR.

Evidence: 244 of 244 corpus files round-trip; 5000 of 5000 random documents; 5000 of 5000 random three-replica operation sequences converge with no lost insert; concurrent first inserts made by Yjs (JavaScript) and `pycrdt` (Python) into the same absent property are both kept on all 245 test documents.
