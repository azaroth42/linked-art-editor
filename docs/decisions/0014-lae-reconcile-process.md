# ADR-0014: A new lae-reconcile process instead of wrapping the LUX pipeline

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
The design said to wrap the existing LUX reconciliation pipeline as HTTP. The survey found no service: reconciliation is exact-key LMDB lookup inside a batch build, mutating records and sometimes fetching over the network.

## Decision
Build `lae-reconcile`: vendor `TabLmdb`, `extract_names`, and the `LmdbReconciler` matching logic; run with network off; return ranked candidates with evidence; never mutate input.

## Consequences
Phase 3 Step 3.11; the editor adds lexical/semantic candidate search beside it.
