# Engineering Conventions

These rules apply to every phase. They exist so that work by different people and agents fits together without negotiation.

## 1. Repository layout

```
backend/            Python 3.12 service, package `lae` (src layout), managed with uv
  src/lae/          api/ crdt/ projection/ rdf/ sync/ validation/ profiles/ vocab/ labels/
                    db/ auth/ curation/ search/ jobs/ llm/ mcp/ agents/ admin/ cli.py settings.py
  migrations/       Alembic; one file per change, numbered (see §3)
  tests/            pytest; mirrors src/lae layout
frontend/           React 19 + TypeScript SPA, managed with pnpm
  src/app/ editor/ widgets/ crdt/ search/ reader/ i18n/ admin/ graph/ map/
profiles/           ui/ shapes/ vocab/ context/ schema/   (data, validated in CI)
deploy/             Qleverfile, Procfile, systemd/, postgres/, README.md (native install)
scripts/            One-off and operational scripts; spikes under scripts/spikes/
tests/corpus/       Golden Linked Art corpus
docs/               Everything in docs/README.md
```

Processes (Procfile entries, systemd unit names, and `lae` CLI sub-commands are identical): `api`, `crdt-ws`, `sync-worker`, `scheduler`, `embedder`, `reconcile`, `llm`, `qlever`, `postgres`.

## 2. Naming

- Python: modules and functions `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE`. Public functions have full type hints and a one-line docstring.
- TypeScript: components `PascalCase.tsx`, hooks `useThing.ts`, everything else `camelCase.ts`. One component per file.
- REST: `/api/<plural-resource>/{id}/<sub-resource>`. Internal-only endpoints under `/internal/` and protected by the shared secret `LAE_INTERNAL_TOKEN`. Public dereference under `/id/{uuid}`. WebSocket under `/ws/`. MCP at `/mcp`. SPARQL proxy at `/sparql`.
- Database: tables plural `snake_case`; primary keys `id`; foreign keys `<table_singular>_id`; timestamps `created_at`, `updated_at`; enums as Postgres enums named `<table>_<column>`.
- Diagnostics codes: `LAE-<AREA>-<NNN>`, e.g. `LAE-SCHEMA-001`, `LAE-SHACL-014`, `LAE-VOCAB-003`. The code is the i18n key under the `errors` namespace.
- Permissions: exactly the names in spec §8.2 (`edit_draft`, `publish`, `delete`, `merge`, `unmerge`, `protect`, `propose_merge`, `review_proposal`, `approve_merge_scoped`, `manage_vocabularies`, `manage_roles`, `manage_users`, `manage_backups`, `manage_system`, `view_analytics`).
- Settings: environment variables prefixed `LAE_`, declared in `lae/settings.py`, never read with `os.environ` elsewhere.

## 3. Migrations

- Numbered by phase: Phase 0–1 use `0001`–`0009`, Phase 2 `0010`–`0019`, Phase 3 `0020`–`0029`, Phase 4 `0030`–`0039`. Within a phase, take the next free number.
- Every migration has a working `downgrade`. CI runs `upgrade head`, `downgrade base`, `upgrade head`.
- Never edit a migration that is on `main`. Add a new one.

## 4. Code rules

- **Async everywhere in `api`, `crdt-ws`, and `sync-worker`.** Blocking work (pyld expansion, pyshacl, zstd) runs in `asyncio.to_thread` or a process pool.
- **No network in unit tests.** The JSON-LD document loader only serves `profiles/context/`; tests that need QLever or Postgres are marked `integration` and run against the native PostgreSQL and QLever.
- **Every endpoint has at least one test** that exercises the happy path and one permission failure.
- **Every projection or RDF change reruns the corpus round-trip test.** It is the oracle.
- **Structured output only.** Backend never returns English prose for diagnostics, diffs, notifications, or QA findings; it returns codes and parameters.
- **Internal node IRIs never reach public serializations.** `strip_internal_iris` is applied in exactly one place per output path, and there is a test for each path.
- **No `_label` in storage** except for unresolved references (spec §3.2).
- **Lossless by default.** Unknown JSON-LD keys are preserved. If a widget does not understand a value, it must show it in the raw inspector, not drop it.
- **Pins.** `litellm` is pinned to an exact version. `pycrdt` and `pycrdt-websocket` are pinned together. QLever is installed natively at the version recorded in `deploy/QLEVER_VERSION` (`/Users/rs2668/Development/lux/qlever-dev` is the reference source).
- **No Docker or containers** anywhere: development, CI, and production install everything natively (ADR-0017).

## 5. Frontend rules

- No literal user-facing strings in `.tsx` (ESLint `i18next/no-literal-string` is an error). Keys live in `frontend/public/locales/<lang>/<namespace>.json`.
- Dates, numbers, lists, and relative times go through `Intl`.
- Only CSS logical properties and Tailwind logical utilities (`ms-`, `me-`, `ps-`, `pe-`, `start-`, `end-`). CI greps for physical ones.
- Every drag-and-drop interaction ships with a keyboard and single-pointer equivalent in the same PR.
- Every new route gets an `axe` check in `frontend/e2e/`.
- Interactive targets are at least 24×24 CSS px.
- `aria-live` is used only for the events listed in spec §6.9.

