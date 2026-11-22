"""Entity labels: extraction, write-time stub normalization, read-time transclusion (spec §3.2).

``extract`` derives BCP-47 labels and a default label from a record; ``upsert`` stores them in
``entity_labels``; ``normalize`` strips ``_label`` from references whose target has a row there
(decision A11, ADR-0006); ``transclude`` puts current labels back on read, in the caller's
locale chain.
"""
