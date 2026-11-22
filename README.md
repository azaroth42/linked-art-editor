# linked-art-editor
A web based multi-user real time editor for Linked Art records

Design, decisions, and the implementation plan live in [`docs/`](docs/README.md).

## Repository layout

| Path | Contents |
| :-- | :-- |
| `backend/` | Python 3.12 service, package `lae` (src layout), managed with `uv` |
| `frontend/` | React 19 + TypeScript SPA (Vite), managed with `pnpm` |
| `profiles/` | UI Profiles, SHACL shapes, Value Sets, pinned `@context`, vendored JSON Schema |
| `deploy/` | Native setup: `Qleverfile`, Postgres init script, `Procfile`, systemd units ([`deploy/README.md`](deploy/README.md)) |
| `scripts/` | One-off and operational scripts |
| `tests/corpus/` | Golden Linked Art corpus |

## Prerequisites

- **Python 3.12** via [`uv`](https://docs.astral.sh/uv/) 0.11 or later. `backend/.python-version`
  pins 3.12; `uv python install 3.12` fetches it if missing. Do not build the project venv
  with a system Python of another version.
- **Node.js** 22.22+, 24.15+, or 26+ with **Corepack**. Always run pnpm as `corepack pnpm …`:
  Corepack uses the exact pnpm version pinned in `frontend/package.json` (`packageManager`).
  Node 25 and later no longer bundle Corepack; install it with `npm install -g corepack` or
  `brew install corepack`.
- **PostgreSQL** 16 or later with the `vector` (pgvector), `pg_trgm`, and `btree_gin`
  extensions, already running. On macOS, Postgres.app bundles all three; put its tools on
  `PATH`: `export PATH="/Applications/Postgres.app/Contents/Versions/latest/bin:$PATH"`.
- **QLever** native binaries (`qlever-index`, `qlever-server`) and the qlever-control CLI
  (`qlever`): on macOS `brew tap qlever-dev/qlever && brew install qlever`. The verified version
  is in `deploy/QLEVER_VERSION`.

  Everything runs natively; [`deploy/README.md`](deploy/README.md) has the install steps for
  macOS and Debian/Ubuntu and the production setup with systemd.
- **pre-commit** (optional): `uvx pre-commit install` after `make setup`.

## Run locally

```sh
make setup    # uv sync --locked (backend) and corepack pnpm install --frozen-lockfile (frontend)
make up       # create database `lae` + extensions if missing; build an empty QLever index if missing; start QLever on 7010
(cd backend && uv run --env-file ../.env lae migrate)   # apply database migrations (alembic upgrade head)
make run      # run the lae processes in deploy/Procfile (api, crdt-ws, sync-worker, scheduler) with honcho
make logs     # follow the QLever server log
make down     # stop QLever
```

`make up` never starts or stops PostgreSQL; it must already be running (Postgres.app,
`brew services`, or `systemctl`). It prints the `LAE_*` settings for the `lae` processes; put
them in a `.env` file in the repository root (not committed), which `make run` loads.
To sign in, set `LAE_DEV_USER=you@example.org:Editor` there, or configure an OpenID Connect
provider; [`docs/dev-setup.md`](docs/dev-setup.md) covers both and Personal Access Tokens.

Ports, database name, and index directory are overridable, e.g.
`make up LAE_DB=lae_i3 QLEVER_PORT=7003 QLEVER_DIR=/tmp/lae-i3/qlever` and
`make down QLEVER_PORT=7003`. Defaults: `PGPORT=5432`, `LAE_DB=lae`, `QLEVER_PORT=7010`,
`QLEVER_DIR=deploy/data/qlever`; the full list is in
[`deploy/README.md`](deploy/README.md#development).

Then, for example:

```sh
cd backend && uv run lae --help        # process sub-commands: api, crdt-ws, sync-worker, ...
cd frontend && corepack pnpm dev       # Vite dev server
```

## Run tests

```sh
make lint     # ruff check, ruff format --check, mypy (backend); eslint, tsc, prettier (frontend);
              # and `make profiles`: profiles/ui and profiles/vocab YAML against their schemas
make test     # pytest (backend); vitest (frontend)
```

The equivalent direct commands:

```sh
cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
cd frontend && corepack pnpm lint && corepack pnpm typecheck && corepack pnpm test
```

Database tests use only `LAE_TEST_DATABASE_URL` (for example
`postgresql+asyncpg://localhost:5432/lae_test`) on a native PostgreSQL with pgvector. The test
session drops and recreates that database (its name must end in `_test`), applies
`deploy/postgres/init.sql` and the migrations, and drops it at the end. The tests are skipped when
the variable is unset (under CI they fail).

End-to-end tests (Playwright + axe) run against the production build:

```sh
cd frontend && corepack pnpm exec playwright install chromium   # once
cd frontend && corepack pnpm build && corepack pnpm test:e2e
```

CI (`.github/workflows/ci.yml`) runs the `backend`, `frontend`, `e2e`, and `config` jobs on every
pull request; all four must be green before merging.
