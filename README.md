# linked-art-editor
A web based multi-user real time editor for Linked Art records

Design, decisions, and the implementation plan live in [`docs/`](docs/README.md).

## Repository layout

| Path | Contents |
| :-- | :-- |
| `backend/` | Python 3.12 service, package `lae` (src layout), managed with `uv` |
| `frontend/` | React 19 + TypeScript SPA (Vite), managed with `pnpm` |
| `profiles/` | UI Profiles, SHACL shapes, Value Sets, pinned `@context`, vendored JSON Schema |
| `deploy/` | `docker-compose.yml`, `Qleverfile`, Postgres init script |
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
- **Docker** with Compose v2 for `make up` (the reference setup, also used by CI). A native
  setup with Postgres.app and Homebrew QLever (`make up-native`) arrives in Phase 0, Step 0.3.
- **pre-commit** (optional): `uvx pre-commit install` after `make setup`.

## Run locally

```sh
make setup    # uv sync --locked (backend) and corepack pnpm install --frozen-lockfile (frontend)
make up       # start local infrastructure (deploy/docker-compose.yml)
make down     # stop it
```

Then, for example:

```sh
cd backend && uv run lae --help        # process sub-commands: api, crdt-ws, sync-worker, ...
cd frontend && corepack pnpm dev       # Vite dev server
```

## Run tests

```sh
make lint     # ruff check, ruff format --check, mypy (backend); eslint, tsc, prettier (frontend)
make test     # pytest (backend); vitest (frontend)
```

The equivalent direct commands:

```sh
cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
cd frontend && corepack pnpm lint && corepack pnpm typecheck && corepack pnpm test
```
