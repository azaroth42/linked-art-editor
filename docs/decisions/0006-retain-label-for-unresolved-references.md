# ADR-0006: Retain _label for references with no entity_labels row

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
Write-time stripping of `_label` would lose the only label for external URIs that are in neither local records nor sidecars.

## Decision
Strip `_label` only when the target exists in `entity_labels`; otherwise keep it and raise an unresolved-reference QA finding.

## Consequences
`normalize_refs` takes the known set; the QA sweeper has rule `ref.unresolved_target`.
