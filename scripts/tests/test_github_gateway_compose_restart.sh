#!/bin/sh
# Exercise the production gateway process across real Compose service restarts.
# GitHub and City mutation boundaries are replaced with local filesystem
# fixtures, so this test cannot contact GitHub or mutate a developer City.
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)

if ! docker info >/dev/null 2>&1; then
  echo 'github gateway Compose restart: skipped (Docker daemon unavailable)'
  exit 0
fi
if ! docker image inspect python:3.12-alpine >/dev/null 2>&1; then
  echo 'github gateway Compose restart: skipped (local python:3.12-alpine image unavailable)'
  exit 0
fi

temp=$(mktemp -d)
project="gateway-restart-$$"
override="$temp/compose.yaml"
compose="docker compose -p $project --env-file $root/.env.example -f $root/compose.yaml -f $override --profile github-docs-impact"

cleanup() {
  FIXTURE_ROOT="$temp" $compose down --remove-orphans --volumes >/dev/null 2>&1 || true
  rm -rf "$temp"
}
trap cleanup EXIT INT TERM

mkdir -p "$temp/app" "$temp/pack/github/scripts" "$temp/state/docs-review"

cp "$root/scripts/github_durable_gateway.py" "$temp/app/github_durable_gateway.py"
cp "$root/scripts/github_docs_impact_webhook.py" "$temp/app/github_docs_impact_webhook.py"

cat > "$temp/pack/github/scripts/github_intake_common.py" <<'PY'
import hashlib
import hmac


def load_effective_config():
    return {"app": {"app_id": "1", "installation_id": "23", "private_key_pem": "fixture", "webhook_secret": "fixture-secret"}}


def verify_github_signature(secret, payload, signature):
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def create_installation_token(_app, _installation_id):
    return "fixture-token"
PY

cat > "$temp/app/github_docs_impact_compose_adapter.py" <<'PY'
#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import sqlite3
import sys


state = pathlib.Path(os.environ["GC_SERVICE_STATE_ROOT"])
review = pathlib.Path(os.environ["GC_GITHUB_DOCS_REVIEW_RUNS_DIR"])
database = state / "gateway.sqlite"


def write_once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
    except FileExistsError:
        pass


with sqlite3.connect(database) as connection:
    row = connection.execute(
        """
        SELECT jobs.delivery_id, jobs.kind, deliveries.payload
        FROM jobs JOIN deliveries USING (delivery_id)
        WHERE jobs.status = 'leased'
        ORDER BY jobs.id LIMIT 1
        """
    ).fetchone()
if row is None:
    raise SystemExit("fixture adapter found no leased job")
