#!/bin/sh
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
compose="$root/compose.yaml"
rules="$root/config/github-intake/rules.toml"
ci_workflow="$root/.github/workflows/ci.yml"

require() {
  local pattern=$1 file=$2
  grep -Eq "$pattern" "$file" || { echo "missing $pattern in $file" >&2; exit 1; }
}

require 'github-webhook:' "$compose"
require 'profiles: \[github-docs-impact\]' "$compose"
require 'GC_SERVICE_HOST: 0.0.0.0' "$compose"
require 'GC_SERVICE_PORT: "8080"' "$compose"
require 'HOME: /var/lib/github-intake/home' "$compose"
require 'GITHUB_WEBHOOK_WAN_IP:-127.0.0.1' "$compose"
require 'GITHUB_WEBHOOK_WAN_PORT:-8088' "$compose"
if grep -Fq '${GITHUB_PACK_DIR:-' "$compose"; then
  echo 'github docs-impact mounts must not fall back to a sibling pack checkout' >&2
  exit 1
fi
require 'GITHUB_PACK_DIR:\?Set GITHUB_PACK_DIR in \.env to the absolute GitHub pack checkout' "$compose"
# Keep CI's fixture environment aligned with compose's required reviewer
# inputs, so interpolation failures are caught before GitHub Actions runs.
require 'GC_CITY_DOCS_REVIEW_RIG_DIR: \$\{\{ github\.workspace \}\}/fixture-my-project' "$ci_workflow"
require 'GC_CITY_DOCS_REVIEW_TARGET: my-project/github-docs-impact\.docs-impact-reviewer' "$ci_workflow"
require 'GC_CITY_DOCS_JOURNEY_TARGET: my-project/github-docs-impact\.docs-journey' "$ci_workflow"
require 'github-webhook:8080' "$root/nginx/nginx.conf"
require 'location = /v0/github/webhook' "$root/nginx/nginx.conf"
require 'action = "opened"' "$rules"
require 'action = "reopened"' "$rules"
require 'action = "synchronize"' "$rules"
require 'action = "ready_for_review"' "$rules"
require 'github_docs_impact_compose_adapter.py", "intake", "--once' "$rules"
require 'github_app_token_env = "GH_TOKEN"' "$rules"
require 'libicu72' "$root/Dockerfile.city"
require 'BEADS_VERSION=v1.1.1-0.20260805093327-bf97b73749ac' "$root/Dockerfile.city"

# The profile service deliberately has no `ports:` declaration. Its only
# reachable path is Nginx, which can then be scoped by Tailscale Funnel.
if awk '/^  github-webhook:/{inside=1; next} inside && /^  [^ ]/{exit} inside && /^    ports:/{found=1} END{exit !found}' "$compose"; then
  echo 'github-webhook must not publish a host port' >&2
  exit 1
fi

webhook_block=$(awk '/^  github-webhook:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-webhook:/{exit}' "$compose")
[ -n "$webhook_block" ] || { echo 'github-webhook service block is missing' >&2; exit 1; }
reviewer_block=$(awk '/^  github-docs-techdocs-reviewer:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-docs-techdocs-reviewer:/{exit}' "$compose")
egress_block=$(awk '/^  github-docs-model-egress:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-docs-model-egress:/{exit}' "$compose")
city_block=$(awk '/^  city:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  city:/{exit}' "$compose")
[ -n "$city_block" ] || { echo 'city service block is missing' >&2; exit 1; }
[ -z "$reviewer_block" ] || { echo 'legacy cagent reviewer service must be removed' >&2; exit 1; }
[ -z "$egress_block" ] || { echo 'legacy OpenAI model egress service must be removed' >&2; exit 1; }

require_webhook() {
  pattern=$1
  printf '%s\n' "$webhook_block" | grep -Eq "$pattern" || {
    echo "missing $pattern in github-webhook" >&2
    exit 1
  }
}

forbid_webhook() {
  pattern=$1
  if printf '%s\n' "$webhook_block" | grep -Eq -- "$pattern"; then
    echo "forbidden $pattern in github-webhook" >&2
    exit 1
  fi
}

require_city() {
  pattern=$1
  printf '%s\n' "$city_block" | grep -Eq "$pattern" || {
    echo "missing $pattern in city" >&2
    exit 1
  }
}

forbid_city() {
  pattern=$1
  if printf '%s\n' "$city_block" | grep -Eq "$pattern"; then
    echo "forbidden $pattern in city" >&2
    exit 1
  fi
}

# Codex authentication stays in the already-trusted City runtime. The City
# receives the persisted immutable assignment only after the runtime records it.
require_city 'CODEX_AUTH_FILE.*:/run/secrets/codex-auth.json:ro'
require_city 'GC_CITY_DOCS_REVIEW_ENABLED:.*true'
require_city 'GC_CITY_DOCS_REVIEW_TARGET:.*GC_CITY_DOCS_REVIEW_TARGET'
require_city 'GC_CITY_DOCS_JOURNEY_TARGET:.*GC_CITY_DOCS_JOURNEY_TARGET'
require_city 'docs-review:/var/lib/github-docs-impact/review'
require_city 'GITHUB_PACK_DIR.*:/opt/gascity-packs:ro'
# City runs as HOST_UID:GID, so its supervisor state and real home must not
# live below /root (which an unprivileged container user cannot traverse).
require_city 'GC_HOME: /var/lib/gascity'
require_city 'HOME: /var/lib/gascity/home'
require_city 'state/gc-runtime:/var/lib/gascity'
forbid_city 'GC_HOME: /root'
forbid_city 'HOME: /root'
forbid_city 'GITHUB_(APP|WEBHOOK|TOKEN|INTAKE).*:'

