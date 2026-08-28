# Agentic Dashboard — developer commands.
# Quick start:  make venv && make up && make ports
# The project path contains spaces, so every path is quoted and we use a one-shell-per-recipe.

SHELL := /bin/bash
.ONESHELL:
.DEFAULT_GOAL := help

VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
ROOT    := $(CURDIR)
TICKER  ?= NVDA

# Load the picked ports (if present) into the recipe shell; harmless when absent.
define load_ports
set -a; [ -f "$(ROOT)/.env.ports" ] && source "$(ROOT)/.env.ports"; set +a
endef

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'

# ── environment ────────────────────────────────────────────────────────────────
.PHONY: venv
venv: $(VENV)/.installed ## Create .venv and install backend + dev deps

$(VENV)/.installed: backend/requirements.txt
	python3 -m venv "$(VENV)"
	$(PIP) install --upgrade pip >/dev/null
	$(PIP) install -r backend/requirements.txt
	$(PIP) install ruff
	touch "$(VENV)/.installed"
	@echo "✓ venv ready at $(VENV) (activate: source $(VENV)/bin/activate)"

.PHONY: fe-install
fe-install: ## Install frontend node deps
	cd "$(ROOT)/frontend" && npm install --no-audit --no-fund

# ── tests / quality ────────────────────────────────────────────────────────────
.PHONY: test
test: test-backend test-src ## Run all Python tests (backend + src)

.PHONY: test-backend
test-backend: venv ## Backend dashboard unit tests
	cd "$(ROOT)" && PYTHONPATH="$(ROOT)" $(PY) -m pytest backend/tests -q

.PHONY: test-src
test-src: venv ## Sprinkle Sauce screen unit tests
	cd "$(ROOT)" && $(PY) -m pytest tests -q

.PHONY: lint
lint: venv ## Ruff lint the backend + screen (same invocation as CI)
	cd "$(ROOT)" && "$(ROOT)/$(VENV)/bin/ruff" check backend/app src db

.PHONY: build-fe
build-fe: ## Production build the frontend (type-check + compile)
	cd "$(ROOT)/frontend" && NEXT_TELEMETRY_DISABLED=1 npm run build

.PHONY: check
check: test lint build-fe ## Full local gate: tests + lint + frontend build

# ── database ────────────────────────────────────────────────────────────────────
# The DB is a separate compose project so it outlives a dashboard redeploy, and it has no host port
# (ADR-001) — every target below goes through a container on rh-internal.
.PHONY: db-up
db-up: ## Start Postgres (generates db/.env with a fresh password on first run)
	bash "$(ROOT)/bin/db_up.sh"

.PHONY: db-down
db-down: ## Stop Postgres. Volume PRESERVED — data survives.
	docker compose -p rh-db -f "$(ROOT)/docker-compose.db.yml" down
	@echo "✓ Postgres stopped (volume rh_db_data preserved)"

.PHONY: db-status
db-status: ## Show applied vs pending migrations
	bash "$(ROOT)/bin/db_migrate.sh" status

.PHONY: db-migrate
db-migrate: ## Apply pending migrations
	bash "$(ROOT)/bin/db_migrate.sh" up

.PHONY: db-migrate-dry
db-migrate-dry: ## Print the migration plan without applying (also evaluates the destructive gate)
	bash "$(ROOT)/bin/db_migrate.sh" up --dry-run

.PHONY: db-psql
db-psql: ## Open psql against the database
	bash "$(ROOT)/bin/db_psql.sh"

# ── docker stack ────────────────────────────────────────────────────────────────
.PHONY: up
up: ## Pick fresh ports, build, start the stack + refresh daemon, print URLs
	bash "$(ROOT)/bin/up.sh"

.PHONY: down
down: ## Stop the stack and the refresh daemon
	$(load_ports)
	docker compose down
	pkill -f "bin/refresh_daemon.sh" 2>/dev/null || true
	@echo "✓ stack down, refresh daemon stopped"

.PHONY: restart-backend
restart-backend: ## Recreate just the backend (e.g. after editing backend/.env)
	$(load_ports)
	@# Parsed, never sourced. `source` executes the file as shell: an owner label containing a
	@# space took down four jobs on 2026-08-26 (see bin/lib_env.sh). Recipes run one line per
	@# shell, so this cannot use the bash library and repeats the parse inline.
	set -a; while IFS= read -r l; do case "$$l" in \#*|"") continue;; *=*) export "$${l%%=*}=$${l#*=}";; esac; done < "$(ROOT)/backend/.env"; set +a
	docker compose up -d --force-recreate backend

.PHONY: ps
ps: ## Show container status
	$(load_ports)
	docker compose ps

.PHONY: logs
logs: ## Tail backend logs (Ctrl-C to stop)
	$(load_ports)
	docker compose logs -f backend

.PHONY: ports
ports: ## Show the current dashboard + API URLs
	$(load_ports)
	if [ -z "$${FRONTEND_PORT:-}" ]; then echo "No .env.ports yet — run 'make up'."; exit 0; fi
	echo "Dashboard:   http://localhost:$${FRONTEND_PORT}"
	echo "Backend API: http://localhost:$${BACKEND_PORT}/api/health"

.PHONY: refresh-daemon
refresh-daemon: ## Start the host-side refresh daemon (Refresh button bridge)
	mkdir -p "$(ROOT)/logs/refresh"
	pkill -f "bin/refresh_daemon.sh" 2>/dev/null || true
	nohup bash "$(ROOT)/bin/refresh_daemon.sh" >"$(ROOT)/logs/refresh/daemon.out" 2>&1 &
	@echo "✓ refresh daemon started"

# ── dev servers (no docker; uses the venv) ───────────────────────────────────────
.PHONY: dev-backend
dev-backend: venv ## Run the backend locally with hot-reload (reads backend/.env)
	cd "$(ROOT)/backend" && PYTHONPATH="$(ROOT)" \
	  "$(ROOT)/$(VENV)/bin/uvicorn" app.main:app --reload --port 8000

.PHONY: dev-frontend
dev-frontend: ## Run the frontend dev server locally (expects backend on :8000)
	cd "$(ROOT)/frontend" && NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev

# ── quick probes against the running stack ───────────────────────────────────────
.PHONY: scan
scan: ## Local Sprinkle Sauce scan (no docker). Usage: make scan TICKER="AAPL NVDA"
	cd "$(ROOT)" && $(PY) -m src.daily_scan $(TICKER)

.PHONY: account
account: ## Print the live account summary from the running backend
	$(load_ports)
	curl -fsS "http://localhost:$${BACKEND_PORT}/api/account" | $(PY) -m json.tool

.PHONY: debate
debate: ## Stream a live debate. Usage: make debate TICKER=NVDA
	$(load_ports)
	curl -fsS -N -X POST "http://localhost:$${BACKEND_PORT}/api/debate/run-stream" \
	  -H 'Content-Type: application/json' -d "{\"ticker\":\"$(TICKER)\"}" --max-time 180

# ── cleanup ──────────────────────────────────────────────────────────────────────
.PHONY: clean
clean: ## Remove venv, build caches, and generated runtime files
	rm -rf "$(VENV)" "$(ROOT)/frontend/.next" "$(ROOT)/.env.ports"
	find "$(ROOT)" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "✓ cleaned (containers untouched — use 'make down' for those)"
