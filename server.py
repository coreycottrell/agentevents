"""
AgentEvents v1.0 — FastAPI Server

Port 8400. AgentAUTH JWT verification. All endpoints require Bearer JWT.
"""

import logging
import os
from datetime import datetime, timezone
from uuid import UUID

import asyncpg
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key

from models import (
    SubscriptionCreate, SubscriptionResponse, SubscriptionList,
    EventCreate, EventResponse, EventList, PendingEventsResponse,
    AckRequest, MuteRequest, ErrorResponse,
    DeliveryMethod, ScopeType, EventStatus,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="AgentEvents API", version="1.0.0")
security = HTTPBearer()

# ─── Config ────────────────────────────────────────────────────────────────────

AGENTAUTH_URL = os.environ.get("AGENTAUTH_URL", "https://agentauth.ai-civ.com")
HUB_DB_URL = os.environ.get(
    "HUB_DB_URL",
    "postgresql://hubuser:aiciv_hub_secure_2026@87.99.131.49:5432/hubdb"
)
JWKS_URL = f"{AGENTAUTH_URL}/.well-known/jwks.json"

# ─── Auth ────────────────────────────────────────────────────────────────────

import base64
import time

_jwks_cache: dict = {}
_jwks_fetched_at: float = 0.0
_JWKS_TTL: float = 300.0


def _b64url_to_bytes(value: str) -> bytes:
    padding = 4 - len(value) % 4
    if padding != 4:
        value += "=" * padding
    return base64.urlsafe_b64decode(value)


async def _fetch_jwks() -> dict:
    """Fetch JWKS from AgentAUTH, return {kid: Ed25519PublicKey}."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(JWKS_URL)
        resp.raise_for_status()
        data = resp.json()

    keys = {}
    for jwk in data.get("keys", []):
        if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
            continue
        kid = jwk.get("kid", "default")
        try:
            x_bytes = _b64url_to_bytes(jwk["x"])
            keys[kid] = Ed25519PublicKey.from_public_bytes(x_bytes)
        except Exception as e:
            logger.warning(f"Skipping malformed JWK kid={kid}: {e}")
    return keys


async def _get_jwks() -> dict:
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()
    if not _jwks_cache or (now - _jwks_fetched_at) > _JWKS_TTL:
        try:
            _jwks_cache = await _fetch_jwks()
            _jwks_fetched_at = now
            logger.info(f"JWKS refreshed: {len(_jwks_cache)} key(s)")
        except Exception as e:
            logger.warning(f"JWKS fetch failed: {e} — using cached")
    return _jwks_cache


async def verify_jwt(token: str) -> dict:
    """Verify AgentAUTH Ed25519 JWT. Returns claims dict."""
    jwks = await _get_jwks()
    if not jwks:
        raise HTTPException(status_code=401, detail="No JWKS keys available")

    # Try each key
    for kid, pub_key in jwks.items():
        try:
            claims = jwt.decode(
                token, pub_key,
                algorithms=["EdDSA"],
                options={"verify_aud": False},
            )
            return claims
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="JWT expired")
        except jwt.InvalidTokenError:
            continue

    raise HTTPException(status_code=401, detail="JWT verification failed — no matching key")


async def get_civ_id(creds: HTTPAuthorizationCredentials = Depends(security)) -> str:
    claims = await verify_jwt(creds.credentials)
    civ_id = claims.get("civ_id") or claims.get("sub")
    if not civ_id:
        raise HTTPException(status_code=401, detail="No civ_id in JWT")
    return civ_id


# ─── DB helper ────────────────────────────────────────────────────────────────

async def get_db():
    conn = await asyncpg.connect(HUB_DB_URL, timeout=10)
    try:
        yield conn
    finally:
        await conn.close()


# ─── Subscription Endpoints ──────────────────────────────────────────────────

@app.post("/subscriptions", response_model=SubscriptionResponse, status_code=201)
async def create_subscription(
    sub: SubscriptionCreate,
    civ_id: str = Depends(get_civ_id),
    db: asyncpg.Connection = Depends(get_db),
):
    """Subscribe to an event type with optional scope (group/room/thread)."""

    # Validate webhook URL
    if sub.delivery_method == DeliveryMethod.WEBHOOK and not sub.webhook_url:
        raise HTTPException(status_code=400, detail="webhook_url required for webhook delivery")

    # Scope membership check
    if sub.scope_type in (ScopeType.GROUP, ScopeType.ROOM, ScopeType.THREAD):
        if not sub.scope_id:
            raise HTTPException(status_code=400, detail="scope_id required for scoped subscription")

        is_member = await db.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM public.group_members gm
                JOIN public.entities e ON e.id = gm.entity_id
                WHERE gm.group_id = $1 AND e.civ_id = $2
            )
            """,
            sub.scope_id,
            civ_id,
        )
        if not is_member:
            raise HTTPException(
                status_code=403,
                detail="Cannot subscribe to groups you are not a member of"
            )

    row = await db.fetchrow(
        """
        INSERT INTO agentevents.subscriptions
            (civ_id, event_type, scope_type, scope_id, delivery_method, webhook_url, agentmail_address)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (civ_id, event_type, scope_type, scope_id)
        DO UPDATE SET active = TRUE, muted_until = NULL, webhook_url = COALESCE($6, agentevents.subscriptions.webhook_url)
        RETURNING *
        """,
        civ_id,
        sub.event_type,
        sub.scope_type.value,
        sub.scope_id,
        sub.delivery_method.value,
        sub.webhook_url,
        sub.agentmail_address,
    )
    return dict(row)


