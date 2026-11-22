# ADR-0023: Records API and public dereference details

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Claude Opus 5.5 (Phase 1 Step 1.6, conventions §8.2: the guide and spec are silent on these points) · **Recorded by:** Claude

## Context
Step 1.6 specifies `POST`/`GET /api/records`, the revision routes, and content negotiation at `/id/{uuid}` with `?version=N`, 410, 301, `ETag`, `Last-Modified` and `If-None-Match`. It does not say how a browser reaches the JSON-LD and Turtle the HTML page links to, what entity tag each representation carries, which classes `POST` accepts, where the `template` goes before Step 1.8 opens rooms, how `?version=N` combines with a status, or what the signed-in API does for deleted and merged records.

## Decision
1. **`?format=jsonld|ttl|html` overrides `Accept`** on `/id/{uuid}`, because a link cannot set `Accept`. The HTML page links to `?format=jsonld` and `?format=ttl` (keeping `?version=N`). Without `format`, the highest `q` wins, then an explicitly named type over a wildcard, then HTML, JSON-LD, Turtle in that order; `application/json` counts as JSON-LD; no `Accept`, `*/*`, or nothing acceptable gives HTML. Other `Accept` profile parameters are ignored (there is one profile).
2. **Entity tags are weak and per representation:** `W/"<version etag>-<jsonld|ttl|html>"`, where the version etag is `records.etag` for the current version and a BLAKE2b-128 of the stored snapshot for `?version=N`. Weak, because transcluded labels can change while the record does not; per representation, because the three bodies differ. `Vary: Accept, Accept-Language`. `records.etag` is BLAKE2b-128 hex of the canonical JSON (`lae.api.records.record_etag`, as Spike S6's bulk load).
3. **Statuses before versions.** `/id/{uuid}?version=N` of a deleted record is 410 and of a merged one 301 to the target's URI (keeping `?format`, dropping `?version`, whose numbers belong to the old record). A never-published record (`version = 0`) is 404, so drafts are not even shown to exist. A merged record without `merged_into` is answered as deleted.
4. **Version N** is the `record_revisions` row for N, else `records.data` when N is the current version (a bulk-loaded record at version 1), else 404 `LAE-REC-004`.
5. **`POST /api/records`** accepts the top-level Linked Art classes only (`RECORD_TYPES`: HumanMadeObject, Person, Group, Place, Type, Language, Material, Currency, MeasurementUnit, VisualItem, LinguisticObject, DigitalObject, Set, Activity, Event, Period; 422 `LAE-REC-005` otherwise). A `template` is stored in `records.data` beside the skeleton, its `@context`, `id` and `type` replaced and its own record's node IRIs re-minted, so the room that Step 1.8 hydrates from `records.data` starts with it; a template the projection cannot hydrate is refused up front (422 `LAE-REC-006`).
6. **Signed-in API.** `GET /api/records/{id}` answers 410 for a deleted record and 301 to `/api/records/{merged_into}` for a merged one, like the public route. The revision routes answer whatever the status, so history stays available for unmerge and undelete. The revision list is oldest first; a current version without a revision row (bulk-loaded) is listed with `published_by: null`.

## Consequences
- Step 1.21's reader keeps `?format=` for its download links. Memento (`Accept-Datetime`, Step 2.16) is added beside `?version=N` and does not change these rules.
- Publish (Step 1.10) sets `records.etag` with `record_etag`, so the public entity tag changes on every publish and only then.
- A 304 can be answered for a body whose transcluded labels have since changed; the weak tag says the representations are equivalent, not identical.
- Version 1 of a bulk-loaded record is served only while it is the current version; see `docs/questions.md`, Step 1.6.
