"""The Value Set registry (spec §7; Phase 2 Step 2.1).

Modules:

- ``loader``: ``profiles/vocab/*.yaml`` ⇄ ``vocabularies``, ``vocabulary_terms``,
  ``slot_vocabulary_bindings`` (``lae vocab import`` / ``export``).
- ``membership``: ``refresh_membership``, the materialized members of a Value Set in
  ``vocabulary_membership`` and ``entity_labels.vocab_ids`` (rule sets through QLever).
- ``metatypes``: harvesting the metatypes records state inline on concept references into
  ``entity_labels.metatype_uris`` (ADR-0032).
- ``usage``: ``refresh_usage_counts``, ``entity_labels.usage_count`` from QLever (nightly).
- ``service``: the ETag-cached bundle and term search behind ``/api/vocab`` (``lae.api.vocab``).
- ``cli``: ``lae vocab <import|export|refresh-membership|refresh-usage|harvest-metatypes>``.
"""
