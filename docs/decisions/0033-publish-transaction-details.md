# ADR-0033: Details of the publish transaction (outbox, revisions, warnings, bundles, conflicts)

**Status:** Accepted · **Date:** 2026-11-23 · **Decided by:** the Step 1.10 contract (`docs/contracts/1.10-publish.md`, Claude Fable 5.2), implemented by Claude Opus 5.5 (Rob Sanderson may override) · **Recorded by:** Claude

## Context
Phase 1 Step 1.10 builds the publish transaction (decision B8, spec §4.1): the draft of a record and every never-published record it refers to become published versions in one PostgreSQL transaction. The guide step and the spec leave several details open or state them loosely: what the outbox event of a first publish says, who published the revision that is inserted for a bulk-loaded version (decision A9), how a warning in a dependent stub is accepted, which class a reference to a stub is validated against, which conflicts publish reports and how, and what happens to `records.type` when a draft changes the class of its record. Step 1.11 (sync worker), Step 1.14 (revisions, revert) and the editor's publish dialog depend on the answers.

## Decision
1. **`outbox_events.from_version` is NULL on a first publish** (`version` 0 → 1). There is nothing in QLever to retract and no revision 0. For every later publish it is the previous version, and the revisions of both `from_version` and `to_version` exist when the event becomes visible. `payload = {"context_version", "drift_check"}`; `drift_check` is true only on the first publish of a bulk-loaded record whose loaded data could not be expanded or whose index build is not known to have used both the record's previous and the current context version and the current expander version (`sync_state` keys of `lae.bootstrap.common`; a missing key counts as a difference). The worker then retracts with `DELETE WHERE` over the owned subjects of `from_version`'s JSON-LD (ADR-0007, decision A13).
2. **`record_revisions.published_by` is nullable** (migration `0009`). The first publish of a bulk-loaded record inserts the revision of the loaded version in the same transaction (decision A9; `docs/questions.md`, Step 1.6): its JSON-LD and N-Triples (empty if it cannot be expanded), `authors = []`, `summary = ""`, `created_at` = the record's `updated_at`, and `published_by = NULL`, because nobody published that version through the editor. The downgrade writes the nil UUID into those rows.
3. **An accepted warning carries an optional `record_id`.** The API body is `accepted_warnings: [{code, path, record_id?}]`; without `record_id` it names the record being published. A path is a position in one document, so a warning in a dependent stub is accepted for that stub. Matching is exact on `(record, code, path)`; accepted entries that match nothing are ignored. The revision stores the warnings actually found, `[{code, path}]`, deduplicated and ordered by `(path, code)`.
4. **The bundle is validated as it will be after the commit.** For every bundle member, the class used by reference checks (`ReferenceFacts.types`) is the root `type` of that member's draft, because that is what `entity_labels` says once the bundle is published. Validated alone, a draft that cites a never-published record has no class to check against (ADR-0025, item 5).
5. **Three conflict codes besides "no draft" (`LAE-PUB-001`), warnings (`-002`) and violations (`-003`):** `LAE-PUB-004` (409) when a bundle draft changed, was published or was discarded after the snapshot that was validated, or a stub's row is gone; `LAE-PUB-005` (409) when a record is deleted or merged; `LAE-PUB-006` (409) when `records.version` is no longer the snapshot's `base_version`. None of them is retried automatically: a retry would publish content the caller has not seen diagnostics for.
6. **`records.type` follows the draft.** A publish sets `records.type` to the published data's `type`; `entity_labels.type` follows through `upsert_entity_labels_for_record`. The schema has already accepted the value.

## Alternatives considered
- **`from_version = 0` on a first publish.** There is no version 0 to read; the worker would need a special case anyway, and NULL says "nothing to retract" directly.
- **A synthetic system user as the publisher of a back-filled revision.** It would make bulk-loaded versions look edited and needs a user row in every database; `GET /api/records/{id}/revisions` already answers `published_by: null` for such versions.
- **Accepting warnings by `(code, path)` across the whole bundle.** Ambiguous: the same code at the same path in two documents are two different warnings.
- **Validating each member alone.** A reference to a stub would be accepted whatever class it claims, and the published object would fail validation from then on.
- **One generic 409 for every conflict.** The editor reacts differently: reload the draft (`-004`), stop (`-005`), discard and redo (`-006`).
- **Refusing a draft whose `type` differs from `records.type`.** Reclassification is a Phase 4 feature; keeping the two in step is what every reader of `records.type` expects.

## Consequences
- Step 1.11 treats a NULL `from_version` as "insert only", and reads the back-filled revision for the first edit of a bulk-loaded record.
- `published_by` must be treated as optional by every reader (revisions API, diff, revert).
- The editor's publish dialog sends `record_id` for warnings of a stub it shows.
- A known gap remains (recorded in `docs/questions.md`, Step 1.10): a warning accepted by `(code, path)` can be a different warning at the same position if the draft changed between the editor's validation and the publish.

Evidence: `backend/tests/publish/` (`test_publish.py`, `test_concurrency.py`, `test_discard.py`), against the real `api` and an in-process `crdt-ws` on PostgreSQL.
