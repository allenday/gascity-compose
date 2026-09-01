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

## GitHub documentation-impact smoke adapter

Enable the complete GitHub profile with the initial App credentials in ignored
`.env` or an already-imported protected intake state. Before starting it, set
the absolute pack checkout and City review-rig settings:

```dotenv
GITHUB_PACK_DIR=/absolute/path/to/gascity-packs
GC_CITY_DOCS_REVIEW_RIG_DIR=/absolute/path/to/my-project
GC_CITY_DOCS_REVIEW_TARGET=my-project/github-docs-impact.docs-impact-reviewer
```

`GC_CITY_DOCS_REVIEW_TARGET` is the qualified `<rig>/<agent>` name. The
`city-bootstrap` target registers `GC_CITY_DOCS_REVIEW_RIG_DIR` under its path
basename, so the example above uses `my-project` for both the directory name
and target rig. Run the bootstrap before the preflight:

```bash
make city-bootstrap ENV_FILE=.env
make github-docs-impact-preflight ENV_FILE=.env
```

The preflight verifies the Compose route, pack entrypoints, and either the
initial `.env` credentials or the imported credentials in
`state/github-intake/data/config.json`. This profile starts the City, signed
webhook service, and local runtime adapter together:

```bash
docker compose --profile github-docs-impact up -d --build
```

Because this profile now includes `city`, stop any host or other Compose City
supervisor for the same `CITY_DIR` before running it. The profile does not
create a second supervisor or weaken the single-supervisor rule.

The signed webhook reads every GitHub PR-files page and creates one exact,
SHA-bound assignment. The runtime first saves its durable lifecycle record,
then creates and slings an immutable City review task. The reviewer has no
GitHub credential and returns only a JSON decision. The trusted runtime reads
that task's final session transcript, binds and validates it against the saved
assignment, then uses the App credentials to publish the compact Check Run or
App-owned follow-up PR. It periodically reconciles interrupted runs. No
proposal diff or deployment-admin page is linked from GitHub.

## Start the City safely

The `city` service is profile-gated because two supervisors must never reconcile the same mounted
City. Stop the existing host supervisor first, then start City alone in Compose:

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

### Opinionated Superpowers builds

