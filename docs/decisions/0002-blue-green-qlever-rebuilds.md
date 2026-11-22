# ADR-0002: Blue/green QLever rebuilds with persisted updates and an outbox watermark

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
The original design rebuilt the index when zero editors were logged in and the delta-triple count exceeded a threshold. Public traffic is continuous and multi-billion-triple builds take hours.

## Decision
Run QLever with `--persist-updates`. Keep `sync_state.qlever_index_watermark`. Rebuild by snapshotting at a watermark, building a second index, replaying outbox events past the watermark, and swapping atomically; keep the previous index for rollback. Trigger by threshold or on demand, never by idleness.

## Consequences
Phase 2 Step 2.18 implements it; the sync worker must accept a target URL; the Qleverfile and compose define two index directories.
