"""
AgentEvents v1.0 — Local Event Bridge

For collocated civs (same machine as Hub). This bridge:
1. Registers as a webhook subscriber to AgentEvents
2. When events arrive, queries pane registry for the target civ's primary pane
3. Injects a notification into the right tmux pane

This is a BRIDGE component, not the main service.
The main service (server.py) runs on the Hub VPS.
This bridge runs on Corey's machine (same machine as ACG/Proof/Hengshi).
"""

import logging
import os
import json
import asyncio
import subprocess
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="AgentEvents Local Bridge")

PANE_REGISTRY_PATH = os.environ.get(
    "PANE_REGISTRY_PATH",
    "/home/corey/projects/AI-CIV/ACG/config/pane_registry.json"
)
BRIDGE_PORT = 8401  # Local webhook receiver port


# ─── Pane Registry Helpers ────────────────────────────────────────────────────

def get_primary_pane(civ_id: str) -> dict | None:
    """
    Query the pane registry for the newest primary pane of a civ.
    Returns dict with tmux_session, tmux_pane or None if not found.
    """
    try:
        with open(PANE_REGISTRY_PATH) as f:
            registry = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning(f"Pane registry not found at {PANE_REGISTRY_PATH}")
        return None

    panes = registry.get("panes", {})
    # Our registry is a dict keyed by name, not a list
    # Find the primary pane for this civ
    civ_map = {"acg": "acg-primary", "proof": "proof-primary", "qwen": "hengshi-primary", "hengshi": "hengshi-primary"}
    reg_name = civ_map.get(civ_id, f"{civ_id}-primary")
    entry = panes.get(reg_name)
    if entry:
        return entry
    return None


def inject_into_pane(session: str, message: str):
    """
    Send a notification message into a tmux pane.
    5x Enter presses to ensure Claude processes it.
    """
    try:
        pane = session if ":" in session else f"{session}:"
        cmd = [
            "tmux", "send-keys", "-t", pane, message
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        # 5 Enter presses
        import time
        for _ in range(5):
            subprocess.run(["tmux", "send-keys", "-t", pane, "", "Enter"],
                           capture_output=True)
            time.sleep(0.3)
        logger.info(f"Injected into pane: {session}")
    except subprocess.CalledProcessError as e:
        logger.error(f"tmux inject failed: {e}")


# ─── Bridge Endpoints ─────────────────────────────────────────────────────────

class WebhookEvent(BaseModel):
    event_type: str
    event_id: str
    source_civ: str
    scope: dict
    payload: dict
    timestamp: str
    resource_url: str


@app.post("/events")
async def receive_event(event: WebhookEvent):
    """
    AgentEvents calls this endpoint when a webhook event fires.
    We look up the target civ's pane and inject the notification.
    """
    logger.info(f"Bridge received event: {event.event_type} from {event.source_civ}")

    # Determine target civ from scope or payload
    target_civ = None
    group_id = event.scope.get("group_id")

    if group_id:
        # Look up which civs are members of this group
        # For now, broadcast to all known local civs
        target_civ = "proof"  # TODO: resolve group_id → member civs

    pane = get_primary_pane(target_civ or "proof")
    if not pane:
        logger.warning(f"No pane found for {target_civ or 'proof'}")
        return {"status": "no_pane_found"}

    tmux_session = pane.get("session", pane.get("tmux_session", ""))
    title = event.payload.get("title", "") or event.payload.get("body_preview", "")[:80]
    message = f"\r\n[HUB NOTIFICATION] {event.event_type}: {title}\r\n"

    inject_into_pane(tmux_session, message)
    return {"status": "injected", "pane": tmux_session}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agentevents-bridge", "local": True}


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=BRIDGE_PORT, log_level="info")
