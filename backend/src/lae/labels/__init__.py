"""Entity labels: extraction, write-time stub normalization, read-time transclusion (spec §3.2).

``extract`` derives BCP-47 labels and a default label from a record; ``derive`` derives a
record's root ``_label`` from its primary name (Step 1.19a); ``upsert`` stores the labels in
``entity_labels``; ``normalize`` strips ``_label`` from references whose target has a row there
(decision A11, ADR-0006); ``transclude`` puts current labels back on read, in the caller's
locale chain.
"""
