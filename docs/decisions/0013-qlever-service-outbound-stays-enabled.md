# ADR-0013: QLever outbound SERVICE stays enabled

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
The review suggested disabling outbound SERVICE on the public QLever endpoint for SSRF reasons.

## Decision
Rejected as harmless; the public endpoint is behind the read-only `/sparql` proxy and QLever runs on the internal network. The `/sparql/vector` SERVICE depends on outbound SERVICE.

## Consequences
No change to the Qleverfile for this.
