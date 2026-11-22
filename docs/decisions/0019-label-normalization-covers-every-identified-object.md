# ADR-0019: Label normalization and transclusion cover every object with an `id`

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Claude Opus 5.5 (Phase 1 Step 1.5, conventions §8.2: the guide does not define the term) · **Recorded by:** Claude

## Context
Step 1.5 strips `_label` from "each reference whose `id` is in `known`" and transcludes it back on read, but the guide does not say which objects count as references. Spike S3 rule R3 (the projection's `is_reference`) counts only objects whose keys are among `id`, `type` and `_label`. By that rule, Linked Art's common shapes that say something about the target are not references: a `Type` with its metatype (`{"id": aat:Painting, "_label": …, "classified_as": [Type of Work]}`, 208 such objects in the corpus) and a `Language` with a `notation`. Their `_label` would then stay in storage for ever and never follow a renamed or translated target. Spec §3.2 and conventions §4 ("no `_label` in storage except for unresolved references") say the opposite. Step 1.3 already treats any object with a foreign `id` as a reference when it strips stub attributes.

## Decision
For labels, a reference is any object below the record root, outside `@context`, whose `id` is a string (`lae.labels.refs`). `normalize_refs` removes its `_label` when the `id` has an `entity_labels` row. `hydrate_labels` sets its `_label` from that row. Nothing else on the object changes. The root, objects without an `id`, and objects whose `id` has no row keep their `_label` (ADR-0006). Internal node IRIs never have a row, so embedded nodes are never touched. S3 rule R3 still classifies objects for projection; this ADR does not change it.

## Consequences
Metatype-bearing `Type` references and `Language` references with a `notation` show current, localized labels, like plain stubs. The `entity_labels` lookup includes every `id` in the document, internal node IRIs among them. That adds a few dozen elements to one indexed `= ANY` query and returns no extra rows.
