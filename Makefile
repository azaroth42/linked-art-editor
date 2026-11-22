# Developer entry points. See README.md.
# pnpm is always run through Corepack so the version pinned in frontend/package.json is used.

UV      ?= uv
PNPM    ?= corepack pnpm
COMPOSE ?= docker compose -f deploy/docker-compose.yml

.PHONY: setup lint test up down

setup: ## Install backend (uv) and frontend (pnpm) dependencies from the lockfiles
	cd backend && $(UV) sync --locked
	cd frontend && $(PNPM) install --frozen-lockfile

lint: ## Lint, format-check, and type-check backend and frontend
	cd backend && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy src
	cd frontend && $(PNPM) lint && $(PNPM) typecheck && $(PNPM) format:check

test: ## Run backend and frontend unit tests
	cd backend && $(UV) run pytest -q
	cd frontend && $(PNPM) test

up: ## Start local infrastructure with Docker Compose
	$(COMPOSE) up -d

down: ## Stop local infrastructure
	$(COMPOSE) down
