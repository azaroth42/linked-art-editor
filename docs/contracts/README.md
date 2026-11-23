# Contracts for critical steps

Each critical step (issue labelled `critical-model`) gets a contract here, written by a Fable/Mythos-class model and merged before implementation starts (`docs/conventions.md` §10). File name: `<step-id>-<slug>.md`, e.g. `1.10-publish.md`.

A contract contains, in this order:

1. **Scope:** which modules and functions are *core* (reviewed line by line) and which are scaffolding.
2. **Interfaces:** public functions, signatures, request and response shapes, and structured error codes.
3. **Invariants:** what must always hold (locking order, atomicity, idempotency, ownership, ordering, crash safety), each stated so a test can check it.
4. **Failure modes:** every way the operation can fail and what the caller sees.
5. **Required tests:** a named list, each with the property it proves; the implementation PR must contain all of them.
6. **Out of scope / later steps:** what not to build now.

The Opus implementer builds to the contract and stops to report if the contract is wrong. The larger-model reviewer checks the core against it and merges.
