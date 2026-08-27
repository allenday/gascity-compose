# Buzz Mayor Chat Bridge Design

## Purpose

Provide one private Buzz conversation through which an allowlisted human can
chat with the Mayor. Buzz is a transport only: it has no Gitea, issue,
CI, City-control, or work-lifecycle semantics.

## Scope

The first slice is a self-hosted, Tailnet-only Buzz deployment and a separate
bridge between its one configured private channel and the Mayor's existing
Agent Mail conversation.

```text
human Buzz client <-> Tailnet Nginx <-> Buzz relay <-> buzz-mayor-bridge <-> Agent Mail <-> Mayor
```

The bridge preserves message content without interpreting URLs or commands. A
Gitea URL is ordinary text; Buzz must not read from or write to Gitea.

## Non-goals

- Gitea integration, issue creation, issue status, pull-request status, or CI
  notifications.
- City API access, run creation, bead closure, or any Gas City state mutation.
- Arbitrary rooms, direct-message routing, broadcasts, room creation, or
  external federation policy beyond the configured private channel.
- Replacing Agent Mail as Mayor's internal communication mechanism.
- WebSocket event consumption in the first slice.

## Deployment

Add a `buzz` Compose profile containing a pinned upstream Buzz relay and the
upstream durable services it requires (PostgreSQL, Redis, object storage, and
its Git storage). Persist every upstream state directory below `./state`.

Nginx remains the only Tailnet-facing listener. It publishes a configurable
`BUZZ_PORT` and proxies WebSocket and HTTP traffic to the relay. The relay has
no direct host port. Human clients use the canonical Tailnet
`BUZZ_PUBLIC_RELAY_URL`. The bridge transports requests to the private
`BUZZ_RELAY_URL` (normally `http://buzz-relay:3000`) but sends the canonical
public Host authority from `BUZZ_PUBLIC_RELAY_URL` on every relay request.
This preserves one Buzz community without relying on container-to-host
hairpin routing.

Add a separate `buzz-mayor-bridge` profile. Its service has a dedicated durable
ledger mount, no published port, no mounted City runtime, and no Gitea or City
environment variables. It depends only on the healthy Buzz relay and Agent
Mail. Agent Mail is enabled for this profile but is not exposed through Nginx.

All Buzz image/version references are immutable. The implementation resolves a
published upstream `ghcr.io/block/buzz:sha-<short-sha>` tag and records that
exact pin in Compose and acceptance evidence; it must not use `main` or a
floating tag.

## Identity and bootstrap

Buzz relay membership and private-channel membership are separate controls.
Bootstrap is a dedicated idempotent script, not part of the broad platform
bootstrap. It starts the relay, creates or verifies exactly one private Mayor
channel, verifies the configured human and bridge public keys as members, and
stores stable generated private material only in ignored configuration/state.

The bridge refuses to start unless all of these are configured and valid:

- canonical external and internal relay coordinates;
- a fixed private Mayor channel ID;
- an allowlist of human Buzz public keys;
- the bridge signing key and public key;
- a distinct Agent Mail bridge identity and its Mayor destination.

The Mayor's Buzz private key is not required in this slice: the bridge signs
its transport messages with its own narrowly scoped key and labels the content
as a Mayor relay. The bridge never impersonates a human or receives broad
Mayor/City credentials.

The bridge command requires both relay coordinates. It validates that the
public coordinate is an absolute HTTP(S) URL with no credentials, query, or
fragment, and extracts its host authority. The internal coordinate has the
same URL restrictions and is used only as the TCP/TLS destination. The Host
authority is not accepted from chat content, Agent Mail, or an inbound event.
At bootstrap/preflight, an authenticated internal `/query` request made with
the canonical public Host authority must succeed before the steady-state
bridge starts.

## Bridge protocol and durability

The bridge owns an independent durable ledger and outbox. Its logical records
map:

```text
Buzz event ID <-> Agent Mail message ID <-> Buzz channel ID/thread-root ID
```

Inbound polling uses Buzz's JSON CLI/API `messages get` against only the fixed
channel. It reads a bounded overlap window, treats Buzz event ID as the stable
logical identity, and records the intent before sending Agent Mail. The cursor
advances only after durable recording. On retry or restart, an already-recorded
event produces no second logical Mayor delivery.

Outbound polling reads only the bridge's Agent Mail identity. A valid structured
Mayor reply identifies the mapped inbound conversation. The bridge records the
complete signed raw-event outbox intent before its first publish and marks it
complete only after a durable successful result. Duplicate mail receipts or a
restart must not create duplicate Buzz replies.

The initial adapter uses bounded polling rather than raw Nostr WebSockets
because the supported Buzz CLI provides no durable subscribe cursor. The
persisted inbound cursor is the ordered pair `(created_at, event_id)`, not a
timestamp alone: a saturated page at one timestamp is resumed by event ID
without skipping or permanently stalling same-timestamp events.

The bridge may use the Buzz CLI for bounded reads, but it does not use the CLI
for outbound replies. It constructs one signed Nostr event with a stable
logical reply ID in its tags and posts that exact event through Buzz's pinned
raw event API. The durable outbox saves the complete signed event before its
first POST; a timeout or restart retries the identical event ID, rather than
creating another human-visible reply. This is a narrow provider adapter, not a
generic Nostr client or WebSocket consumer.

Inbound messages fail closed when the event is not in the configured channel,
the signer is not allowlisted, the content is malformed for the bridge envelope,
or the ledger cannot be persisted. Outbound messages fail closed when the Agent
Mail reply has no valid mapping or uses a wrong destination. Failures remain
visible and retryable from the durable outbox; they do not create tracker or
City side effects.

## Interface boundaries

The durable Go bridge belongs in `cyberstorm-dev/gascity-gitea`, alongside the
existing Agent Mail controller conventions, but in a new Buzz-specific package.
It must not reuse Gitea tracker envelope types or intake policy as a generic
chat model. A small generic Agent Mail transport may be extracted from existing
code only if it does not widen existing authority.

`allenday/gascity-compose` owns the deployment profile, upstream pinning,
Nginx exposure, bootstrap, configuration validation, and live fixture. The
public `gascity-mcp` remains read-only and out of scope.

## Acceptance

Automated repository validation covers Compose profile rendering, immutable
provider pins, Nginx route/port configuration, state mounts, service dependency
health checks, and negative authority assertions: the bridge has no Gitea,
City, host-port, or runtime-mount access.

Bridge unit tests prove channel/signer rejection, exactly-once logical inbound
delivery across duplicate polling, exactly-once outbound reply across duplicate
mail receipts, and cursor/outbox recovery after a restart.

The deployed service exposes `/healthz` and `/readyz`; its container image
includes only the minimal local probe required for Compose to check `/readyz`.
Readiness requires a successful bounded reconciliation and expires after the
configured stale age. Deployment tests assert this health check as well as the
absence of every disallowed authority.

Live acceptance uses an allowlisted human identity and the configured private
channel to demonstrate:

1. a human Buzz message reaches the Mayor through Agent Mail;
2. the Mayor's response returns in the same Buzz thread;
3. replaying the source event and restarting the bridge makes neither duplicate
   a logical message nor lose the mapping.

CI is necessary but not sufficient. The integration parent closes only after
the child graph is closed and it receives independent critique, current CI, and
this live acceptance evidence.
