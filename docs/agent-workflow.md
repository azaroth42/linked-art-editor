# Agent Workflow: How an Implementing Agent Works One Issue

Every implementing agent follows this procedure for exactly one GitHub issue (one guide step). Several agents run in parallel, each in its own git worktree, so the isolation and merge rules below are mandatory.

## 1. Before you start

1. Read `docs/README.md`, `docs/conventions.md`, `docs/glossary.md`, `docs/decisions/REGISTER.md`, then your issue (`gh issue view <N>`) and the guide step it names. The guide step is the specification. The spec sections named in the guide's "Read first" line are the design.
2. `git fetch origin && git checkout -b step/<step-id>-<short-slug> origin/main` (e.g. `step/1.10-publish`).
3. Check the issue's **Depends on** list. Every dependency issue must be closed. If one is open, stop and report.

## 2. Local environment (development machine)

| Tool | How to use it |
| :-- | :-- |
| Docker | **Not available.** Write `deploy/docker-compose.yml` and Dockerfiles as specified, but verify behaviour with the native setup below. CI on GitHub Actions has Docker. |
| PostgreSQL | Postgres.app 18 running on `localhost:5432`, user `rs2668`, trust auth, extensions `vector` 0.8.7, `pg_trgm`, `btree_gin`. Binaries in `/Applications/Postgres.app/Contents/Versions/latest/bin` (add to `PATH`). |
| QLever | Homebrew 0.6.0: `qlever-index`, `qlever-server` (the IndexBuilderMain/ServerMain binaries) and `qlever` (qlever-control). Source for reference: `/Users/rs2668/Development/lux/qlever-dev`. |
| Python | `uv` 0.11; use Python 3.12 via `uv python install 3.12` (the repo pins it). System `python3` is 3.14; do not use it for the project venv. |
| Node | Node 26, npm 11. Use pnpm through Corepack (`corepack pnpm …`) as set up in Step 0.1. |
| Ollama | Installed, not running. Only Phase 3–4 steps may start it, and only if the step needs a real local model. Tests always use LiteLLM `mock_response`. |

## 3. Isolation rules for parallel agents

Your **slot number** is your GitHub issue number `N`.

- **Databases:** use only `lae_i<N>` and `lae_i<N>_test` (create them with `createdb`, drop them when done). Set `LAE_DATABASE_URL` and `LAE_TEST_DATABASE_URL` accordingly. Never touch another database.
- **Ports:** API `9000+N`, crdt-ws `9500+N`, QLever `7000+N` (and `7300+N` for a second "green" index), embedder `9800+N`. Never bind 5432, 7010, 8000 or another slot's ports.
- **Files outside the worktree:** scratch data goes under `/tmp/lae-i<N>/` (create it; delete large files when done). Never write into the adjacent repositories.
- **Background processes:** stop every server you start before you finish (`qlever stop`, `kill`).

## 4. Implementing

- Do exactly what the guide step says, in its order. Name files, functions, tables, endpoints, and commands exactly as written.
- If a later step's guide text implies an interface you must expose now, expose it (later steps extend, never reshape).
- If something is impossible on this machine (for example a human sign-off or a staging host), implement and test everything else, then record the gap in `docs/questions.md` under a heading `## Step <id>` with what was not verified and why. Do not fake measurements.
- **Spike NO-GO:** record the measured result, apply the fallback named in the guide or `docs/implementation_phases.md`, write it in the spike report, and add a `docs/questions.md` entry so a human can acknowledge it. The project owner has authorised continuing with the named fallback.
- **Migrations:** use the number the guide gives. Before pushing, rebase on `origin/main`; if `alembic heads` shows more than one head, set your migration's `down_revision` to the current head on main and renumber only if the number is already taken (take the next free number in your phase's range).
- **Do not edit the progress board** in `docs/README.md`. The orchestrator updates it.

## 5. Verifying

Run every command in the step's **Verify** block and the repo-wide checks:

```
cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
cd frontend && corepack pnpm lint && corepack pnpm typecheck && corepack pnpm test
```

(Use only the parts that exist at your point in the plan.) Integration tests that need Postgres or QLever run against your slot's databases and ports.

## 6. Pull request and merge

1. Commit with the message(s) given in the step's **Commit** line, ending with the attribution trailer `Co-Authored-By: <your model> <noreply@anthropic.com>`.
2. `git fetch origin && git rebase origin/main`, re-run the checks, `git push -u origin <branch>`.
3. `gh pr create --base main --title "[P<step-id>] <step title>" --body-file -` with: `Closes #<N>`, a summary of what was built, the Verify results (commands and key numbers), any deviations, any `docs/questions.md` entries, and the line "Implemented by: <model>". End the body with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
4. Wait for CI: `gh pr checks <PR> --watch --fail-fast`. Fix failures and push again. (Before Step 0.2 exists there is no CI; local checks suffice.)
5. **Non-critical steps:** when CI is green and the branch is up to date with main, merge with `gh pr merge <PR> --squash --delete-branch`. If the merge fails because main moved, rebase, re-check, push, wait for CI, and merge.
6. **Critical steps** (issue labelled `critical-model`): do **not** merge. Leave the PR open and report. A separate reviewer agent on a larger model reviews, fixes, and merges.

## 7. Final report (your last message)

Under 200 words: PR number and URL, merged or awaiting review, Verify results with key numbers, deviations from the guide, `docs/questions.md` entries, and anything the next step must know.