Fresh and existing mounted cities automatically receive the pinned
[`superpowers`](https://github.com/gastownhall/gascity-packs/tree/main/superpowers)
pack unless `SUPERPOWERS_PACK_ENABLED=false` is set. The recommended build
formula is `superpowers-build`; use `build-basic` when the extra specification,
TDD, and review stages are not worth their additional inference cost.

`superpowers-build` extends Gas City's `build-base`; it does not extend
Gastown. Gastown supplies the continuously operating Mayor, Deacon, Witness,
and related city roles, while Superpowers supplies the build workflow and its
rig-scoped implementation and review roles.

The pack vendors its Superpowers skills and Gas City materializes the relevant
material for each workflow lane. Do not separately install a Codex
Superpowers skill: Codex workers and OpenAI-compatible models such as Gemma
receive the same versioned instructions from the pack. Provider-native
subagent calls are translated into Gas City graph lanes, beads, and convoys.

The source and immutable pin are explicit upgrade controls:

| Variable | Default |
| --- | --- |
| `SUPERPOWERS_PACK_ENABLED` | `true` |
| `SUPERPOWERS_PACK_SOURCE` | `https://github.com/gastownhall/gascity-packs/tree/main/superpowers` |
| `SUPERPOWERS_PACK_VERSION` | `sha:3b3b89f2011e06d84459aa7bea1552382f13930a` |

For example, after creating a target bead in a rig:

```bash
gc sling gc.run-operator <bead-id> --on superpowers-build \
  --var artifact_root=plans/<change>/build \
  --var drain_policy=separate
```

This import is additive: `build-basic` remains available, and an existing
`imports.superpowers` binding is never overwritten by container startup.

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

# Run authenticated mcp_agent_mail and the read-only gascity-mcp HTTP service.
# The City mail bridge bootstrap below creates the required Mail bearer.
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

`gascity-mcp` is built from an immutable `cyberstorm-dev/gascity-mcp` Git context and offers the
same read-only typed operations over `/mcp`. Set `GASCITY_MCP_DIR` to a local checkout only when
developing the adapter. Its surface includes bounded health, status, agent, rig, run, and run-step
reads; run graphs, event feeds, bead metadata, and mutations remain deliberately excluded from the
public MCP/CLI registry. The library does contain a narrow, non-public `close_bead` client method
for a future capability-token-gated delivery bridge after policy authorizes an exact blocked step;
it is not a general City write surface. The direct Docker endpoint is bound to localhost and Nginx
proxies it only onto the configured Tailnet address. The standalone `mcp-agent-mail` service is the
upstream Gas City-compatible inter-agent mail bridge, not a City administration MCP.

The companion `cyberstorm-dev/gascity-gitea` repository provides the tested
`DeliveryBinding` reconciliation library and read-only `DecisionPolicy` evidence verification.
The latter requires a configured approval label and verifies that its newest matching timeline
event came from an authorized Gitea actor. Its capability-scoped `ResolveApprovedGate` boundary
can invoke an injected resolver only for an exact City/run/step-bead identity after acceptance
checks and tracker revalidation, using a canonical idempotency key. That explicit step-bead target
matches `gascity-mcp`'s private `close_bead` capability without broadening its public MCP surface.
The pinned `gitea-bridge` Compose profile builds from `GITEA_BRIDGE_DIR` and runs its new
single-binding status daemon. Because the source repository is private, clone it beside this
repository (or set an absolute checkout path); the image build verifies that its HEAD matches
`GASCITY_GITEA_REF`. The build follows the repository's native packaging: a static Go binary in a
non-root distroless runtime image. Configure a dedicated restricted `GITEA_BRIDGE_TOKEN`, one canonical
`GITEA_BRIDGE_ISSUE_URL`, and one `GITEA_BRIDGE_RUN_ID`, then start it alongside the City:

```bash
docker compose --profile city --profile gitea-bridge up -d --build gitea-bridge
```

This daemon reads the selected run and steps and updates only its managed Gitea status comment.
It deliberately does not wire the library's gate resolver or MCP's private `close_bead` method;
it has no bead-close, issue-close, parent-run finalization, or general City mutation authority.
Its configuration also fails closed unless `GITEA_URL` exactly matches the instance in the issue
URL; City endpoints reject embedded credentials, non-HTTP schemes, query strings, and fragments.

The pinned library also contains replay-safe `StepResolver` and `OutcomeStore` primitives. They
re-read one exact blocked step, send a scoped idempotency key, verify City reports completion, and
only then atomically journal the outcome. They remain deliberately uninstantiated by this status
profile: tracker authorization and CI/acceptance evidence must be wired by a separate resolution
controller before any automated step closure is enabled.

That future controller must bind every acceptance check to one immutable source subject: the
canonical repository URL plus a full Git object ID or `sha256:` digest. Mutable branches/tags and
evidence for any other revision are rejected, so a passing build cannot authorize closure of work
for a different source state.

The library also offers evidence-gated issue finalization, but this profile does not instantiate
it. Finalization requires the bound run and every step to be completed, fresh acceptance for the
binding's immutable source, and a current authorized tracker decision. It cannot infer completion
from a prior gate resolution, and it remains separate from the deployed status projection.

### Private Buzz relay

The optional `buzz` profile runs one self-hosted Buzz relay behind the existing
Tailnet Nginx gateway. The relay has no published host port; Nginx alone binds
`${TAILNET_IP}:${BUZZ_PORT}` and forwards HTTP and WebSocket traffic to the
private `buzz-relay` service. Set `BUZZ_RELAY_URL` to that one canonical
Tailnet `ws://` or TLS-terminated `wss://` URL for human clients. The internal
Docker hostname is not a client coordinate.

The relay image is pinned to upstream source tag `sha-53771c8`
(`53771c8f5439f9c5c26876f0229bfcfe5da9b170`) and its OCI manifest digest in
`compose.yaml`; it does not use `main`, `latest`, or another floating tag.
The profile persists relay Git data, PostgreSQL, Redis, and MinIO beneath
`./state/buzz-*`. Before starting, copy `.env.example` to ignored `.env` and
replace every `CHANGE_ME` Buzz secret with a stable generated value:

```bash
make buzz-up ENV_FILE=.env
```

This foundation provides only the private relay and its durable upstream
dependencies. It does not deploy a Mayor bridge, bootstrap channel membership,
connect to Agent Mail, or receive Gitea or City credentials.

### Private Buzz Mayor bridge

`buzz-mayor-bridge` is a separate, non-root Compose profile for one configured
private Buzz channel. It has no host port, Gitea token, City endpoint, City
runtime mount, Codex configuration, or Mayor private key. Its only persistent
mount is `state/buzz-mayor-bridge/ledger.json`; the core bridge owns the
replay-safe mapping between Buzz event IDs and Agent Mail messages.

The service deliberately uses two different relay coordinates. Human clients
keep using the canonical Tailnet `BUZZ_RELAY_URL` (`ws://` or `wss://`). The
bridge receives the same canonical authority in HTTP(S) form as
`BUZZ_PUBLIC_RELAY_URL`, which the raw core requires for canonical Host
preservation. Its transport itself is fixed to the private
`BUZZ_MAYOR_BRIDGE_RELAY_URL=http://buzz-relay:3000`; it never publishes that
internal name. Its Docker health check calls `/readyz`, which becomes healthy
only after the core has completed its initial authenticated relay query.

`GASCITY_GITEA_REF` is pinned in `.env.example` to
`827d768468a76787655ef46be24679301dc7e217`, the full immutable merged
`gascity-gitea` revision that includes `cmd/buzz-mayor-bridge` and
dual-coordinate Host handling. The preflight refuses a dirty checkout, a
non-full SHA, or a checkout at a different revision. Do not use a branch or
floating tag.

The dedicated bootstrap uses upstream `buzz-admin` only to generate the
bridge's persistent Nostr keypair and reconcile relay membership. It uses the
operator-installed upstream `buzz` CLI—configured as `BUZZ_CLI`—with the
separate `BUZZ_CHANNEL_ADMIN_PRIVATE_KEY` to create or verify the fixed private
channel and its human and bridge memberships. It also creates only the distinct
Agent Mail bridge identity and its contact grants with the existing Mayor
identity. It never creates Gitea users, tokens, labels, webhooks, repositories,
or City work.

After copying `.env.example` to ignored `.env`, set the listed Buzz keys,
`BUZZ_ALLOWED_HUMAN_PUBKEYS`, and the existing Mayor Agent Mail registration
token, then run:

```bash
make buzz-mayor-bridge-bootstrap ENV_FILE=.env
make buzz-mayor-bridge-up ENV_FILE=.env
```

The second command runs strict configuration/source preflight, builds the
pinned command, and executes its bounded `--preflight` authenticated private
query before starting the relay, private Agent Mail service, and bridge with
`--wait`. It additionally checks `/readyz`, proving the steady-state bridge
has reconciled successfully after the canonical-Host preflight. Live
bidirectional acceptance and replay evidence are intentionally provided by the
separate Buzz Mayor fixture, not by this deployment target.

### Tracker-to-City mail intake

The `gitea-mail-bridge` profile is the independent ingress companion to the
status-only `gitea-bridge`. It accepts signed Gitea issue webhooks, repairs missed
deliveries with bounded read-only reconciliation, and writes normalized events to
Mayor's authenticated Agent Mail inbox. It has no City API endpoint, run mutation
credential, public port, issue writer, or issue-close authority. Its replay ledger
is isolated at `state/gitea-mail-bridge/ledger.json`.

Configure `INTAKE_MANIFEST_PATH` with a tracked manifest such as
`config/gitea-intake.toml`. The `gitea-intake-doctor` preflight derives
`INTAKE_REPOSITORY_SCOPES`, `INTAKE_CITY_IDENTITIES`, and
`INTAKE_MINIMUM_REPOSITORY_ROLE` from that manifest, so Compose no longer treats
an operator-maintained human allow-list as authority. The fixed Gitea role
accounts are `gascity-mcp-mayor`, `gascity-mail-bridge`, and
`gascity-mail-launcher`; bootstrap creates or verifies them as restricted,
non-admin users. The controller accepts Mayor assignment and approval only from
a current non-City Gitea collaborator with `triage` or stronger access. The
issue repository is the default plan target, while an approved cross-repository
plan can target only another configured scope.

Bootstrap is idempotent. It creates the restricted read-only bridge account,
distinct bridge/Mayor/launcher Mail registrations, bidirectional contact grants,
the two lifecycle labels, and one signed webhook for each scoped repository.
Secrets are generated only into the ignored mode-0600 environment file:

```bash
make gitea-intake-doctor ENV_FILE=.env
make gitea-mail-bridge-bootstrap ENV_FILE=.env
make gitea-mail-bridge-up ENV_FILE=.env
```

Removing a repository from the manifest stops new intake after redeploy because
bootstrap reconciles only the current manifest scopes. That removal is
non-destructive: it does not delete prior labels, webhooks, users, or ledger history,
so old evidence remains intact while new intake ceases for the removed repository.

Both services remain on the private Compose network. Agent Mail rejects
unauthenticated requests and the bridge calls its pinned `/mcp` endpoint with a
server bearer plus its own registration token. The Gitea webhook allow-list names
only `gitea-mail-bridge` in addition to the existing loopback and Woodpecker hosts.

Mayor's Agent Mail and restricted Gitea MCP bindings live in
`codex/runpod.config.toml.template`, the profile assigned only through the
`codex-mayor-runpod` provider. Copy the current `config/city-cost-safe.toml` into
the mounted City's `.gc/compose/` directory (as described above) before rebuilding
the City service. Bootstrap writes Mayor's bearer, registration token, project
slug, and identity to `state/city-mail-secrets/mayor.env`; the Mayor-only provider
wrapper starts a loopback-only MCP proxy that injects and constrains those values
server-side, then starts Codex without either credential in its environment or
Codex-generated tool arguments. The proxy exposes only Mayor's
inbox/read/ack/send/reply operations
and rejects another project or identity. Those values therefore do not enter the
City supervisor, tmux server, Codex process, or dynamic-worker environment. This
is a role/configuration boundary inside one Unix-UID container, not a separate OS
security domain. The shared Codex profile intentionally has no Agent Mail or
issue-writing Gitea server.

Agent Mail writes non-secret notification metadata to its signals directory. The
City entrypoint watches only Mayor's exact project/identity signal and issues one
hard-time-bounded `gc session nudge --delivery=wait-idle mayor` per changed signal.
That queued nudge creates the managed wake
for the on-demand Mayor and explicitly directs it to fetch authenticated mail,
follow the installed Superpowers/IDD planning process, and respond visibly on the
linked issue before waiting on internal ceremony. The tracker bridge itself still
has no City credential or wake endpoint.

Authority remains intentionally split:

| Component | May do | Must not do |
| --- | --- | --- |
| `gitea-mail-bridge` | Read exact scoped tracker state; send authenticated Mayor/launcher mail | Call City, write/close issues, create runs |
| City mail watcher | Read Mayor's non-secret signal; nudge the exact `mayor` target | Read tracker/Mail credentials, create or bind runs |
| Mayor | Read its Mail inbox; plan and comment/label through restricted Gitea MCP | Use bridge or launcher identity; bypass human approval |
| Launcher identity | Return an exact immutable run binding for one authorization | Plan publicly or approve its own work |

Agent Mail requires an absolute path to create a project, while the pinned bridge
requires the returned slash-free project slug. Configure
`MCP_AGENT_MAIL_PROJECT_PATH`; bootstrap stores the canonical result in
`MCP_AGENT_MAIL_PROJECT_KEY`. Do not hand-edit the derived key.

The disposable smoke fixture uses a deliberately reusable configured repository,
accounts, labels, and webhook. Its only per-run resource is a uniquely titled issue,
which it closes after proving creation/assignment ingress, logical-event replay,
authenticated Mayor plan plus fresh human approval, external reply routing, City
reply suppression, failed-send restart recovery, and binding persistence before
acknowledgement:

```bash
make gitea-mail-bridge-smoke ENV_FILE=.env
```

The fixture deliberately does not start a City run. Its launcher identity returns
an immutable synthetic run binding through private Mail, which exercises the
bridge's record-before-ack boundary without granting the bridge launch authority.

For a repeatable operator demo of the real private launcher handoff, use:

```bash
make gitea-mail-acceptance-demo ENV_FILE=.env
```

This target reruns the idempotent bootstrap and then creates one fresh disposable issue
for the real launcher fixture. It does not delete, edit, or reuse failed
fixture state; preserve that state as acceptance evidence. A successful demo
proves the real launcher binding only: the real Mayor/formula trace remains a separate Gate D requirement.

Bootstrap the dedicated restricted service-account token and `.env` placeholders with:

```bash
make gitea-bridge-bootstrap ENV_FILE=.env
```

`make gitea-bridge-up ENV_FILE=.env` additionally refuses to start until both the issue URL and
run ID are non-empty.

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
directly. The optional Woodpecker integration is an evidence producer: its immutable run and
artifact URLs can later be reflected by the bridge into the managed status comment; it does not
participate in gate resolution.

The pinned `gascity-gitea` library also offers a `woodpecker.AcceptanceVerifier` for a future
resolution controller. It uses a dedicated Woodpecker PAT and one explicitly pinned pipeline number,
then emits evidence only when that finished successful pipeline and its repository URL exactly match
the delivery binding's immutable source revision. It is not instantiated by `gitea-bridge`.

### Optional Woodpecker fixture

The `woodpecker` profile is a small, local-only CI deployment backed by the
same Gitea service. It is intentionally separate from the delivery bridge:
Woodpecker produces build evidence only and receives neither City nor Gitea
bridge credentials.

Create an agent secret, bootstrap the private fixture repository and its
least-privilege Gitea OAuth application, then start the profile:

```bash
openssl rand -hex 32  # copy the result to WOODPECKER_AGENT_SECRET in .env
make woodpecker-up ENV_FILE=.env
make woodpecker-smoke ENV_FILE=.env
```

Bootstrap creates the regular `woodpecker-fixture` account and private
`gascity-compose-fixture` repository. That repository contains a single
Alpine `.woodpecker.yml` which checks out its `README.md`, writes an artifact
whose package version is the immutable commit SHA, and publishes it to Gitea's
generic package registry. Its generated Gitea password is stored as
`GITEA_WOODPECKER_PASSWORD` in the ignored `.env`; use it to sign into
`http://127.0.0.1:8000` through Gitea as the fixture user, activate that
repository, and push a fixture change. Then confirm the registered Gitea
webhook, successful Woodpecker run for the branch head, and retained artifact:

```bash
make woodpecker-acceptance ENV_FILE=.env
```

The server binds only to loopback, accepts only the fixture owner by default,
and runs one workflow at a time. The Docker agent necessarily owns the Docker
socket to create step containers; no socket or privileged-plugin permission is
given to pipeline steps. Keep the fixture repository private and review any
change that widens `WOODPECKER_REPO_OWNERS` or adds privileged plugins. The
workflow network is deliberately separate and uses the `gitea` service name
for API and clone traffic; the browser OAuth flow instead uses
`WOODPECKER_GITEA_BROWSER_URL` (loopback Gitea by default). Gitea sends its
webhooks to Woodpecker's internal service address, which is intentionally
separate from the browser URL. Registration stays closed; only the configured
fixture admin is admitted. For an external deployment, set both
`WOODPECKER_HOST` and `WOODPECKER_GITEA_BROWSER_URL` to their stable HTTPS
browser URLs before bootstrapping the OAuth client, while keeping
`WOODPECKER_GITEA_URL` and the internal webhook route on Docker-only service
addresses.

## Monitoring notes

- Gatus has role-specific checks via the tiny read-only `role-health` adapter. A suspended or
  missing role is reported as down instead of treated as healthy.
- `node-exporter` reports the Linux Docker VM on Docker Desktop for macOS, not macOS kernel metrics.
  On Linux it reports the mounted host root filesystem as expected.
- City role checks are expected to be down until the `city` profile is running.
- Set `CITY_API` and `CITY_API_HOST` only when monitoring a City reached through a proxy or from a
  separate network. Gas City validates Host headers; the default permitted header is
  `127.0.0.1:8372`.
