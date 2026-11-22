# ADR-0011: Explicit process topology; publish routed to crdt-ws over internal RPC

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
In-memory rooms live in one process while REST may run on another worker that cannot see the live Y.Doc.

## Decision
Define processes explicitly; `api` calls `crdt-ws` `/internal/rooms/{id}/snapshot|close|apply` with `X-Internal-Token`; `crdt-ws` persists every update synchronously so state is reconstructable.

## Consequences
Phase 1 Steps 1.1 and 1.8; Procfile entries and systemd unit names equal CLI sub-commands.
