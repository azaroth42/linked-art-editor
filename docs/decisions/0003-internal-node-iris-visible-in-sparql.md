# ADR-0003: Internal node IRIs are stripped from JSON-LD but visible in SPARQL

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson (design review) · **Recorded by:** Claude

## Context
Embedded nodes carry `<record_uri>/node/<uuidv7>` IRIs for diffing and anchoring. They cannot be hidden from a public SPARQL endpoint.

## Decision
"Public" means anonymous viewing applications consuming JSON-LD. Node IRIs are stripped from JSON-LD serializations and appear in SPARQL results and N-Triples; `/id/{uuid}/node/{id}` 303-redirects to the parent record.

## Consequences
Turtle/N-Triples downloads may contain node IRIs; the Public Reader and JSON-LD never do. Tests assert both.