if grep -Fq 'github_intake_city_docs_launcher.py' "$root/docker/city-entrypoint.sh"; then
  echo 'retired City docs launcher must be absent' >&2
  exit 1
fi
require '/opt/gascity-packs/github' "$root/docker/city-entrypoint.sh"
require 'gc --city "\$CITY_PATH" import add /opt/gascity-packs/github --name github-docs-impact' "$root/docker/city-entrypoint.sh"
if grep -Fq 'gc --city "$CITY_PATH" --rig "$GC_CITY_DOCS_REVIEW_RIG_DIR" import add /opt/gascity-packs/github' "$root/docker/city-entrypoint.sh"; then
  echo 'github docs-impact is city-scoped and must not be imported into a rig' >&2
  exit 1
fi
require 'github-docs-impact-city-dispatcher' "$root/docker/city-entrypoint.sh"
require '127\.0\.0\.1:8372/api/health' "$root/docker/city-entrypoint.sh"
require 'github_docs_impact_city_dispatcher.py' "$root/Dockerfile.city"
require 'city-docs-impact.toml' "$root/docker/city-entrypoint.sh"
require '^\[providers\.codex-docs-impact\]$' "$root/config/city-docs-impact.toml"
require '^name = "github-docs-impact\.docs-impact-reviewer"$' "$root/config/city-docs-impact.toml"
require '^name = "github-docs-impact\.docs-journey"$' "$root/config/city-docs-impact.toml"
require '^work_dir = "\.gc/agents/\{\{\.AgentBase\}\}"$' "$root/config/city-docs-impact.toml"
require 'gpt-5\.6-terra' "$root/config/city-docs-impact.toml"
require 'model_reasoning_effort=medium' "$root/config/city-docs-impact.toml"

if [ "$(grep -Fc '/run/secrets/codex-auth.json' "$compose")" -ne 1 ]; then
  echo 'Codex auth must be mounted only into the trusted City service' >&2
  exit 1
fi
if grep -Eq 'GC_TECHDOCS_MODEL_(TOKEN|ENDPOINT)|github_docs_model_egress_proxy|github_intake_city_techdocs_adapter|github-docs-model' "$compose"; then
  echo 'legacy OpenAI API-key/cagent reviewer route must be absent' >&2
  exit 1
fi

# One profile activates the complete trusted review path. City remains
# profile-gated, so this does not weaken the one-supervisor-at-a-time guard.
require '^    profiles: \[city, github-docs-impact\]$' "$compose"
profile_services=$(docker compose --env-file "$root/.env.example" --profile github-docs-impact config --services)
for expected in city github-webhook; do
  printf '%s\n' "$profile_services" | grep -Fx "$expected" >/dev/null || {
    echo "github-docs-impact profile does not activate $expected" >&2
    exit 1
  }
done

# The authenticated webhook rule builds complete paginated evidence, persists
# the run, and asks the runtime adapter to dispatch only after that record.
require_webhook 'GC_GITHUB_DOCS_REVIEW_RUNS_DIR: /var/lib/github-intake/docs-review'
require_webhook 'GC_GITHUB_DOCS_CANDIDATE_DIR: /var/lib/github-intake/docs-review/candidates'
require_webhook 'GC_SERVICE_STATE_ROOT: /var/lib/github-intake'
require_webhook 'scripts/github_docs_impact_webhook.py:/opt/gascity-compose/scripts/github_docs_impact_webhook.py:ro'
require_webhook 'scripts/github_durable_gateway.py:/opt/gascity-compose/scripts/github_durable_gateway.py:ro'
require_webhook 'scripts/github_docs_impact_compose_adapter.py:/opt/gascity-compose/scripts/github_docs_impact_compose_adapter.py:ro'
require 'github_docs_impact_compose_adapter.py.*intake.*--once' "$rules"
if printf '%s\n' "$webhook_block" | grep -Eq 'network_mode: "service:city"'; then
  echo 'github-webhook must not share City network mode' >&2
  exit 1
fi
require_webhook 'github_docs_impact_webhook.py'
for forbidden in 'GC_CITY_ROOT:' 'GC_HOME:' 'BEADS_DIR:' 'GC_GITHUB_INTAKE_DIRECT_BD:' 'GC_CITY_DOCS_REVIEW_RIG_DIR:' 'CITY_DIR.*:.*CITY_DIR' 'GC_CITY_DOCS_REVIEW_RIG_DIR.*:.*GC_CITY_DOCS_REVIEW_RIG_DIR' 'state/gc-runtime:/var/lib/gascity' '- \./:/opt/gascity-compose:ro'; do
  forbid_webhook "$forbidden"
