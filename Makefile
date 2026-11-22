# Developer entry points. See README.md and deploy/README.md.
# pnpm is always run through Corepack so the version pinned in frontend/package.json is used.
# Everything runs natively: PostgreSQL and QLever are installed on the host (deploy/README.md).

UV      ?= uv
PNPM    ?= corepack pnpm

# Native infrastructure. Every value can be overridden from the environment or on the command
# line, e.g. `make up LAE_DB=lae_i3 QLEVER_PORT=7003 QLEVER_DIR=/tmp/lae-i3/qlever`.
# The PostgreSQL client tools read PGHOST, PGPORT, and PGUSER themselves (PGHOST unset: the
# default Unix socket; PGUSER unset: your login).
PGPORT      ?= 5432
LAE_DB      ?= lae
QLEVER_PORT ?= 7010
QLEVER_DIR  ?= deploy/data/qlever
# deploy/Qleverfile reads QLEVER_PORT (and QLEVER_INPUT_FILES, LAE_QLEVER_ACCESS_TOKEN if set).
export PGPORT QLEVER_PORT

# PostgreSQL client tools: Postgres.app's when it is installed, otherwise those on PATH.
PG_BIN  ?= $(wildcard /Applications/Postgres.app/Contents/Versions/latest/bin)
PG      := $(if $(PG_BIN),$(PG_BIN)/)
QLEVER  ?= qlever
QLEVER_CMD := $(QLEVER) --qleverfile "$(CURDIR)/deploy/Qleverfile"

.PHONY: setup lint test up down logs run

setup: ## Install backend (uv) and frontend (pnpm) dependencies from the lockfiles
	cd backend && $(UV) sync --locked
	cd frontend && $(PNPM) install --frozen-lockfile

lint: ## Lint, format-check, and type-check backend and frontend
	cd backend && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy src
	cd frontend && $(PNPM) lint && $(PNPM) typecheck && $(PNPM) format:check

test: ## Run backend and frontend unit tests
	cd backend && $(UV) run pytest -q
	cd frontend && $(PNPM) test

# `up` never starts or stops PostgreSQL itself: it must already be running.
up: ## Create the database and extensions if missing, build an empty QLever index if missing, start QLever
	@$(PG)pg_isready -q || { echo "PostgreSQL is not accepting connections on $(or $(PGHOST),the default socket), port $(PGPORT). Start it first (Postgres.app, brew services, or systemctl); make up does not start it." >&2; exit 1; }
	@if $(PG)psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$(LAE_DB)'" | grep -q 1; then \
	  echo "Database $(LAE_DB) exists"; \
	else \
	  $(PG)createdb $(LAE_DB) && echo "Created database $(LAE_DB)"; \
	fi
	@PGOPTIONS='-c client_min_messages=warning' $(PG)psql -d $(LAE_DB) -q -v ON_ERROR_STOP=1 -f deploy/postgres/init.sql
	@mkdir -p "$(QLEVER_DIR)"
	@cd "$(QLEVER_DIR)" && if [ -e lae.meta-data.json ]; then \
	  echo "QLever index exists in $(QLEVER_DIR)"; \
	else \
	  $(QLEVER_CMD) get-data && $(QLEVER_CMD) index; \
	fi
	@if curl -sf -o /dev/null --max-time 2 "http://localhost:$(QLEVER_PORT)/ping"; then \
	  echo "QLever is already running on port $(QLEVER_PORT)"; \
	else \
	  cd "$(QLEVER_DIR)" && $(QLEVER_CMD) start; \
	fi
	@echo
	@echo "Ready. Settings for the lae processes (e.g. in .env):"
	@echo "  LAE_DATABASE_URL=postgresql+asyncpg://$(if $(PGUSER),$(PGUSER)@)$(or $(PGHOST),localhost):$(PGPORT)/$(LAE_DB)"
	@echo "  LAE_QLEVER_URL=http://localhost:$(QLEVER_PORT)"
	@echo "  LAE_QLEVER_UPDATE_URL=http://localhost:$(QLEVER_PORT)"

# Matches the server by port, so QLever servers on other ports keep running. The regex is
# anchored to the start of the command line so that it does not match `qlever stop` itself.
down: ## Stop the QLever server on QLEVER_PORT (PostgreSQL and the database are left alone)
	$(QLEVER_CMD) stop --no-containers --cmdline-regex '^\S*qlever-server .* -p $(QLEVER_PORT) '

logs: ## Follow the QLever server log
	tail -n 100 -f "$(QLEVER_DIR)/lae.server-log.txt"

run: ## Run the lae processes in deploy/Procfile with honcho (Ctrl-C stops them)
	$(UV) run --project backend honcho start -f deploy/Procfile
