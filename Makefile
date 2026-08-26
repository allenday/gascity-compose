ENV_FILE ?= .env
COMPOSE = docker compose --env-file $(ENV_FILE)

.PHONY: config bootstrap up down smoke gitea-mcp-up gitea-mcp-bootstrap gitea-bridge-bootstrap gitea-bridge-up gitea-intake-doctor gitea-mail-bridge-bootstrap gitea-mail-bridge-up gitea-mail-launcher-up gitea-mail-launcher-smoke gitea-mail-acceptance-demo gitea-mail-bridge-smoke woodpecker-fixture-bootstrap woodpecker-preflight woodpecker-up woodpecker-smoke woodpecker-acceptance test

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
	@test -n "$$(awk -F= '$$1=="GITEA_BRIDGE_RUN_ID" {print substr($$0,length($$1)+2)}' $(ENV_FILE))"
	$(COMPOSE) --profile city --profile gitea-bridge up -d --build gitea-bridge

gitea-intake-doctor:
	ENV_FILE=$(ENV_FILE) sh ./scripts/gitea-intake-doctor.sh

gitea-mail-bridge-bootstrap:
	ENV_FILE=$(ENV_FILE) sh ./scripts/gitea-mail-bridge-bootstrap.sh

gitea-mail-bridge-up: gitea-mail-bridge-bootstrap
	$(COMPOSE) --profile mcp --profile gitea-mcp --profile gitea-mail-bridge up -d --build --wait --wait-timeout 120 gitea-mcp-mayor gitea-mail-bridge

gitea-mail-launcher-up: gitea-mail-bridge-bootstrap
	$(COMPOSE) --profile mcp --profile gitea-mail-bridge up -d --build --wait --wait-timeout 180 gitea-mail-bridge city-mail-launcher

gitea-mail-launcher-smoke:
	ENV_FILE=$(ENV_FILE) sh ./scripts/gitea-mail-launcher-smoke.sh

gitea-mail-acceptance-demo:
	ENV_FILE=$(ENV_FILE) sh ./scripts/gitea-mail-acceptance-demo.sh

gitea-mail-bridge-smoke:
	ENV_FILE=$(ENV_FILE) sh ./scripts/gitea-mail-bridge-smoke.sh

woodpecker-fixture-bootstrap:
	ENV_FILE=$(ENV_FILE) sh ./scripts/woodpecker-fixture-bootstrap.sh

woodpecker-preflight:
	ENV_FILE=$(ENV_FILE) sh ./scripts/woodpecker-preflight.sh

woodpecker-up: bootstrap woodpecker-fixture-bootstrap woodpecker-preflight
	$(COMPOSE) --profile woodpecker up -d --wait --wait-timeout 90

woodpecker-smoke:
	ENV_FILE=$(ENV_FILE) sh ./scripts/woodpecker-smoke.sh

woodpecker-acceptance:
	ENV_FILE=$(ENV_FILE) sh ./scripts/woodpecker-acceptance.sh

test:
	sh ./scripts/tests/test_woodpecker_fixture.sh
	sh ./scripts/tests/test_gitea_mail_bridge.sh
	PYTHONDONTWRITEBYTECODE=1 python3 ./scripts/tests/test_city_mail_mcp_proxy.py
	PYTHONDONTWRITEBYTECODE=1 python3 ./scripts/tests/test_city_mail_launcher.py
