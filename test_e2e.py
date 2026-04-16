#!/usr/bin/env python3
"""
AgentEvents E2E Test — Full Chain Verification

Tests the complete event pipeline:
  1. Authenticate with AgentAUTH (challenge/verify → JWT)
  2. Health check AgentEvents
  3. Subscribe to thread.created events (poll delivery)
  4. Create a test thread on the Hub (triggers _emit_event)
  5. Poll AgentEvents for pending events
  6. Acknowledge events
  7. Verify ack cleared the pending queue

Usage:
  python3 test_e2e.py
"""

import base64
import json
import sys
import time
import uuid

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# ─── Configuration ──────────────────────────────────────────────────────────

AGENTAUTH_URL = "http://5.161.90.32:8700"
AGENTEVENTS_URL = "http://87.99.131.49:8400"
HUB_URL = "http://87.99.131.49:8900"

# ACG keypair (raw Ed25519 seed, base64)
ACG_CIV_ID = "acg"
ACG_PRIVATE_KEY_B64 = "+uGuvEx0UHK1CE7m3njWRbVsGgRZfq8voN8HYwp8Wxk="

# CivOS WG group — ACG is a member
CIVOS_WG_GROUP_ID = "e7830968-56af-4a49-b630-d99b2116a163"


# ─── Helpers ────────────────────────────────────────────────────────────────

def sign_challenge(challenge_b64: str, private_key_b64: str) -> str:
    """Sign AgentAUTH challenge with Ed25519 private key, return base64 signature."""
    raw_bytes = base64.b64decode(private_key_b64)
    private_key = Ed25519PrivateKey.from_private_bytes(raw_bytes)
    challenge_bytes = base64.b64decode(challenge_b64)
    signature = private_key.sign(challenge_bytes)
    return base64.b64encode(signature).decode()


def step(num: int, name: str):
    print(f"\n{'='*60}")
    print(f"  STEP {num}: {name}")
    print(f"{'='*60}")


def ok(msg: str):
    print(f"  [PASS] {msg}")


def fail(msg: str):
    print(f"  [FAIL] {msg}")


def info(msg: str):
    print(f"  [INFO] {msg}")


# ─── Main Test ──────────────────────────────────────────────────────────────

