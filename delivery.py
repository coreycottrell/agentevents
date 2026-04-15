"""
AgentEvents v1.0 — Webhook + Poll Delivery Engine

- process_event(): called by listener on pg_notify. Finds matching subscriptions,
  creates event + delivery records, dispatches webhooks.
- deliver_webhook(): HTTP POST with retry + exponential backoff.
- poll_pending_events(): mark events as delivered after subscriber ACKs.
"""

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg
import httpx

logger = logging.getLogger(__name__)

# Retry config: 3 attempts, backoff 10s → 30s → 90s
RETRY_DELAYS = [10, 30, 90]


async def process_event(
    event_type: str,
    entity_id: str,
    room_id: str | None,
    group_id: str | None,
    created_by: str,
    created_at: str,
    title: str,
    body_preview: str,
    db_url: str,
):
    """
    Main entry point from listener.
    1. Insert event record
    2. Find all active, unmuted subscriptions matching this event
    3. Create delivery records
    4. Fire webhook deliveries async
    """
    conn = await asyncpg.connect(db_url, timeout=10)

    try:
        # ── 1. Insert event ──────────────────────────────────────────────────────
        event_row = await conn.fetchrow(
            """
            INSERT INTO agentevents.events (event_type, source, payload)
            VALUES ($1, 'agenthub', $2)
            RETURNING id, created_at
            """,
            event_type,
            {
                "entity_id": entity_id,
                "room_id": room_id,
                "group_id": group_id,
                "created_by": created_by,
                "title": title,
                "body_preview": body_preview,
            },
        )
        event_id = event_row["id"]
        logger.info(f"Event stored: {event_id} ({event_type})")

        # ── 2. Find matching subscriptions ─────────────────────────────────────
        # Wildcard matching: if sub event_type is "entity.*", match "entity.created"
        subs = await conn.fetch(
            """
            SELECT id, civ_id, event_type, scope_type, scope_id,
                   delivery_method, webhook_url, agentmail_address
            FROM agentevents.subscriptions
            WHERE active = TRUE
              AND (muted_until IS NULL OR muted_until < NOW())
              AND (
                  event_type = $1
                  OR event_type = LEFT($1, POSITION('.' IN $1)-1) || '.*'
                  OR event_type = '*'
              )
            """,
            event_type,
        )
        logger.info(f"Found {len(subs)} matching subscriptions for {event_type}")

        if not subs:
            return

        # ── 3. Create delivery records ─────────────────────────────────────────
        delivery_ids = []
        for sub in subs:
            # Don't notify author of their own content
            if sub["civ_id"] == created_by:
                continue

            # Scope check: if scoped to a group, verify the EVENT is in that group
            # (Subscriber membership was already verified at subscription time)
            if sub["scope_type"] == "group" and sub["scope_id"]:
                # Check if this event's group matches the subscription scope
                if group_id and str(sub["scope_id"]) != str(group_id):
                    logger.debug(f"Skipping sub {sub['id']} — event group {group_id} != scope {sub['scope_id']}")
                    continue
            elif sub["scope_type"] == "room" and sub["scope_id"]:
                if room_id and str(sub["scope_id"]) != str(room_id):
                    logger.debug(f"Skipping sub {sub['id']} — event room {room_id} != scope {sub['scope_id']}")
                    continue

            delivery_id = await conn.fetchval(
                """
                INSERT INTO agentevents.deliveries (event_id, subscription_id, status)
                VALUES ($1, $2, 'pending')
                RETURNING id
                """,
                event_id,
                sub["id"],
            )
            delivery_ids.append((delivery_id, sub))

        logger.info(f"Created {len(delivery_ids)} delivery records")

        # ── 4. Fire webhook deliveries async ────────────────────────────────────
        for delivery_id, sub in delivery_ids:
            if sub["delivery_method"] == "webhook" and sub["webhook_url"]:
                asyncio.create_task(
                    deliver_webhook(delivery_id, sub, event_row, db_url)
                )
            # agentmail / poll — handled by subscriber polling

    finally:
        await conn.close()


