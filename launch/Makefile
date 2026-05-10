# Cognitive Core — public quickstart Makefile.
# All commands assume the launch/ directory is the working tree.
#
# Usage:
#   make init       # generate .env with random secrets
#   make up         # start the stack
#   make logs       # tail all containers
#   make ps         # show status + healthchecks
#   make smoke      # E2E smoke test (room create + post + ask)
#   make backup     # one-shot pg_dump → ./backups/
#   make down       # stop, keep data
#   make clean      # stop + DELETE volumes (DESTRUCTIVE)
#   make help

SHELL := /usr/bin/env bash
COMPOSE := docker compose -f docker-compose.public.yml
ENV_FILE := .env
EXAMPLE := .env.example
BACKUP_DIR := ./backups

.PHONY: help init up up-edge down restart ps logs logs-api logs-rooms smoke \
        backup restore clean pull build test fmt versions

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

init: ## Create .env from template with auto-generated secrets
	@if [ -f "$(ENV_FILE)" ]; then \
		echo "❌ $(ENV_FILE) exists — delete it first or edit manually."; \
		exit 1; \
	fi
	@cp $(EXAMPLE) $(ENV_FILE)
	@PG_PWD=$$(openssl rand -hex 24); \
	S3_KEY=$$(openssl rand -hex 12); \
	S3_SEC=$$(openssl rand -hex 24); \
	ALICE=$$(openssl rand -hex 16); \
	BOB=$$(openssl rand -hex 16); \
	sed -i.bak \
		-e "s|CHANGE_ME_postgres_password|$$PG_PWD|" \
		-e "s|CHANGE_ME_minio_access_key|$$S3_KEY|" \
		-e "s|CHANGE_ME_minio_secret_key|$$S3_SEC|" \
		-e "s|key-alice-CHANGE|$$ALICE|" \
		-e "s|key-bob-CHANGE|$$BOB|" \
		$(ENV_FILE); \
	rm -f $(ENV_FILE).bak
	@echo "✅ Generated $(ENV_FILE) with random secrets."
	@echo "⚠  Set DEEPSEEK_API_KEY manually (https://platform.deepseek.com/api_keys)."

up: $(ENV_FILE) ## Start the stack (no edge nginx)
	$(COMPOSE) up -d
	@$(MAKE) -s ps

up-edge: $(ENV_FILE) ## Start with nginx edge profile (TLS termination)
	$(COMPOSE) --profile edge up -d
	@$(MAKE) -s ps

down: ## Stop the stack (data preserved)
	$(COMPOSE) down

restart: ## Restart all services
	$(COMPOSE) restart

ps: ## Show container status
	@$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'

logs: ## Tail logs from all services
	$(COMPOSE) logs -f --tail 50

logs-api: ## Tail API logs only
	$(COMPOSE) logs -f --tail 100 api

logs-rooms: ## Tail rooms service logs
	$(COMPOSE) logs -f --tail 100 rooms

pull: ## Pull latest images
	$(COMPOSE) pull

build: ## Build local image (if you cloned the source repo)
	$(COMPOSE) build

backup: ## One-shot pg_dump → ./backups/cognitive_core_<ts>.sql.gz
	@mkdir -p $(BACKUP_DIR)
	@TS=$$(date -u +%Y%m%dT%H%M%SZ); \
	OUT="$(BACKUP_DIR)/cognitive_core_$$TS.sql.gz"; \
	$(COMPOSE) exec -T postgres pg_dump -U $${POSTGRES_USER:-cognitive} $${POSTGRES_DB:-cognitive_core} | gzip > "$$OUT"; \
	ls -lh "$$OUT"; \
	echo "✅ backup → $$OUT"

restore: ## Restore from FILE=./backups/xxx.sql.gz
	@test -n "$(FILE)" || (echo "Usage: make restore FILE=./backups/xxx.sql.gz" && exit 1)
	@gunzip -c "$(FILE)" | $(COMPOSE) exec -T postgres psql -U $${POSTGRES_USER:-cognitive} $${POSTGRES_DB:-cognitive_core}
	@echo "✅ restored from $(FILE)"

smoke: ## E2E smoke test (creates room, posts, asks)
	@bash ./scripts/smoke-test.sh

clean: ## DESTRUCTIVE: stop + delete volumes (loses all data)
	@read -p "⚠  Delete all data volumes? Type YES to confirm: " ans; \
	if [ "$$ans" = "YES" ]; then \
		$(COMPOSE) down -v; \
		echo "✅ stack down + volumes purged."; \
	else \
		echo "Aborted."; \
	fi

versions: ## Print versions of stack components
	@echo "Docker:  $$(docker --version)"
	@echo "Compose: $$(docker compose version --short)"
	@$(COMPOSE) images --format 'table {{.Service}}\t{{.Repository}}:{{.Tag}}'

$(ENV_FILE):
	@echo "❌ $(ENV_FILE) not found. Run: make init"
	@exit 1
