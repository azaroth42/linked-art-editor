# ADR-0021: Embedded nodes that arrive with their own id are indexed under derived node IRIs

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Claude Fable 5.2 as the critical-step reviewer of Phase 1 Step 1.3, under the owner's delegation (Rob Sanderson may override) · **Recorded by:** Claude · **Reviewed by:** Claude Fable 5.2 · **Amended by:** ADR-0028 (a nested `@context` equal to the pinned one is ignored; an `id` may be percent-encoded in RDF)

## Context
Spec §3.3 and decision A12 say a record emits only triples whose subject it owns (its URI and its `<record_uri>/node/<uuidv7>` IRIs), and that every other object is reduced to `{"id": uri}` before expansion. Step 1.3 as first written applied that to every object whose `id` is not in the record's namespace.

That is right for references. It is wrong for an embedded node that came with an `id` of its own. Institutional Linked Art often mints IRIs for an embedded `Name`, `Identifier`, `Production`, `TimeSpan`, `Dimension` or statement `LinguisticObject`. Under the first rule such a node contributed one linking triple and its content never reached QLever, so the record could not be found by its title, identifier or date. In the golden corpus 205 objects have a foreign `id` and more than `type` / `_label`. 202 of them are references that repeat something about their target (183 `Type` with an inline meta-type, 18 `Language` / `Currency` with `notation`, and `Group`, `Place`, `Activity` references with `classified_as`). Three are embedded `Name`s (`object-3.json`, `object-nightwatch-43.json`, `person-rembrandt-4.json`).

Three things must hold together:

1. The content of an embedded node is indexed, whatever namespace its `id` is in.
2. Ownership stays exclusive: a revision diff or a `DELETE WHERE` for one record can never remove a triple of another record. Two records may embed a node with the same foreign `id`.
3. The JSON-LD round trip stays lossless: `records.data` keeps the `id` the source gave.

## Decision
**In RDF only, an embedded node with a foreign `id` is emitted under the derived node IRI `<record_uri>/node/<uuidv5(NAMESPACE_URL, original id)>`, with one extra triple `<derived> la:equivalent <original id>`.** The JSON-LD is not changed. `lae.ids.derived_node_iri` builds the IRI; `lae.rdf.expand` applies the rule.

A foreign object (one whose `id` is neither the record URI nor one of its UUIDv7 node IRIs) is classified from the object alone, in this order:

