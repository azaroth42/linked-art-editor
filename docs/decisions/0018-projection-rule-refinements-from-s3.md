# ADR-0018: Projection rule refinements found by Spike S3

**Status:** Proposed · **Date:** 2026-11-22 · **Decided by:** pending (Rob Sanderson) · **Recorded by:** Claude

## Context
Spike S3 (Phase 0 Step 0.8) tested register entries B2 and B3 against the golden corpus. Taken literally they fail 17 of 244 files: `pycrdt` and Yjs carry numbers as doubles, so integers stored as "plain values" come back as floats (15 files), and a map keyed by target URI silently drops a reference that occurs twice in one array (2 files). They also leave arrays of scalars, objects without `type`, empty arrays, `@context` arrays, the sort tie-break, the behaviour at float precision limits, and concurrent creation of a container undefined.

## Decision
Keep B2 and B3 and refine them as rules R1–R18 of `docs/spikes/S3-projection.md`. The changes to the locked wording are:

- **B2, "other scalars are plain values":** integers within ±2^53 and non-integral floats are Y numbers; every other number, and every array or object that is not a node, reference or property-level array, is stored as exact JSON text in a plain `{"_json": …}` wrapper (R9).
- **B2, "keyed by internal node IRI / target URI":** the key is the `id`, with ` #2`, ` #3`, … appended for repeats within one array, so duplicates survive; scalar items are wrapped as `{_value, _order}` under a type-tagged key (R6, R7). `_value` is reserved alongside `_order`.
- **B3, "sorts by `_order` then key":** the key comparison is by Unicode code point, and an exhausted gap or a tie triggers a rebalance of that container to `0.0, 1.0, …` (R12, R13).
- **New:** an empty container projects to an absent key, so containers can be created before clients diverge (R11, R14).

## Consequences
Step 1.4 and Step 1.17 implement R1–R18 instead of the shorter lists in their guide text; the TypeScript mirror needs a code-point comparator and must never write `BigInt` or plain arrays. The semantic diff (B10) matches entries by key. If this ADR is accepted, B2 and B3 in the register should cite it; they are not edited here because they are locked.
