# ADR-0016: Adopt the LUX sparql_service contract for /sparql/vector (to be confirmed in Phase 3 Step 3.3)

**Status:** Proposed · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
QLever federates to an external SPARQL endpoint for vector search. LUX already has a tested implementation (`qlever-middletier/sparql_service`) with known pitfalls documented.

## Decision
Leading option: port the `vec:` contract to pgvector; interactive autocomplete still uses application-layer RRF. Phase 3 Step 3.3 confirms after Spike S1 results and flips this ADR to Accepted.

## Consequences
Qleverfile gets `service-max-value-rows = 100000`; the service enforces a 30 s timeout.