delivery_id, kind, raw = row
payload = json.loads(raw)
repository_id = str(payload["repository"]["id"])
pull_request = payload["pull_request"]
number = pull_request["number"]
sha = pull_request["head"]["sha"].lower()
base = pull_request["base"]
base_binding = json.dumps(
    {"base_ref": str(base["ref"]), "base_sha": str(base["sha"]).lower()},
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
source_key = f"github-pr:v2:{repository_id}:{number}:{sha}:{hashlib.sha256(base_binding).hexdigest()}"
source_digest = hashlib.sha256(source_key.encode()).hexdigest()
write_once(review / "effects" / source_digest / f"{kind}.json", {"first_delivery_id": delivery_id, "kind": kind, "source_key": source_key})

if kind == "dispatch":
    write_once(review / "requests" / f"{source_digest}.json", {"first_delivery_id": delivery_id, "source_key": source_key})
elif kind == "harvest":
    write_once(review / "candidates" / f"{source_digest}.json", {"artifact": {"identity": {"source_key": source_key}}})
elif kind == "project":
    write_once(review / "runs" / f"{source_digest}.json", {"identity": source_key, "pending_actions": [], "state": "terminal"})

print("{}")
PY
chmod +x "$temp/app/github_docs_impact_compose_adapter.py"

cat > "$temp/app/city_fixture.py" <<'PY'
import json
import pathlib
import time


root = pathlib.Path("/state/docs-review")
while True:
    if pathlib.Path("/state/city-ready").exists():
        for request in (root / "requests").glob("*.json"):
            value = json.loads(request.read_text(encoding="utf-8"))
            marker = root / "dispatch" / request.name
            marker.parent.mkdir(parents=True, exist_ok=True)
            if not marker.exists():
                marker.write_text(json.dumps({"bead_id": "fixture-bead-" + request.stem, "dispatched": True, "source_key": value["source_key"]}), encoding="utf-8")
    time.sleep(0.05)
PY

cat > "$override" <<'YAML'
services:
  github-webhook:
    image: python:3.12-alpine
    build: !reset null
    user: "0:0"
    entrypoint: ["python3", "-u", "/app/github_docs_impact_webhook.py"]
    environment: !override
      GC_SERVICE_HOST: 0.0.0.0
      GC_SERVICE_PORT: "8080"
      GC_SERVICE_STATE_ROOT: /state
      GC_GITHUB_PACK_SCRIPTS: /fixture-pack/github/scripts
      GC_GITHUB_DOCS_REVIEW_RUNS_DIR: /state/docs-review
      GC_GITHUB_DOCS_CANDIDATE_DIR: /state/docs-review/candidates
      GC_GITHUB_DOCS_ASSIGNMENT_DIR: /state/docs-review/assignments
      GC_GITHUB_GATEWAY_POLL_SECONDS: "0.1"
      GC_GITHUB_GATEWAY_STALL_SECONDS: "60"
      HOME: /state/home
    volumes: !override
      - ${FIXTURE_ROOT}/app:/app:ro
      - ${FIXTURE_ROOT}/pack:/fixture-pack:ro
      - ${FIXTURE_ROOT}/state:/state
    healthcheck: !reset null
  city:
    image: python:3.12-alpine
    build: !reset null
    user: "0:0"
    entrypoint: ["python3", "-u", "/app/city_fixture.py"]
    environment: !override {}
    volumes: !override
      - ${FIXTURE_ROOT}/app:/app:ro
      - ${FIXTURE_ROOT}/state:/state
    ports: !reset []
    healthcheck: !reset null
YAML

FIXTURE_ROOT="$temp" $compose config --quiet
FIXTURE_ROOT="$temp" $compose up -d --no-build --pull never --no-deps github-webhook city >/dev/null

wait_for() {
  description=$1
  command=$2
  attempts=0
  until sh -c "$command"; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 120 ]; then
      echo "timed out waiting for $description" >&2
      FIXTURE_ROOT="$temp" $compose ps >&2 || true
      FIXTURE_ROOT="$temp" $compose logs --no-color github-webhook city >&2 || true
      exit 1
    fi
    sleep 0.25
  done
}

wait_for 'gateway HTTP listener' "FIXTURE_ROOT='$temp' $compose exec -T github-webhook python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')\" >/dev/null 2>&1"

payload='{"action":"opened","installation":{"id":23},"repository":{"full_name":"example/docs","id":17},"pull_request":{"base":{"ref":"main","repo":{"full_name":"example/docs","id":17},"sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"head":{"ref":"feature/docs","repo":{"full_name":"example/docs","id":17},"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"number":9}}'
signature=$(PAYLOAD="$payload" python3 -c 'import hashlib,hmac,os; print("sha256=" + hmac.new(b"fixture-secret", os.environ["PAYLOAD"].encode(), hashlib.sha256).hexdigest())')
response=$(printf '%s' "$payload" | FIXTURE_ROOT="$temp" $compose exec -T -e SIGNATURE="$signature" github-webhook python3 -c 'import os,sys,urllib.request; body=sys.stdin.buffer.read(); req=urllib.request.Request("http://127.0.0.1:8080/v0/github/webhook", data=body, headers={"Content-Type":"application/json","X-GitHub-Delivery":"compose-delivery-1","X-GitHub-Event":"pull_request","X-Hub-Signature-256":os.environ["SIGNATURE"]}); print(urllib.request.urlopen(req).read().decode())')
printf '%s' "$response" | grep -q '"accepted": true'