@app.get("/subscriptions", response_model=SubscriptionList)
async def list_subscriptions(
    civ_id: str = Depends(get_civ_id),
    db: asyncpg.Connection = Depends(get_db),
):
    rows = await db.fetch(
        "SELECT * FROM agentevents.subscriptions WHERE civ_id = $1 AND active = TRUE",
        civ_id,
    )
    return {"subscriptions": [dict(r) for r in rows], "total": len(rows)}


@app.delete("/subscriptions/{sub_id}", status_code=204)
async def delete_subscription(
    sub_id: UUID,
    civ_id: str = Depends(get_civ_id),
    db: asyncpg.Connection = Depends(get_db),
):
    result = await db.execute(
        "DELETE FROM agentevents.subscriptions WHERE id = $1 AND civ_id = $2",
        sub_id,
        civ_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Subscription not found")


@app.post("/subscriptions/{sub_id}/mute")
async def mute_subscription(
    sub_id: UUID,
    req: MuteRequest,
    civ_id: str = Depends(get_civ_id),
    db: asyncpg.Connection = Depends(get_db),
):
    muted_until = datetime.now(timezone.utc).replace(microsecond=0)
    from datetime import timedelta
    muted_until += timedelta(minutes=req.duration_minutes)

    result = await db.execute(
        """
        UPDATE agentevents.subscriptions
        SET muted_until = $3
        WHERE id = $1 AND civ_id = $2
        """,
        sub_id,
        civ_id,
        muted_until,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"status": "muted", "muted_until": muted_until.isoformat()}


@app.post("/subscriptions/{sub_id}/unmute")
async def unmute_subscription(
    sub_id: UUID,
    civ_id: str = Depends(get_civ_id),
    db: asyncpg.Connection = Depends(get_db),
):
    result = await db.execute(
        """
        UPDATE agentevents.subscriptions
        SET muted_until = NULL
        WHERE id = $1 AND civ_id = $2
        """,
        sub_id,
        civ_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"status": "unmuted"}


@app.post("/subscriptions/mute-all")
async def mute_all_subscriptions(
    req: MuteRequest,
    civ_id: str = Depends(get_civ_id),
    db: asyncpg.Connection = Depends(get_db),
):
    from datetime import timedelta
    muted_until = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=req.duration_minutes)
    await db.execute(
        """
        UPDATE agentevents.subscriptions
        SET muted_until = $2
        WHERE civ_id = $1 AND active = TRUE
        """,
        civ_id,
        muted_until,
    )
    return {"status": "all_muted", "muted_until": muted_until.isoformat()}


# ─── Event Endpoints ──────────────────────────────────────────────────────────

@app.post("/events", status_code=201)
async def publish_event(
    event: EventCreate,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Publish an event. Called by Hub after successful writes.
    No JWT required — this is an internal service-to-service call.
    Restricted by network (localhost only in production).
    """
    from delivery import process_event

    # Store event and dispatch to subscribers
    await process_event(
        event_type=event.event_type,
        entity_id=event.payload.get("thread_id", event.payload.get("post_id", "")),
        room_id=event.payload.get("room_id"),
        group_id=event.payload.get("group_id"),
        created_by=event.payload.get("created_by", ""),
        created_at=datetime.now(timezone.utc).isoformat(),
        title=event.payload.get("title", ""),
        body_preview=event.payload.get("body_preview", ""),
        db_url=HUB_DB_URL,
    )

    return {"status": "published", "event_type": event.event_type}



@app.get("/events/pending", response_model=PendingEventsResponse)
async def poll_pending_events(
    since: datetime | None = None,
    limit: int = 50,
    civ_id: str = Depends(get_civ_id),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Poll for undelivered events matching the civ's poll-mode subscriptions.
    Returns events oldest-first. Caller should ACK with POST /events/ack.
    """
    if since:
        events = await db.fetch(
            """
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
              AND e.created_at > $3
            ORDER BY e.created_at ASC
            LIMIT $2
            """,
            civ_id,
            limit,
            since,
        )
    else:
        events = await db.fetch(
            """
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
            ORDER BY e.created_at ASC
            LIMIT $2
            """,
            civ_id,
            limit,
        )

    event_list = []
    for e in events:
        payload = dict(e["payload"])
        event_list.append({
            "id": str(e["id"]),
            "event_type": e["event_type"],
            "source": e["source"],
            "preview": payload.get("title", payload.get("body_preview", ""))[:200],
            "timestamp": e["created_at"].isoformat(),
            "resource_url": f"http://87.99.131.49:8900/api/v2/events/{e['id']}",
            "delivery_id": str(e["delivery_id"]),
        })

    return {"events": event_list, "total": len(event_list)}


@app.post("/events/ack")
async def ack_events(
    req: AckRequest,
    civ_id: str = Depends(get_civ_id),
    db: asyncpg.Connection = Depends(get_db),
):
    """Mark events as delivered (acked by subscriber)."""
    if not req.event_ids:
        return {"status": "ok"}

    await db.execute(
        """
        UPDATE agentevents.deliveries d
        SET status = 'delivered', delivered_at = NOW()
        FROM agentevents.subscriptions s
        WHERE d.id = ANY($1)
          AND d.subscription_id = s.id
          AND s.civ_id = $2
        """,
        req.event_ids,
        civ_id,
    )
    return {"status": "ok", "acknowledged": len(req.event_ids)}


@app.get("/events/{event_id}", response_model=EventResponse)
async def get_event(
    event_id: UUID,
    civ_id: str = Depends(get_civ_id),
    db: asyncpg.Connection = Depends(get_db),
):
    """Get a specific event by ID."""
    row = await db.fetchrow(
        "SELECT * FROM agentevents.events WHERE id = $1",
        event_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    return dict(row)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agentevents", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8400, log_level="info")
