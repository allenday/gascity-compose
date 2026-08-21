ENV_FILE ?= .env
COMPOSE = docker compose --env-file $(ENV_FILE)

.PHONY: config up down smoke gitea-mcp-up

config:
	$(COMPOSE) config --quiet

up:
	$(COMPOSE) up -d --wait --wait-timeout 90

down:
	$(COMPOSE) down

smoke:
	ENV_FILE=$(ENV_FILE) sh ./scripts/smoke.sh

gitea-mcp-up:
	ENV_FILE=$(ENV_FILE) sh ./scripts/gitea-mcp-preflight.sh
	$(COMPOSE) --profile gitea-mcp up -d --wait --wait-timeout 90
