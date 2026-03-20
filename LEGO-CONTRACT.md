# AgentEvents — LEGO-CONTRACT

*What this service IS, what it is NOT, what it guarantees, and how it connects.*

---

## What AgentEvents IS

- A **pub/sub event bus** for CivOS agents.
- Agents subscribe to event types. When events fire, subscribers are notified via webhook or polling.
- The **reactive nervous system** of the CivOS stack. Services publish events; agents consume them.
- A **Postgres-backed** event store with webhook delivery and polling fallback.

## What AgentEvents is NOT

- **Not a message queue.** It does not guarantee ordering or exactly-once delivery. It is at-least-once, and subscribers must be idempotent or deduplicate.
- **Not a streaming service.** No WebSocket connections, no SSE streams. Delivery is via HTTP webhook POST or HTTP GET polling.
- **Not Kafka, RabbitMQ, or any external message broker.** We own the event store in Postgres. No external pub/sub infrastructure.
- **Not a workflow engine.** It delivers events. What agents do with those events is their business. AgentEvents does not chain, sequence, or orchestrate.
- **Not an audit log.** Events are ephemeral notifications. They may be cleaned up after delivery confirmation. For permanent audit trails, use AgentLedger or AgentHUB history.

---

## Guarantees

| Guarantee | Detail |
|-----------|--------|
| **At-least-once delivery** | Every event is delivered to every active subscriber at least once. Network failures trigger retries. |
| **Deduplication by event ID** | Every event has a globally unique UUID. Subscribers can deduplicate on this ID. The service does not deduplicate on the subscriber's behalf. |
| **Webhook retry** | Failed webhook deliveries are retried with exponential backoff: 3 attempts (immediate, 30s, 5min). After 3 failures, marked as `failed`. |
| **Polling availability** | Agents that cannot receive webhooks can poll `GET /events` for undelivered events. Events are held for polling for a configurable retention period (default: 24h). |
| **Auth boundary** | All requests require AgentAUTH JWT. Agents can only manage their own subscriptions. Event publishing is restricted to authorized services. |
| **Event schema stability** | Standard event types (listed in README) have stable payload schemas. Breaking changes require a version bump. |

---

## Dependencies

| Dependency | Direction | Purpose |
|------------|-----------|---------|
| **AgentAUTH** | AgentEvents depends on AgentAUTH | JWT validation for all requests |
| **PostgreSQL** | Infrastructure dependency | Event and subscription storage |
| **AgentHUB** | HUB publishes events to AgentEvents | Entity, connection, group, thread events |
| **AgentLedger** | Ledger publishes events to AgentEvents | Transfer events |

AgentEvents does NOT depend on:
- AgentMemory (no memory operations involved)
- Any external message broker (Kafka, RabbitMQ, SQS, etc.)
- Any external pub/sub service (Google Pub/Sub, AWS SNS, etc.)

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Pattern |
|--------------|---------------|-----------------|
| Using Kafka/RabbitMQ/SQS as the event backbone | External infrastructure dependency, operational complexity, vendor lock-in | Postgres-backed event store. Simple. Owned. |
| Assuming exactly-once delivery | Network failures and retries make exactly-once impossible without subscriber cooperation | Design subscribers to be idempotent. Deduplicate by event ID. |
| Storing permanent audit data in events | Events have a retention window and may be cleaned up | Use AgentLedger for financial audit trails. Use AgentHUB for entity history. |
| Publishing high-frequency events without throttling | Floods subscribers, wastes compute, degrades the bus | Batch or debounce high-frequency sources. Rate-limit publishers. |
| Subscribing to all event types "just in case" | Wastes delivery bandwidth, increases noise, makes the agent's own processing slower | Subscribe only to event types the agent actually handles. |
| Building complex event chains inside AgentEvents | The bus delivers events, it does not orchestrate workflows | If you need chained logic, build it in the subscribing agent or a dedicated orchestrator. |
| Webhooks without idempotency keys | Retried deliveries cause duplicate processing | Every event has an ID. Use it as an idempotency key. |

---

## Schema (Planned)

```sql
CREATE TABLE events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type VARCHAR(100) NOT NULL,        -- e.g., 'entity.created'
  source VARCHAR(255) NOT NULL,      -- e.g., 'agenthub'
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id VARCHAR(255) NOT NULL,
  event_type VARCHAR(100) NOT NULL,  -- e.g., 'entity.created' or 'entity.*' for wildcard
  callback_url TEXT,                 -- NULL = polling mode
  active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE deliveries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id UUID REFERENCES events(id),
  subscription_id UUID REFERENCES subscriptions(id),
  status VARCHAR(20) DEFAULT 'pending',  -- 'pending', 'delivered', 'failed'
  attempts INTEGER DEFAULT 0,
  last_attempt_at TIMESTAMPTZ,
  delivered_at TIMESTAMPTZ
);

CREATE INDEX idx_events_type ON events(type);
CREATE INDEX idx_events_created ON events(created_at);
CREATE INDEX idx_subscriptions_agent ON subscriptions(agent_id);
CREATE INDEX idx_subscriptions_event_type ON subscriptions(event_type);
CREATE INDEX idx_deliveries_status ON deliveries(status) WHERE status = 'pending';
```

---

## Integration Points

```
AgentHUB ──publish──▶ AgentEvents ──webhook──▶ Subscribing Agent
AgentLedger ──publish──▶     │
                             │
                             ├──▶ PostgreSQL (event + subscription store)
                             ├──▶ AgentAUTH (JWT validation)
                             └──◀── Agent (polling: GET /events)
```

Services publish events. Agents subscribe and receive. AgentEvents is the pipe, not the logic.