async def deliver_webhook(
    delivery_id: UUID,
    subscription: dict,
    event: asyncpg.Record,
    db_url: str,
):
    """
    POST event to subscriber's webhook_url with exponential backoff retry.
    Marks delivery record as delivered/failed.
    """
    webhook_url = subscription["webhook_url"]
    headers = {
        "Content-Type": "application/json",
        "X-AgentEvents-Event-Type": event["event_type"],
        "X-AgentEvents-Event-Id": str(event["id"]),
    }

    payload = {
        "event_type": event["event_type"],
        "event_id": str(event["id"]),
        "source_civ": subscription["civ_id"],
        "scope": {
            "group_id": subscription.get("scope_id"),
            "room_id": None,
        },
        "payload": event["payload"],
        "timestamp": event["created_at"].isoformat(),
        "resource_url": f"http://87.99.131.49:8900/api/v2/events/{event['id']}",
    }

    for attempt_idx, delay in enumerate(RETRY_DELAYS):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(webhook_url, json=payload, headers=headers)
                if resp.status_code < 400:
                    await mark_delivered(delivery_id, db_url)
                    logger.info(f"Webhook delivered: {delivery_id} → {webhook_url}")
                    return
                else:
                    logger.warning(
                        f"Webhook HTTP {resp.status_code} attempt {attempt_idx+1}: {webhook_url}"
                    )
        except httpx.RequestError as e:
            logger.warning(f"Webhook error attempt {attempt_idx+1}: {e}")

        if delay == RETRY_DELAYS[-1]:
            break  # no more retries

        await asyncio.sleep(delay)

    # All retries exhausted
    await mark_failed(delivery_id, db_url)
    logger.error(f"Webhook FAILED after {len(RETRY_DELAYS)} attempts: {delivery_id}")


async def mark_delivered(delivery_id: UUID, db_url: str):
    conn = await asyncpg.connect(db_url, timeout=10)
    try:
        await conn.execute(
            """
            UPDATE agentevents.deliveries
            SET status = 'delivered',
                delivered_at = NOW(),
                attempts = attempts + 1,
                last_attempt_at = NOW()
            WHERE id = $1
            """,
            delivery_id,
        )
    finally:
        await conn.close()


async def mark_failed(delivery_id: UUID, db_url: str, error: str = None):
    conn = await asyncpg.connect(db_url, timeout=10)
    try:
        await conn.execute(
            """
            UPDATE agentevents.deliveries
            SET status = 'failed',
                attempts = attempts + 1,
                last_attempt_at = NOW(),
                error_message = $2
            WHERE id = $1
            """,
            delivery_id,
            error or "All retry attempts exhausted",
        )
    finally:
        await conn.close()


# ─── Polling API helpers ────────────────────────────────────────────────────────

async def get_pending_events(civ_id: str, since: datetime | None, limit: int, db_url: str):
    """
    Returns undelivered events for a civ's subscriptions (poll delivery path).
    Used by GET /events/pending.
    """
    conn = await asyncpg.connect(db_url, timeout=10)
    try:
        query = """
            SELECT DISTINCT e.id, e.event_type, e.source, e.payload, e.created_at,
                   d.id as delivery_id
            FROM agentevents.events e
            JOIN agentevents.deliveries d ON d.event_id = e.id
            JOIN agentevents.subscriptions s ON s.id = d.subscription_id
            WHERE s.civ_id = $1
              AND s.active = TRUE
              AND s.delivery_method = 'poll'
              AND d.status = 'pending'
              AND (s.muted_until IS NULL OR s.muted_until < NOW())
            """
        params = [civ_id]

        if since:
            query += " AND e.created_at > $2"
            params.append(since)

        query += " ORDER BY e.created_at ASC LIMIT $" + str(len(params))
        params.append(limit)

        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def ack_events(delivery_ids: list[UUID], db_url: str):
    """Mark deliveries as delivered after subscriber ACKs."""
    if not delivery_ids:
        return
    conn = await asyncpg.connect(db_url, timeout=10)
    try:
        await conn.execute(
            """
            UPDATE agentevents.deliveries
            SET status = 'delivered', delivered_at = NOW()
            WHERE id = ANY($1)
            """,
            delivery_ids,
        )
    finally:
        await conn.close()
