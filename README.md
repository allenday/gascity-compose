# gascity-compose

A host-mounted Docker Compose deployment for a Gas City installation and its operational
dependencies: Ollama, OpenTelemetry Collector, Prometheus, Loki, Grafana, Gatus, node exporter,
Gitea, and optional MCP services.

It is a deployment kit, not an official Gas City distribution. Gas City currently has no general
purpose published container image, so the `city` profile builds a small controller image from the
upstream tagged source. The City mount and its state are never copied into an image.

## Quick start

```bash
cp .env.example .env
# Choose a non-secret Grafana password and set CITY_DIR to an absolute path.
docker compose up -d
```

This starts the platform services, including a fresh Gitea at `http://127.0.0.1:3002`, Grafana at
`http://127.0.0.1:3000`, Gatus at `http://127.0.0.1:8080`, and Prometheus at
`http://127.0.0.1:9090`. Ollama is in the explicit `models` profile because its current base image
is roughly 7 GB; enable it only when you intend to use Docker-hosted inference.

All mutable state is bind-mounted beneath `./state`; no named Docker volumes are used.

After startup, run `make smoke ENV_FILE=.env` to verify the exposed platform endpoints and
Prometheus scrape targets.

## Start the City safely

The `city` service is profile-gated because two supervisors must never reconcile the same mounted
City. Stop the existing host supervisor first, then start it in Compose:

```bash
docker compose --profile city up -d --build
```

### Cost-safe inference policy

This repository includes an opinionated Gastown role policy at
`config/city-cost-safe.toml`. It keeps continuous operational roles on the
`tailnet` Codex profile and reserves the paid `runpod` profile for the Mayor:

| Roles | Default profile |
| --- | --- |
| Deacon, Boot, Dog, Witness, Refinery | `tailnet` (local Ollama) |
| Mayor | `runpod`, `on_demand` |
| Polecats and other implementation agents | the City workspace provider (normally Codex) |

To apply it, copy the fragment into the City and include it near the top of
`city.toml`:

```toml
include = [".gc/compose/city-cost-safe.toml"]
```

The included Codex profiles are installed into the City container's ephemeral
`CODEX_HOME` on every start. Runpod remains configured with zero minimum
workers; an idle City therefore does not require a paid GPU. Before assigning
the Mayor to a serverless OpenAI-compatible endpoint, verify a complete
streaming Responses turn, including the terminal `response.completed` event.

Profile endpoints and the shared model name are linked directly to Compose
variables rather than hard-coded in TOML:

| Variable | Used by | Example |
| --- | --- | --- |
| `TAILNET_OLLAMA_BASE_URL` | `tailnet` profile | `http://100.64.0.1:11435/v1` |
| `RUNPOD_OPENAI_BASE_URL` | `runpod` profile | `https://api.runpod.ai/v2/<id>/openai/v1` |
| `GEMMA_MODEL` | both profiles | `gemma4:12b-it-qat` |
| `RUNPOD_API_KEY` | Runpod authentication | supplied from the host environment, not `.env` |

The checked-in files under `codex/` are templates. `city-entrypoint` renders
them at container startup, so changing a profile is a normal `.env` edit plus
a City service recreation; no generated TOML is committed.

#### Known-working Runpod Gemma 4 12B QAT deployment

The following 24 GB configuration was established through live deployment and
inference testing. The important compatibility choices are the pinned Runpod
vLLM worker and Google's official QAT checkpoint; the stock/current vLLM path
tested previously was incompatible with this model.

