"""
AgentEvents v1.0 — PostgreSQL LISTEN/NOTIFY Listener

Listens on the 'agentevents' pg_notify channel. On each notification:
1. Parse the event payload
2. Find matching (active, unmuted) subscriptions
3. Create event + delivery records
4. Dispatch webhook deliveries asynchronously
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

HUB_DB_URL = os.environ.get(
    "HUB_DB_URL",
    "postgresql://hubuser:aiciv_hub_secure_2026@87.99.131.49:5432/hubdb"
)


async def handle_notification(connection: asyncpg.Connection, pid: int, channel: str, payload: str):
    """Called by asyncpg when pg_notify fires."""
    try:
        data = json.loads(payload)
        event_type = data.get("event_type")
        entity_id = data.get("entity_id")
        room_id = data.get("room_id")
        group_id = data.get("group_id")
        created_by = data.get("created_by")
        created_at = data.get("created_at")
        title = data.get("title", "")
        body_preview = data.get("body_preview", "")

        logger.info(f"pg_notify received: {event_type} | entity={entity_id}")

        # Route to subscription matcher + dispatcher (imported from delivery.py)
        from delivery import process_event

        await process_event(
            event_type=event_type,
            entity_id=entity_id,
            room_id=room_id,
            group_id=group_id,
            created_by=created_by,
            created_at=created_at,
            title=title,
            body_preview=body_preview,
            db_url=HUB_DB_URL
        )

    except Exception:
        logger.exception(f"handler failed for payload: {payload[:200]}")


async def listenforever(db_url: str):
    """Connect to Postgres and listen indefinitely."""
    logger.info("Connecting to Hub DB for LISTEN/NOTIFY...")
    while True:
        try:
            conn = await asyncpg.connect(db_url, timeout=30)
            logger.info("LISTEN connected. Waiting for events...")
            await conn.add_listener("agentevents", handle_notification)
            # Keep alive — listen runs forever
            await asyncio.Event().wait()
        except asyncpg.PostgresConnectionError:
            logger.warning("LISTEN connection lost. Reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception:
            logger.exception("LISTEN fatal error. Reconnecting in 5s...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(listenforever(HUB_DB_URL))
