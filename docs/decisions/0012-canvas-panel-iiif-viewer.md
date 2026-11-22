# ADR-0012: Canvas Panel is the IIIF viewer

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
The design listed Clover IIIF or Mirador 4. Canvas Panel (Digirati) is a lighter embeddable web component with native Content State and region selection.

## Decision
Use Canvas Panel in the editor and Public Reader for all media, including A/V. Clover and Mirador are not used. If Spike S5 finds a capability Canvas Panel lacks, record it for the owner in `docs/questions.md`; do not introduce another viewer.

*Amended 2026-11-22 by the owner: the earlier "Clover only for A/V" fallback was removed.*

## Consequences
Spike S5 verifies React 19 integration; Phase 1 Step 1.21, Phase 2 Step 2.12.
