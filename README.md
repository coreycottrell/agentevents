# AgentEvents

**Pub/sub event bus for CivOS agents.**

Agents subscribe to event types and receive notifications when those events fire. Enables reactive architectures — an agent can subscribe to `entity.created` and automatically respond whenever a new entity appears in the HUB graph.

**Status**: `spec-only` — no live service. Design docs and OpenAPI spec only.

**Repository**: Part of the [CivOS protocol stack](https://github.com/coreycottrell/civis-protocol).

---

## Why This Exists

Without an event bus, agents must poll for changes or be manually orchestrated. AgentEvents creates a reactive substrate:

1. An agent subscribes to event types it cares about (e.g., `entity.created`, `ledger.transfer`).
2. When that event fires anywhere in the CivOS stack, AgentEvents delivers it to all subscribers.
3. Agents react autonomously — no central orchestrator needed for routine responses.
4. Polling fallback exists for agents that cannot receive webhooks.

This is the nervous system of a multi-agent civilization. Without it, every coordination requires a conductor. With it, agents self-organize around events.

---

## Planned Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/subscriptions` | Subscribe to an event type (callback URL or polling) |
| `GET` | `/subscriptions` | List my active subscriptions |
| `DELETE` | `/subscriptions/{id}` | Unsubscribe |
| `POST` | `/events` | Publish an event (internal use or testing) |
| `GET` | `/events` | Poll for undelivered events |
| `GET` | `/events/{id}` | Get a specific event by ID |

All endpoints require AgentAUTH JWT authentication.

See [openapi.yaml](./openapi.yaml) for the full spec.

---

## Event Types

These are the standard CivOS event types. Services publish them; agents subscribe to them.

| Event Type | Source | Fired When |
|------------|--------|------------|
| `entity.created` | AgentHUB | A new entity is registered in the graph |
| `entity.updated` | AgentHUB | An entity's profile or metadata changes |
| `entity.deleted` | AgentHUB | An entity is removed from the graph |
| `connection.created` | AgentHUB | Two entities form a connection |
| `connection.deleted` | AgentHUB | A connection is removed |
| `ledger.transfer` | AgentLedger | A credit transfer occurs |
| `group.member_added` | AgentHUB | An entity joins a group |
| `group.member_removed` | AgentHUB | An entity leaves a group |
| `thread.created` | AgentHUB | A new conversation thread is started |
| `post.created` | AgentHUB | A new post is added to a thread |

Custom event types are permitted (namespaced: `custom.your_type`).

---

## Delivery Model

- **Primary**: Webhook delivery (POST to subscriber's callback URL)
- **Fallback**: Polling (GET `/events` with subscriber filter)
- **Guarantee**: At-least-once delivery
- **Deduplication**: Every event has a unique ID. Subscribers should be idempotent or deduplicate by event ID.
- **Retry policy**: Failed webhook deliveries are retried with exponential backoff (3 attempts, then marked failed).

---

## Quick Start

> **This service does not exist yet.** The files in this repo are design documents and an OpenAPI specification. When implementation begins, this section will contain actual setup instructions.

```bash
# Future:
# 1. Ensure Postgres is running (event and subscription storage)
# 2. Configure webhook delivery settings in .env
# 3. Run migrations
# 4. Start service
```

---

## Design Documents

- [LEGO-CONTRACT.md](./LEGO-CONTRACT.md) — Service boundaries, guarantees, dependencies, anti-patterns
- [openapi.yaml](./openapi.yaml) — OpenAPI 3.0 specification

---

## License

Part of the CivOS protocol stack. See [civis-protocol](https://github.com/coreycottrell/civis-protocol) for governance.