```bash
template_id="$(runpodctl template create \
  --serverless \
  --name gascity-gemma4-vllm-026 \
  --image runpod/worker-v1-vllm:v2.24.0 \
  --container-disk-in-gb 40 \
  --ports '8888/http,22/tcp' \
  --env '{
    "MODEL_NAME":"google/gemma-4-12B-it-qat-w4a16-ct",
    "OPENAI_SERVED_MODEL_NAME_OVERRIDE":"gemma4:12b-it-qat",
    "MAX_MODEL_LEN":"16384",
    "GPU_MEMORY_UTILIZATION":"0.90",
    "MAX_NUM_SEQS":"4",
    "MAX_CONCURRENCY":"4"
  }' | jq -r '.id')"

endpoint_id="$(runpodctl serverless create \
  --name gascity-gemma4-12b-qat \
  --template-id "$template_id" \
  --gpu-id 'NVIDIA GeForce RTX 4090' \
  --gpu-count 1 \
  --min-cuda-version 12.0 \
  --model-reference \
    'https://huggingface.co/google/gemma-4-12B-it-qat-w4a16-ct:main' \
  --workers-min 0 \
  --workers-max 1 \
  --idle-timeout 30 \
  --execution-timeout 900 \
  --scale-by delay \
  --scale-threshold 4 \
  --flash-boot=false | jq -r '.id')"

printf 'RUNPOD_OPENAI_BASE_URL=https://api.runpod.ai/v2/%s/openai/v1\n' \
  "$endpoint_id"
```

The model reference is not optional for a reliable scale-to-zero deployment.
It resolves `main` to an immutable Hugging Face revision and places the weights
in Runpod's host-side model cache. Without it, cold workers download the model
themselves; a live recreation failed repeatedly while fetching Hugging Face
metadata and cycled paid workers without serving its one queued request.

This exact image/checkpoint/cache combination successfully served
`gemma4:12b-it-qat` through the OpenAI-compatible `/responses` route on a
secure-cloud RTX 4090. The endpoint is intentionally scale-to-zero. Do not use
`workers-min=1` outside a deliberate warm-worker test.

Observed acceptance results on 2026-08-23:

- cache reference pinned to revision `1d2c2d7f2466070e69d6fb3fd5ce9a7d75f2f6ee`;
- the 9.56 GiB checkpoint loaded in 2.69 seconds from cache;
- asynchronous cold warm-up completed after 301 seconds of delay and 1.64
  seconds of execution;
- warm non-streaming `/responses` returned `status=completed`;
- warm streaming `/responses` emitted the terminal `response.completed` event.

Before putting the endpoint into `.env`, test both a non-streaming response and
the streaming completion boundary:

```bash
openai_base="https://api.runpod.ai/v2/${endpoint_id}/openai/v1"

curl -fsS "$openai_base/responses" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma4:12b-it-qat","input":"Reply with exactly OK","stream":false}' \
  | jq -e '.status == "completed"'

curl -fsSN "$openai_base/responses" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma4:12b-it-qat","input":"Reply with exactly OK","stream":true}' \
  | tee /tmp/gemma4-responses.sse

grep -q '^event: response.completed' /tmp/gemma4-responses.sse
```

Treat the final `grep` as a deployment gate. Our one-shot request worked, but
long-lived Codex sessions later encountered streams closing before
`response.completed`; retries then accumulated hundreds of serverless jobs.
The supplied Codex Runpod provider therefore sets both request and stream
retries to zero. If the streaming gate fails, delete the endpoint rather than
connecting it to an always-on or unattended agent.

After setting `RUNPOD_OPENAI_BASE_URL`, recreate only the City service to render
the new profile:

```bash
docker compose --profile city up -d --build city
```

The default City mount preserves its original absolute path inside the container. That is required
because an existing `city.toml` can contain absolute rig and provider paths. Containerizing the
supervisor does not automatically containerize host-specific providers (such as a wrapper that
calls a local binary); move those providers to service-network addresses before enabling workers.

Gas City publishes OTLP only when `GC_OTEL_METRICS_URL` and `GC_OTEL_LOGS_URL` are set. This stack
sets both for the containerized supervisor. The collector turns OTLP metrics into Prometheus
scrapes and forwards OTLP logs to Loki. Grafana provisions a City operations dashboard and the
stock Ollama Metrics dashboard; Grafana Explore remains available for raw logs.

## Gitea adoption

The default state path is `./state/gitea`, so it does not touch the existing QA fixture. To adopt
the existing fixture, stop its current Compose project first, set this in `.env`, and use its
existing ports if desired:

```dotenv
GITEA_STATE_DIR=/Users/gastown/src/gascity-gitea-contrib/test/gitea/state
GITEA_HTTP_PORT=3001
GITEA_SSH_PORT=2221
```