done

# This local fixture exercises the real durable gateway store while keeping the
# City and GitHub boundaries deterministic.  It proves that recreating City
# cannot remove an accepted delivery, and that a retry adopts the controller's
# already-persisted source-branch follow-up intent rather than creating a
# second one.  It deliberately makes no GitHub request.
PYTHONDONTWRITEBYTECODE=1 ROOT="$root" python3 - <<'PY'
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
from unittest import mock

root = pathlib.Path(os.environ["ROOT"])
module_path = root / "scripts/github_durable_gateway.py"
spec = importlib.util.spec_from_file_location("github_durable_gateway_smoke", module_path)
assert spec and spec.loader
gateway = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gateway
spec.loader.exec_module(gateway)

sha = "a" * 40
source_key = f"github-pr:17:9:{sha}"
payload = json.dumps({
    "installation": {"id": 17},
    "action": "opened",
    "repository": {"id": 17, "full_name": "example/docs"},
    "pull_request": {
        "number": 9,
        "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 17, "full_name": "example/docs"}},
        "head": {"sha": sha, "ref": "feature/docs", "repo": {"id": 17, "full_name": "example/docs"}},
    },
}).encode()

with tempfile.TemporaryDirectory() as temporary:
    state_root = pathlib.Path(temporary) / "github-intake"
    city_root = pathlib.Path(temporary) / "city"
    store = gateway.GatewayStore(state_root)
    assert store.enqueue_delivery("city-restart-delivery", "pull_request", payload, 100)

    # Recreate City only; the host-mounted gateway SQLite file remains intact.
    city_root.mkdir()
    shutil.rmtree(city_root)
    city_root.mkdir()
    accepted_before_city_restart = gateway.GatewayStore(state_root)
    queued = accepted_before_city_restart.claim(101)
    assert queued is not None and queued.kind == "intake" and queued.payload == payload

with tempfile.TemporaryDirectory() as temporary:
    state_root = pathlib.Path(temporary) / "github-intake"
    review_root = pathlib.Path(temporary) / "docs-review"
    city_root = pathlib.Path(temporary) / "city"
    store = gateway.GatewayStore(state_root)
    assert store.enqueue_delivery("followup-retry-delivery", "pull_request", payload, 100)

    def run_adapter(job):
        if job.kind == "dispatch":
            marker = review_root / "dispatch/assignment.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"source_key": source_key, "dispatched": True}), encoding="utf-8")
        elif job.kind == "harvest":
            candidate = review_root / "candidates/assignment.json"
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(json.dumps({"artifact": {"identity": {"source_key": source_key}}}), encoding="utf-8")
        elif job.kind == "project":
            run = review_root / "runs" / f"{hashlib.sha256(source_key.encode()).hexdigest()}.json"
            run.parent.mkdir(parents=True, exist_ok=True)
            if not run.exists():
                followup = {"source_key": source_key, "state": "intent-persisted"}
                # The external controller saves its intent before its GitHub
                # mutation.  Simulate City disappearing immediately after it.
                run.write_text(json.dumps({"identity": source_key, "state": "terminal", "pending_actions": [], "followup": followup}), encoding="utf-8")
                raise OSError("City recreated after durable follow-up intent")
            persisted = json.loads(run.read_text(encoding="utf-8"))
            assert persisted["followup"] == {"source_key": source_key, "state": "intent-persisted"}
        return {}

    environment = {
        "GC_GITHUB_DOCS_ASSIGNMENT_DIR": str(review_root / "assignments"),
        "GC_GITHUB_DOCS_CANDIDATE_DIR": str(review_root / "candidates"),
        "GC_GITHUB_DOCS_REVIEW_RUNS_DIR": str(review_root),
    }
    with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(gateway, "_run_adapter", side_effect=run_adapter):
        for now in range(100, 103):
            assert gateway.process_one(store, now, clock=lambda now=now: now)
        assert not gateway.process_one(store, 103, clock=lambda: 103)

        # Recreate City only, then reopen the same mounted gateway state and
        # retry after its bounded backoff.  The persisted intent is adopted.
        city_root.mkdir()
        shutil.rmtree(city_root)
        city_root.mkdir()
        restarted_city_store = gateway.GatewayStore(state_root)
        assert gateway.process_one(restarted_city_store, 105, clock=lambda: 105)
        assert not gateway.process_one(restarted_city_store, 106, clock=lambda: 106)

    persisted_runs = list((review_root / "runs").glob("*.json"))
    assert len(persisted_runs) == 1
    persisted = json.loads(persisted_runs[0].read_text(encoding="utf-8"))
    assert persisted["followup"] == {"source_key": source_key, "state": "intent-persisted"}
    with sqlite3.connect(state_root / "gateway.sqlite") as connection:
        assert connection.execute("SELECT status, attempts FROM jobs WHERE kind = 'project'").fetchone() == ("complete", 1)
PY

sh "$root/scripts/tests/test_github_gateway_compose_restart.sh"
