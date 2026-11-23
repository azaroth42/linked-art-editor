# ADR-0015: Documentation set and model-tier policy

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
Implementation will be done by Claude Opus 5.5 working from documents, with a stronger model available for hard parts. The original single design document was hard to cite and mixed design with process.

## Decision
Split the design into `docs/spec/` (section numbers preserved), keep process in `docs/implementation_phases.md` and `docs/phases/*.md`, record decisions in this register and ADRs, and mark steps needing a larger model with a CRITICAL banner.

## Consequences
The original design document is archived in `docs/old/`; `docs/README.md` is the start-here index and status board.

*Amended 2026-11-23 (owner):* critical steps no longer need the larger model to implement. The larger model writes a contract in `docs/contracts/` and reviews the core diff against it; Opus 5.5 implements (conventions §10). Steps already merged under the earlier rule are unaffected.