wait_for 'durable webhook receipt' "python3 - '$temp/state/gateway.sqlite' <<'PY'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
    raise SystemExit(db.execute(\"SELECT COUNT(*) FROM deliveries WHERE delivery_id='compose-delivery-1'\").fetchone()[0] != 1)
PY"

gateway_id=$(FIXTURE_ROOT="$temp" $compose ps -q github-webhook)
city_id=$(FIXTURE_ROOT="$temp" $compose ps -q city)
FIXTURE_ROOT="$temp" $compose stop city >/dev/null
FIXTURE_ROOT="$temp" $compose up -d --no-build --pull never --no-deps --force-recreate city >/dev/null
[ "$(FIXTURE_ROOT="$temp" $compose ps -q github-webhook)" = "$gateway_id" ] || { echo 'City recreation replaced the gateway service' >&2; exit 1; }
[ "$(FIXTURE_ROOT="$temp" $compose ps -q city)" != "$city_id" ] || { echo 'City service was not recreated' >&2; exit 1; }
python3 -c "import pathlib; pathlib.Path('$temp/state/city-ready').touch()"

wait_for 'first delivery completion after City recreation' "python3 - '$temp/state/gateway.sqlite' <<'PY'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
    rows=db.execute(\"SELECT status FROM jobs WHERE delivery_id='compose-delivery-1'\").fetchall()
raise SystemExit(len(rows) != 4 or any(row[0] != 'complete' for row in rows))
PY"

# Persist an already-leased second intake, then restart the real gateway
# service. Its expired lease must be reclaimed without duplicate effects.
FIXTURE_ROOT="$temp" $compose stop github-webhook >/dev/null
ROOT="$root" STATE="$temp/state" PAYLOAD="$payload" PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import importlib.util
import os
import pathlib
import sqlite3
import sys
import time

path = pathlib.Path(os.environ["ROOT"]) / "scripts/github_durable_gateway.py"
spec = importlib.util.spec_from_file_location("compose_restart_gateway", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
store = module.GatewayStore(os.environ["STATE"])
now = int(time.time())
assert store.enqueue_delivery("compose-delivery-2", "pull_request", os.environ["PAYLOAD"].encode(), now - 31)
leased = store.claim(now - 31)
assert leased is not None and leased.delivery_id == "compose-delivery-2"
with sqlite3.connect(store.path) as connection:
    assert connection.execute("SELECT status FROM jobs WHERE id = ?", (leased.id,)).fetchone() == ("leased",)
PY
FIXTURE_ROOT="$temp" $compose up -d --no-build --pull never --no-deps github-webhook >/dev/null

wait_for 'leased delivery completion after gateway restart' "python3 - '$temp/state/gateway.sqlite' <<'PY'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
    rows=db.execute(\"SELECT status FROM jobs WHERE delivery_id='compose-delivery-2'\").fetchall()
raise SystemExit(len(rows) != 4 or any(row[0] != 'complete' for row in rows))
PY"

python3 - "$temp/state" <<'PY'
import pathlib
import sqlite3
import sys
import hashlib
import json

state = pathlib.Path(sys.argv[1])
base_binding = json.dumps(
    {"base_ref": "main", "base_sha": "b" * 40},
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
source_key = "github-pr:v2:17:9:" + "a" * 40 + ":" + hashlib.sha256(base_binding).hexdigest()
source_digest = hashlib.sha256(source_key.encode()).hexdigest()
effects = sorted(path.name for path in (state / "docs-review/effects" / source_digest).glob("*.json"))
assert effects == ["dispatch.json", "harvest.json", "intake.json", "project.json"], effects
assert len(list((state / "docs-review/requests").glob("*.json"))) == 1
assert len(list((state / "docs-review/dispatch").glob("*.json"))) == 1
assert len(list((state / "docs-review/candidates").glob("*.json"))) == 1
assert len(list((state / "docs-review/runs").glob("*.json"))) == 1
with sqlite3.connect(state / "gateway.sqlite") as database:
    assert database.execute("SELECT COUNT(*) FROM jobs WHERE status != 'complete'").fetchone() == (0,)
PY

echo 'github gateway Compose restart: passed'
