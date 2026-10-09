# Enterprise Knowledge Intelligence RAG
#
# All paths are repo-relative. Every target assumes the repository root as the
# working directory, so a clean clone can run `make up` immediately.

SHELL := /bin/bash
.DEFAULT_GOAL := help

BACKEND := backend
VENV := $(BACKEND)/.venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
COMPOSE := docker compose

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Stack
# ---------------------------------------------------------------------------
.PHONY: up
up: validate-data ## Start the full stack: postgres, backend, frontend
	$(COMPOSE) up --build -d
	@echo ""
	@echo "  frontend  http://localhost:3000"
	@echo "  api       http://localhost:8000/api/v1"
	@echo "  health    http://localhost:8000/api/v1/health"
	@echo ""
	@$(PY) scripts/check_secrets.py

.PHONY: down
down: ## Stop the stack, keeping the database volume
	$(COMPOSE) down

.PHONY: destroy
destroy: ## Stop the stack and DELETE the database volume
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Follow the stack logs
	$(COMPOSE) logs -f

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) ps

# ---------------------------------------------------------------------------
# Data and hygiene gates
# ---------------------------------------------------------------------------
.PHONY: validate-data
validate-data: ## Validate both committed data files against their JSON Schemas
	@$(PY) scripts/validate_datasets.py

.PHONY: hygiene
hygiene: ## Fail on secrets, third-party page content, or machine-specific paths
	@scripts/check_repo_hygiene.sh

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
.PHONY: test
test: ## Run the unit and contract suites (no external services required)
	cd $(BACKEND) && ../$(PY) -m pytest -m "unit or contract" -q

.PHONY: test-all
test-all: ## Run every suite, including integration and e2e
	cd $(BACKEND) && ../$(PY) -m pytest -q

.PHONY: test-integration
test-integration: ## Run the integration suite (requires PostgreSQL and vendor services)
	cd $(BACKEND) && ../$(PY) -m pytest -m integration -q

.PHONY: coverage
coverage: ## Run the suite with a coverage report
	cd $(BACKEND) && ../$(PY) -m pytest --cov=app --cov-report=term-missing -q

.PHONY: lint
lint: ## ruff check and format check
	cd $(BACKEND) && ../$(PY) -m ruff check .
	cd $(BACKEND) && ../$(PY) -m ruff format --check .

.PHONY: typecheck
typecheck: ## mypy in strict mode
	cd $(BACKEND) && ../$(PY) -m mypy app

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
.PHONY: frontend-build
frontend-build: ## Type-check and build the frontend
	cd frontend && npm run typecheck && npm run build

.PHONY: frontend-lint
frontend-lint: ## Lint the frontend
	cd frontend && npm run lint

# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------
.PHONY: venv
venv: ## Create the backend virtualenv and install dependencies
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r $(BACKEND)/requirements-dev.txt

.PHONY: dev-backend
dev-backend: ## Run the backend with reload, against the compose database
	cd $(BACKEND) && ../$(PY) -m uvicorn app.main:app --reload --port 8000

.PHONY: dev-frontend
dev-frontend: ## Run the frontend dev server
	cd frontend && npm run dev

.PHONY: migrate
migrate: ## Apply database migrations
	cd $(BACKEND) && ../$(PY) -m alembic upgrade head

.PHONY: revision
revision: ## Generate a migration: make revision m="add column x"
	cd $(BACKEND) && ../$(PY) -m alembic revision --autogenerate -m "$(m)"

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
.PHONY: check
check: validate-data hygiene lint typecheck test ## Every gate that needs no external service

.PHONY: clean
clean: ## Remove caches and build output
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache $(BACKEND)/htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