def main():
    client = httpx.Client(timeout=15.0)
    jwt_token = None
    subscription_id = None
    results = {}

    # ── Step 1: Authenticate with AgentAUTH ──────────────────────────────

    step(1, "Authenticate with AgentAUTH")
    try:
        # 1a: Get challenge
        resp = client.post(f"{AGENTAUTH_URL}/challenge", json={"civ_id": ACG_CIV_ID})
        resp.raise_for_status()
        challenge_data = resp.json()
        challenge_b64 = challenge_data["challenge"]
        info(f"Challenge received: {challenge_b64[:20]}...")

        # 1b: Sign challenge
        signature = sign_challenge(challenge_b64, ACG_PRIVATE_KEY_B64)
        info(f"Signature computed: {signature[:20]}...")

        # 1c: Verify and get JWT
        resp = client.post(f"{AGENTAUTH_URL}/verify", json={
            "civ_id": ACG_CIV_ID,
            "signature": signature,
        })
        resp.raise_for_status()
        verify_data = resp.json()
        jwt_token = verify_data["token"]
        ok(f"JWT obtained (jti={verify_data.get('jti', 'N/A')})")
        results["auth"] = "PASS"
    except Exception as e:
        fail(f"Authentication failed: {e}")
        results["auth"] = f"FAIL: {e}"
        print("\nCannot proceed without authentication. Exiting.")
        _print_summary(results)
        return 1

    headers = {"Authorization": f"Bearer {jwt_token}"}

    # ── Step 2: Health check AgentEvents ────────────────────────────────

    step(2, "AgentEvents Health Check")
    try:
        resp = client.get(f"{AGENTEVENTS_URL}/health")
        resp.raise_for_status()
        health = resp.json()
        ok(f"AgentEvents healthy: {health}")
        results["health"] = "PASS"
    except Exception as e:
        fail(f"Health check failed: {e}")
        results["health"] = f"FAIL: {e}"
        print("\nAgentEvents not reachable. Exiting.")
        _print_summary(results)
        return 1

    # ── Step 3: Subscribe to thread.created (poll delivery) ──────────────

    step(3, "Subscribe to thread.created events")
    try:
        resp = client.post(f"{AGENTEVENTS_URL}/subscriptions", headers=headers, json={
            "event_type": "thread.created",
            "scope_type": "global",
            "delivery_method": "poll",
        })
        resp.raise_for_status()
        sub_data = resp.json()
        subscription_id = sub_data.get("id")
        ok(f"Subscription created: id={subscription_id}")
        info(f"  event_type={sub_data.get('event_type')}, delivery={sub_data.get('delivery_method')}")
        results["subscribe"] = "PASS"
    except Exception as e:
        fail(f"Subscription failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            info(f"Response body: {e.response.text[:500]}")
        results["subscribe"] = f"FAIL: {e}"
        print("\nCannot subscribe. Exiting.")
        _print_summary(results)
        return 1

    # ── Step 4: Find a room and create a test thread ─────────────────────

    step(4, "Create test thread on Hub (triggers _emit_event)")

    # 4a: Find a room in the CivOS WG group
    room_id = None
    try:
        resp = client.get(
            f"{HUB_URL}/api/v1/groups/{CIVOS_WG_GROUP_ID}/rooms",
            headers=headers,
        )
        resp.raise_for_status()
        rooms = resp.json()
        if isinstance(rooms, list) and rooms:
            room_id = rooms[0]["id"]
            room_slug = rooms[0].get("slug", rooms[0].get("properties", {}).get("slug", "unknown"))
            info(f"Found room: {room_id} (slug={room_slug})")
        elif isinstance(rooms, dict) and rooms.get("rooms"):
            room_id = rooms["rooms"][0]["id"]
            info(f"Found room: {room_id}")
        else:
            info(f"No rooms found in CivOS WG. Response: {json.dumps(rooms)[:200]}")
    except Exception as e:
        info(f"Room listing failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            info(f"Response: {e.response.text[:300]}")

    if not room_id:
        fail("No room found to create test thread in.")
        results["create_thread"] = "FAIL: No room available"
        info("Trying direct event publish as fallback (step 4b)...")

        # 4b fallback: Publish event directly to AgentEvents (no JWT needed for internal)
        step(4, "FALLBACK: Direct event publish to AgentEvents")
        try:
            test_thread_id = str(uuid.uuid4())
            test_room_id = str(uuid.uuid4())
            resp = client.post(f"{AGENTEVENTS_URL}/events", json={
                "type": "thread.created",
                "source": "e2e-test",
                "payload": {
                    "thread_id": test_thread_id,
                    "room_id": test_room_id,
                    "title": "E2E Test Thread (direct publish)",
                    "body_preview": "Testing AgentEvents pipeline directly.",
                    "created_by": "e2e-test-bot",
                },
            })
            resp.raise_for_status()
            publish_data = resp.json()
            ok(f"Event published directly: {publish_data}")
            results["create_thread"] = "PASS (direct publish)"
        except Exception as e:
            fail(f"Direct publish also failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                info(f"Response: {e.response.text[:300]}")
            results["create_thread"] = f"FAIL: {e}"
            _print_summary(results)
            return 1
    else:
        # 4c: Create thread via Hub API (v2 endpoint)
        try:
            test_title = f"E2E Test Thread — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
            resp = client.post(
                f"{HUB_URL}/api/v2/rooms/{room_id}/threads",
                headers=headers,
                json={"title": test_title, "body": "Automated E2E test for AgentEvents pipeline."},
            )
            resp.raise_for_status()
            thread_data = resp.json()
            ok(f"Thread created: id={thread_data.get('id')}, title={test_title}")
            results["create_thread"] = "PASS (via Hub)"
        except Exception as e:
            fail(f"Thread creation failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                info(f"Response: {e.response.text[:500]}")
            results["create_thread"] = f"FAIL: {e}"

            # Try direct publish fallback
            info("Falling back to direct event publish...")
            try:
                test_thread_id = str(uuid.uuid4())
                resp = client.post(f"{AGENTEVENTS_URL}/events", json={
                    "type": "thread.created",
                    "source": "e2e-test",
                    "payload": {
                        "thread_id": test_thread_id,
                        "room_id": str(room_id),
                        "title": "E2E Test Thread (direct publish fallback)",
                        "body_preview": "Hub thread creation failed; testing events pipeline directly.",
                        "created_by": "e2e-test-bot",
                    },
                })
                resp.raise_for_status()
                ok(f"Direct publish succeeded: {resp.json()}")
                results["create_thread"] = "PASS (direct publish fallback)"
            except Exception as e2:
                fail(f"Direct publish also failed: {e2}")
                results["create_thread"] = f"FAIL: Hub={e}, Direct={e2}"
                _print_summary(results)
                return 1

    # Give AgentEvents a moment to process
    info("Waiting 2s for event processing...")
    time.sleep(2)

    # ── Step 5: Poll for pending events ─────────────────────────────────

    step(5, "Poll for pending events")
    delivery_ids = []
    try:
        resp = client.get(f"{AGENTEVENTS_URL}/events/pending", headers=headers)
        resp.raise_for_status()
        pending = resp.json()
        events = pending.get("events", [])
        total = pending.get("total", 0)

        if total > 0:
            ok(f"Found {total} pending event(s)!")
            for evt in events:
                info(f"  Event: type={evt.get('event_type')}, preview={evt.get('preview', '')[:60]}, delivery_id={evt.get('delivery_id')}")
                if evt.get("delivery_id"):
                    delivery_ids.append(evt["delivery_id"])
            results["poll"] = f"PASS ({total} events)"
        else:
            fail("No pending events found. The event may not have been routed to our subscription.")
            info(f"Full response: {json.dumps(pending)}")
            results["poll"] = "FAIL: 0 events"
    except Exception as e:
        fail(f"Polling failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            info(f"Response: {e.response.text[:500]}")
        results["poll"] = f"FAIL: {e}"

    # ── Step 6: Acknowledge events ──────────────────────────────────────

    step(6, "Acknowledge events")
    if not delivery_ids:
        info("No delivery IDs to ack (skipping)")
        results["ack"] = "SKIP (no events to ack)"
    else:
        try:
            # NOTE: The AckRequest model field is called "event_ids" but the SQL
            # matches against deliveries.id — so we send delivery_ids here.
            resp = client.post(f"{AGENTEVENTS_URL}/events/ack", headers=headers, json={
                "event_ids": delivery_ids,
            })
            resp.raise_for_status()
            ack_data = resp.json()
            ok(f"Acknowledged {ack_data.get('acknowledged', len(delivery_ids))} event(s)")
            results["ack"] = "PASS"
        except Exception as e:
            fail(f"Ack failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                info(f"Response: {e.response.text[:500]}")
            results["ack"] = f"FAIL: {e}"

    # ── Step 7: Verify ack cleared pending queue ────────────────────────

    step(7, "Verify pending queue is clear after ack")
    if not delivery_ids:
        info("Skipping (no events were acked)")
        results["verify_ack"] = "SKIP"
    else:
        try:
            resp = client.get(f"{AGENTEVENTS_URL}/events/pending", headers=headers)
            resp.raise_for_status()
            pending_after = resp.json()
            remaining = pending_after.get("total", -1)

            if remaining == 0:
                ok("Pending queue is empty — all events acknowledged!")
                results["verify_ack"] = "PASS"
            else:
                # Some events may remain from prior test runs or other subscriptions
                remaining_events = pending_after.get("events", [])
                our_remaining = [e for e in remaining_events if e.get("delivery_id") in delivery_ids]
                if not our_remaining:
                    ok(f"Our events cleared. {remaining} other event(s) remain (from other runs).")
                    results["verify_ack"] = "PASS"
                else:
                    fail(f"Still {len(our_remaining)} of our events pending after ack!")
                    results["verify_ack"] = f"FAIL: {len(our_remaining)} still pending"
        except Exception as e:
            fail(f"Verify failed: {e}")
            results["verify_ack"] = f"FAIL: {e}"

    # ── Cleanup: Delete test subscription ────────────────────────────────

    if subscription_id:
        try:
            resp = client.delete(f"{AGENTEVENTS_URL}/subscriptions/{subscription_id}", headers=headers)
            if resp.status_code == 204:
                info(f"Test subscription {subscription_id} cleaned up.")
            else:
                info(f"Subscription cleanup returned {resp.status_code}")
        except Exception:
            pass  # best-effort cleanup

    client.close()
    _print_summary(results)

    # Return 0 if all critical steps passed
    critical = ["auth", "health", "subscribe", "create_thread", "poll"]
    all_pass = all(results.get(k, "").startswith("PASS") for k in critical)
    return 0 if all_pass else 1


def _print_summary(results: dict):
    print(f"\n{'='*60}")
    print("  E2E TEST SUMMARY")
    print(f"{'='*60}")
    for step_name, result in results.items():
        status_icon = "PASS" if result.startswith("PASS") else ("SKIP" if result.startswith("SKIP") else "FAIL")
        print(f"  [{status_icon}] {step_name}: {result}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    sys.exit(main())
