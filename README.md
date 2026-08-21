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

The default City mount preserves its original absolute path inside the container. That is required
because an existing `city.toml` can contain absolute rig and provider paths. Containerizing the
supervisor does not automatically containerize host-specific providers (such as a wrapper that
calls a local binary); move those providers to service-network addresses before enabling workers.

Gas City publishes OTLP only when `GC_OTEL_METRICS_URL` and `GC_OTEL_LOGS_URL` are set. This stack
sets both for the containerized supervisor. The collector turns OTLP metrics into Prometheus
scrapes and forwards OTLP logs to Loki. The supplied Grafana dashboard is deliberately small;
Grafana Explore is available for raw logs.

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
```

Two localhost-only endpoints are exposed:

| Endpoint | Intended role | Exposed tools |
| --- | --- | --- |
| `http://127.0.0.1:8771/mcp` | reviewer / QA agent | issue reads and evidence comments, QA labels |
| `http://127.0.0.1:8772/mcp` | worker | issue and pull-request discussion/proposal tools |

The reviewer and worker credentials must be distinct. Neither tool allowlist contains repository,
secret, release, administration, destructive file, or merge operations. Gitea's token permissions
should mirror that limitation; an MCP allowlist is defence in depth, not a substitute for a
least-privilege token. The future Woodpecker integration is an evidence producer: its immutable run
and artifact URLs will be reflected by the bridge into the managed status comment, but Woodpecker is
not part of this Compose file yet.

## Monitoring notes

- Gatus has role-specific checks via the tiny read-only `role-health` adapter. A suspended or
  missing role is reported as down instead of treated as healthy.
- `node-exporter` reports the Linux Docker VM on Docker Desktop for macOS, not macOS kernel metrics.
  On Linux it reports the mounted host root filesystem as expected.
- City role checks are expected to be down until the `city` profile is running.
- Set `CITY_API` and `CITY_API_HOST` only when monitoring a City reached through a proxy or from a
  separate network. Gas City validates Host headers; the default permitted header is
  `127.0.0.1:8372`.
