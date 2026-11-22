# ADR-0008: JSON-Schema is the first-order gate; project-authored SHACL runs second

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
Linked Art publishes JSON Schema but no SHACL; pyshacl is slow on large shapes.

## Decision
Vendor the upstream JSON Schema and validate first; write and maintain our own SHACL shapes with `lae:code` annotations, run only on documents that pass schema, within the budget measured by Spike S4; implement cross-field rules in Python.

## Consequences
Phase 1 Step 1.9; Value Set bindings compile into rules and generated shapes (Phase 2 Step 2.2).
