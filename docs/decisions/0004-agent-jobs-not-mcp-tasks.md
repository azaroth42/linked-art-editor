# ADR-0004: Server-owned agent_jobs instead of the MCP Tasks extension

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
MCP Python SDK v2 (stable July 2026) removed experimental Tasks support and does not implement the ratified extension.

## Decision
Long-running agent work (batch translation, enrichment, transcription, sweeps) runs on an `agent_jobs` table with a runner in `scheduler`, MCP tools `start_job`/`cancel_job`, and `linkedart://job/{id}` progress resources.

## Consequences
Phase 3 Step 3.7. Interactive-origin jobs get priority 1; handlers must check cancellation between items; jobs resume after restart.