## 6. Testing

| Kind | Location | Runs |
| :-- | :-- | :-- |
| Unit (backend) | `backend/tests/` | every PR |
| Corpus round-trip | `backend/tests/test_roundtrip.py` | every PR |
| Property tests | `backend/tests/property/` | every PR (bounded examples), nightly (extended) |
| Integration (Postgres) | marked `integration` | every PR, against PostgreSQL installed natively on the CI runner |
| Integration (QLever) | gated by `LAE_TEST_QLEVER_PORT` | **locally only, never in CI** (owner decision 2026-11-23): CI never installs or starts QLever. Run them with `LAE_TEST_QLEVER_PORT=<slot QLever port>` against Homebrew QLever before opening the PR and paste the pass output into the PR description; the tests skip when QLever is absent. Fable reviewers re-run them locally before merging |
| Frontend unit | `frontend/src/**/*.test.tsx` | every PR |
| E2E + axe | `frontend/e2e/` | every PR |
| Load / scale | `scripts/load/` | nightly, and at each phase exit |

Definition of done for a step: **Verify** passes locally, CI is green, the commit message matches the guide, and no `TODO` without a linked question in `docs/questions.md`.

## 7. Commits and PRs

- Message format: `<type>(<scope>): <summary>` where type is `feat`, `fix`, `db`, `infra`, `ci`, `test`, `docs`, `chore`, `spike`, `i18n`, `config`. Scope is the module or area (`projection`, `sync`, `editor`, `vocab`).
- One step of a phase guide per PR where practical. The PR description names the guide step (e.g. "Phase 1, Step 1.11").
- Branch from `main`; never commit directly to `main`.

## 8. When something is unclear or wrong

1. Re-read the spec section the guide points at. Most questions are answered there.
2. If the spec is silent, choose the simplest option that keeps round-tripping lossless and write a one-paragraph ADR in `docs/decisions/NNNN-<slug>.md` (Context, Decision, Consequences).
3. If the spec seems wrong or two sections conflict, do not change it. Write the ADR as a proposal, append the question to `docs/questions.md` with the step number, continue with independent work, and tell a human.
4. If a spike result or verification fails, record the measured result. Never record a guess as a result.
5. Never modify the adjacent repositories listed in `docs/README.md`. Copy what you need into this repo with a `# Vendored from <repo>/<path> @ <commit>` header.

## 9. Security basics

- Secrets come from environment variables or the encrypted `llm_provider_configs` table. Never in code, YAML, or logs.
- Anything that fetches a user-supplied URL goes through `lae.net.egress` with the SSRF allow/deny list. No exceptions.
- QLever's update endpoint is reachable only from `sync-worker` and `scheduler` on the internal network.
- Public routes (`/id/`, `/sparql`, `/activity/stream`) never require auth and never expose draft data.

## 10. Model-tier policy

- The default implementing agent is **Claude Opus 5.5**. It works through the phase guides step by step.
- Steps carrying the banner **CRITICAL STEP** follow **contract → implement → review** (owner decision, 2026-11-23):
  1. **Contract (larger model, Claude Fable / Mythos class).** Writes `docs/contracts/<step-id>-<slug>.md`: the core's public functions and signatures, the invariants it must keep (locking order, atomicity, idempotency, ownership, ordering), the failure modes and their structured errors, and the exact list of tests that must exist and pass (named, with the property each proves). The contract names which modules are "core" and which are scaffolding. It is merged to `main` in its own docs PR before implementation starts. No exploration beyond what the contract needs, and no implementation code.
  2. **Implementation (Opus 5.5).** Implements everything (core and scaffolding) on a step branch, cites the contract in the PR, writes every test the contract lists, opens the PR, waits for green CI, and does **not** merge. If the contract is wrong or incomplete, the implementer stops at that point and reports, rather than deviating silently.
  3. **Review (larger model).** Reviews the core diff against the contract (scaffolding only for interactions with the core), runs the tests, tries to break the invariants, fixes what is wrong on the branch, and merges after CI succeeds on the final head commit.
  The PR description says which model wrote the contract, which implemented, and which reviewed.
- If a non-critical step turns out to involve any of the following, stop and ask for a contract from the larger model: the publish transaction, the sync worker's diff or replay logic, projection or `_order` rules, room persistence or epochs, merge/split/unmerge, agent permission intersection, or anything that rewrites records other than the one being edited.
- Critical steps as of this writing: Phase 0 Step 0.8; Phase 1 Steps 1.3, 1.4, 1.8, 1.9, 1.10, 1.11, 1.14; Phase 2 Steps 2.6, 2.18; Phase 3 Steps 3.6, 3.8, 3.13; Phase 4 Steps 4.12, 4.15. The banner in the guide is authoritative if this list drifts.