The existing database, repositories, and accounts remain in the same host-mounted directories.
Take a filesystem backup before any production cutover.

## Optional models and MCP

```bash
# Pull the selected model only when wanted; it can take substantial disk and time.
docker compose --profile models run --rm model-init

# Run mcp_agent_mail and the read-only gascity-mcp HTTP service.
docker compose --profile city --profile mcp up -d --build
```

### Ollama request metrics

The `city` profile routes its OpenAI-compatible Ollama traffic through a locally checked-out fork
of `allenday/ollama-metrics`, while the upstream `/v1/chat/completions` contribution is pending.
Clone it beside this repository (or set `OLLAMA_METRICS_DIR` to another absolute checkout):

```bash
git clone https://github.com/allenday/ollama-metrics.git ../ollama-metrics
docker compose --profile city up -d --build
```

The proxy is scraped by Prometheus and its upstream-provided Grafana dashboard is mounted read-only.
`INCLUDE_STREAM_USAGE=true` is enabled only for the proxy so streaming City requests provide their
final prompt/completion usage counters.

`gascity-mcp` comes from the sibling `cyberstorm-dev/gascity-mcp` checkout (`GASCITY_MCP_DIR`) and
offers the same read-only typed operations over `/mcp`. Its Docker endpoint is bound to localhost
only; it is not an unauthenticated remote supervisor-control API. The standalone `mcp-agent-mail`
service is the upstream Gas City-compatible inter-agent mail bridge, not a City administration MCP.

### Role-scoped Gitea MCP

The optional `gitea-mcp` profile uses the official Gitea MCP image, pinned by digest. It is not a
replacement for the `gitea-bridge` policy controller: MCP gives an agent Gitea tools, whereas the
bridge verifies a human actor and allowlisted decision label before changing City work.

Set separate, non-admin tokens in `.env`, then start the profile:

```bash
docker compose --profile gitea-mcp up -d
# or perform the distinct-token preflight first:
make gitea-mcp-up ENV_FILE=.env
```

For a fresh local Gitea, use the idempotent bootstrap instead. It creates three
restricted, non-admin service accounts, stores their scoped tokens only in the
ignored `.env`, starts the profile, and checks the live MCP tool surfaces:

```bash
make gitea-mcp-bootstrap ENV_FILE=.env
```

Three localhost-only endpoints are exposed, one for each permission class:

| Endpoint | Intended role | Exposed tools |
| --- | --- | --- |
| `http://127.0.0.1:8771/mcp` | Mayor | issue planning/status comments and issue labels |
| `http://127.0.0.1:8772/mcp` | Deacon, Boot, worker | read-only issue and label inspection |
| `http://127.0.0.1:8773/mcp` | Witness / QA | evidence comments and issue-level QA labels |

All three credentials must be distinct. A worker that needs write capability should receive a new,
separate permission class only after a policy decision: the official server's current coarse
pull-request write tool includes merge capability. Neither allowlist contains repository
administration, secrets, releases, destructive file operations, or merge operations. Gitea token
permissions should mirror that limitation; an MCP allowlist is defence in depth, not a substitute
for least-privilege tokens and repository membership. These endpoints bind only to localhost, so
host-process agents are separated by configuration rather than a hard OS boundary.
In particular, Gitea's `write:issue` token scope also covers some destructive raw-API issue
operations; agents only see the selected MCP tools, but a stolen PAT could otherwise call Gitea
directly. The future Woodpecker integration is an evidence producer: its immutable run and artifact
URLs will be reflected by the bridge into the managed status comment, but Woodpecker is not part of
this Compose file yet.

## Monitoring notes

- Gatus has role-specific checks via the tiny read-only `role-health` adapter. A suspended or
  missing role is reported as down instead of treated as healthy.
- `node-exporter` reports the Linux Docker VM on Docker Desktop for macOS, not macOS kernel metrics.
  On Linux it reports the mounted host root filesystem as expected.
- City role checks are expected to be down until the `city` profile is running.
- Set `CITY_API` and `CITY_API_HOST` only when monitoring a City reached through a proxy or from a
  separate network. Gas City validates Host headers; the default permitted header is
  `127.0.0.1:8372`.