1. Only `id`, `type`, `_label` (and `@context`): a **reference**.
2. `type` in `ENTITY_TYPES` (`Type`, `Language`, `Material`, `Currency`, `MeasurementUnit`, `Person`, `Group`, `Place`, `HumanMadeObject`, `Set`), the classes Linked Art never embeds: a **reference**, whatever else it carries. The target owns its description, including a meta-type repeated inline.
3. `type` in `EMBEDDED_TYPES` (`Name`, `Identifier`, `TimeSpan`, `Dimension`, `MonetaryAmount`, `AttributeAssignment`, `Production`, `Creation`, `Destruction`, `Birth`, `Death`, `Formation`, `Dissolution`, `Joining`, `Leaving`, `Acquisition`, `Payment`, `TransferOfCustody`, `Move`, `Encounter`, `PartAddition`, `PartRemoval`, `Modification`, `Transformation`, `Addition`, `Removal`, `RightAcquisition`, `Right`, `DigitalService`), the classes that exist only inside a record: an **embedded node**.
4. Any other class (`LinguisticObject`, `DigitalObject`, `VisualItem`, `Activity`, `Event`, `Period`, …, an unknown class, or none): an **embedded node** exactly when it has a property other than `classified_as`; otherwise a **reference** (a reference may repeat its target's classification).

A reference becomes `{"id": uri}` and emits the linking triple only, as before. An embedded node keeps its content, which is processed by the same rule recursively. Every other object in the record with the same foreign `id` (for example the `assigned` of an `AttributeAssignment`) points at the same derived node. A node of the record may not carry an `@context` of its own.

Ownership is now stated by namespace: **a record's subjects are its URI and IRIs under `<record_uri>/node/`** (UUIDv7 minted by the projection, UUIDv5 derived by expansion). `expand_to_ntriples` checks every emitted subject against that. `owned_subjects` returns the record URI, its UUIDv7 node IRIs and the derived IRI of *every* foreign `id` in the document, a superset of what is emitted, so that the ADR-0007 `DELETE WHERE` fallback also removes what an earlier expander version, with different class lists, may have emitted. `ENTITY_TYPES` and `EMBEDDED_TYPES` are part of the expander: changing either bumps `EXPANDER_VERSION`.

`is_internal_node_iri` stays UUIDv7-only, because a derived IRI never appears in JSON-LD and so must never be stripped from it. `owner_of` and `record_id_from_uri` accept both kinds, since both appear in SPARQL results.

## Alternatives considered
- **Keep the first rule (link only).** Fails constraint 1.
- **Structural ownership: emit the foreign IRI itself as a subject, and check at publish that no subject has two owners.** Needs a global subject registry consulted in the publish transaction and by bulk load. It turns a common, harmless input (two records that embed the same minted `TimeSpan` or `Name` IRI) into a publish failure for whichever record comes second, and a registry that is ever wrong lets one record's `DELETE WHERE` remove another's triples, which is the failure the step exists to prevent. Derived IRIs make the collision impossible instead of detecting it.
- **Re-key such nodes to UUIDv7 node IRIs at import.** Fails constraint 3: the source's `id` is gone from `records.data` and from every export.
- **Decide reference or embedded from `entity_labels` (is the id a known entity?).** Expansion would depend on database state at the time it runs, so re-expanding a stored revision (ADR-0007, the consistency audit, a rebuild) could give different triples.
- **Emit both links, to the original IRI and to the derived node.** Keeps a one-hop link to the original, but doubles every count of names, identifiers and dimensions, and joins the embedded copy with whatever else describes the original IRI.
- **A project predicate instead of `la:equivalent`.** `equivalent` is the Linked Art property for "this entity is the same as that IRI", is already in the pinned context, and needs no new vocabulary on the public SPARQL endpoint.

## Consequences
- The three corpus `Name`s are indexed. Nothing changes for the 202 references. A record whose embedded nodes all carry institutional IRIs yields the same graph shape as one whose nodes were anonymous, plus one `la:equivalent` triple per node.
- A search for an institution's own node IRI is `?node la:equivalent <iri>`; `owner_of(?node)` gives the record.
- A content-bearing object of an ambiguous class that was meant as a reference to another record is indexed as an embedded copy, one hop further from its target (`<record> p <derived>`, `<derived> la:equivalent <target>`). Backlink and delete-protection queries of the form `?s ?p <target>` still find it, with a `/node/` subject and the predicate `la:equivalent`; Phase 2 Step 2.7 maps `/node/` subjects to their record already. Write-time normalization to stubs (spec §3.2) is what keeps real references out of this case.
- Inline meta-types on concept references (`classified_as` inside a `Type`) are still not indexed from the referencing record. Rule-backed Value Set membership (Phase 2 Step 2.1) must harvest them into `entity_labels.metatype_uris` or the sidecar graphs, as LUX's `reference_manager.walk_for_refs` does (`docs/questions.md`, Step 1.3).
- `GET /id/{uuid}/node/{nid}` (Step 1.8) must 303 for a UUIDv5 `nid` as well as a UUIDv7 one; `record_id_from_uri` does this.
- The public Turtle of a record (`to_turtle`) is made from the JSON-LD and shows the original ids. Only QLever and revision N-Triples use derived IRIs.
- Register entries A7 and A12 cite this ADR.

Evidence: `backend/tests/rdf/test_expand.py` (exact triple sets for a record whose every embedded node has a minted IRI; two records embedding nodes with the same foreign ids share no subject, and a diff, a full delete and a `DELETE WHERE` of one leave the other's triples intact; the corpus compared with pyld's own output under an independently written statement of the rule). The same sequence was run against Homebrew QLever 0.6.0 over HTTP with an access token: 18 of 18 checks, the triple set equal to the expected one after every step.
