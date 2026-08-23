ENV_FILE ?= .env
COMPOSE = docker compose --env-file $(ENV_FILE)

.PHONY: config bootstrap up down smoke gitea-mcp-up gitea-mcp-bootstrap gitea-bridge-bootstrap gitea-bridge-up

config:
	$(COMPOSE) config --quiet

bootstrap:
	ENV_FILE=$(ENV_FILE) sh ./scripts/bootstrap.sh

up: bootstrap
	$(COMPOSE) up -d --wait --wait-timeout 90

down:
	$(COMPOSE) down

smoke:
	ENV_FILE=$(ENV_FILE) sh ./scripts/smoke.sh

gitea-mcp-up:
	ENV_FILE=$(ENV_FILE) sh ./scripts/gitea-mcp-preflight.sh
	$(COMPOSE) --profile gitea-mcp up -d --wait --wait-timeout 90

gitea-mcp-bootstrap:
	ENV_FILE=$(ENV_FILE) sh ./scripts/gitea-mcp-bootstrap.sh

gitea-bridge-bootstrap:
	ENV_FILE=$(ENV_FILE) sh ./scripts/gitea-bridge-bootstrap.sh

gitea-bridge-up: gitea-bridge-bootstrap
	@test -n "$$(awk -F= '$$1=="GITEA_BRIDGE_ISSUE_URL" {print substr($$0,length($$1)+2)}' $(ENV_FILE))"
	@test -n "$$(awk -F= '$$1=="GASCITY_BRIDGE_RUN_ID" {print substr($$0,length($$1)+2)}' $(ENV_FILE))"
	$(COMPOSE) --profile city --profile gitea-bridge up -d --build gitea-bridge
