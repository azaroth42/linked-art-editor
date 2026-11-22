# ADR-0024: Bulk-loaded records keep the URIs they arrive with

**Status:** Accepted (owner to confirm; `docs/questions.md`, Step 1.12) · **Date:** 2026-11-22 · **Decided by:** Claude Opus 5.5 as the implementing agent of Phase 1 Step 1.12, under conventions §8 (spec silent) · **Recorded by:** Claude

## Context
`lae bootstrap load` writes `records` rows from existing Linked Art JSON-LD. Decision A6 says record ids are UUIDv7 and public URIs are flat `/id/{uuidv7}`, which is how the editor mints new records (Step 1.6). The spec does not say what happens to a record that already has a URI from elsewhere. The golden corpus, which Step 1.12's Verify loads, is entirely such records (`https://linked.art/example/object/0`, `https://ycba-lux.s3.amazonaws.com/v3/object/…`), and an institution migrating into the editor brings its own published URIs. `records.id` is a UUID primary key and cannot hold those URIs.

## Decision
**A bulk-loaded record keeps its root `id` as `records.uri`, unchanged.** Its `records.id` is the UUID of that URI when the URI is local (`<base_uri><uuid>`, `lae.ids.record_id_from_uri`), and a new UUIDv7 otherwise. Internal node IRIs are minted under the record's own URI, `<records.uri>/node/<uuidv7>` (rule R4 accepts any record URI). Nothing else in the document is rewritten: references between loaded records keep pointing at the source URIs, which are the `records.uri` values.

## Alternatives considered
- **Re-mint every foreign record as `<base_uri><uuidv7>`**, keeping the source URI as `equivalent`. Satisfies A6 literally, but needs a second pass over the whole input to rewrite every reference to a re-minted record, changes the root `id` of every record (the corpus round-trip and the source's own links no longer match), and breaks every external link to the institution's published URIs unless the editor also serves redirects for them. It can still be done later as a migration from the state this ADR produces.
- **UUIDv5 of the source URI as `records.id`.** Deterministic across reloads, but not time-ordered (A6, spec §3.1 B-tree locality), and `records.uri` is unique already, so reloads are detected without it.

## Consequences
- The corpus and real institutional exports load as they are; `records.data` keeps the source `id`, and the source's links keep working.
- For such records `records.uri` is not `record_uri(records.id)`. `GET /id/{uuid}` (Step 1.6) finds the record by `id` and serves JSON-LD whose `id` is the source URI.
- `lae.ids.record_id_from_uri` and `owner_of` recognise only local URIs and node IRIs, so they return `None` / raise for these records' URIs and node IRIs. Code that maps a SPARQL subject back to its record (backlinks and delete protection, Phase 2 Step 2.7; merge referrer discovery) must also look up `records.uri` for a non-local subject: the record URI is the part before the last `/node/` (some source URIs contain `/node/` themselves, e.g. every Yale Library record). `is_internal_node_iri(iri, record_uri)` already works for any record URI.
- A local record URI with a UUID that is not version 7 keeps that UUID as its id.
