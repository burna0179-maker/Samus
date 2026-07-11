#!/usr/bin/env python3
"""
Stripe Webhook Receiver — production-grade signed-webhook ingress
Source: ChatGPT recovery chat 10 (stripe receiver build + Caddy reverse-proxy setup)

Canonical relationship:
- [EXPANDS §6 application] webhook ingress sits beside FastAPI gateway on :8081
- [EXPANDS §6 application middleware] HMAC-SHA256 signature + 5-min replay window
                                       (parallel to canonical ReplayProtectionMiddleware)
- [EXPANDS §6 orchestration] idempotent dispatch via stripe-{session_id} task_id
- [DEFERRED] startup-coupling fix: only fail-closed on /webhooks/stripe; let
             other intake routes (/intake/prospect/zipcode, /intake/onboarding,
             /intake/seo, /intake/call-result) start when STRIPE_WEBHOOK_SECRET
             is missing.

Architectural note: the *current* receiver dies entirely on missing
STRIPE_WEBHOOK_SECRET, which is what crashed the v1.7 prospect lane
(see memory: project_samus_crm_design). The corrected pattern below
gates only the Stripe route.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("samus.webhook.stripe")


# ----- Product catalog (commercial mapping → fulfillment dispatch) -----
PRODUCT_CATALOG = {
    "prod_U913rQk3aII2T4": {
        "name": "48-Hour Workflow Rescue",
        "amount_usd": 500.00,
        "dispatch_service": "fulfillment",
        "dispatch_action": "plan_execution",
    },
    # SEO products from chat 09:
    "prod_SEO_AUDIT": {"name": "SEO Audit", "dispatch_service": "seo", "dispatch_action": "audit_site"},
    "prod_SEO_IMPL":  {"name": "SEO Implementation", "dispatch_service": "seo", "dispatch_action": "optimize_page"},
    "prod_SEO_OPT":   {"name": "SEO Optimization", "dispatch_service": "seo", "dispatch_action": "mape_k_cycle"},
    "prod_SEO_AUTO":  {"name": "SEO Automation", "dispatch_service": "fulfillment", "dispatch_action": "plan_execution"},
}

AMOUNT_FALLBACK = {
    50000: "prod_U913rQk3aII2T4",       # $500.00 → 48-hour workflow
}

REPLAY_WINDOW_SEC = 300


# ----- Signature verification (HMAC-SHA256, Stripe-compatible) -----
def verify_stripe_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    if not sig_header or not secret:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    ts = parts.get("t")
    v1 = parts.get("v1")
    if not ts or not v1:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > REPLAY_WINDOW_SEC:
        return False
    signed = f"{ts}.{payload.decode('utf-8')}".encode()
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


# ----- Event router (Stripe checkout.session.completed → dispatch) -----
def route_checkout_session(session: Dict[str, Any], dispatch_fn) -> Dict[str, Any]:
    session_id = session["id"]
    metadata = session.get("metadata") or {}
    product_id = metadata.get("product_id")

    if not product_id:
        amount = session.get("amount_total")
        product_id = AMOUNT_FALLBACK.get(amount)

    if product_id not in PRODUCT_CATALOG:
        return {"dispatched": False, "reason": "product_not_in_catalog", "session_id": session_id}

    cfg = PRODUCT_CATALOG[product_id]
    task_id = f"stripe-{session_id}"   # idempotent → 409 from gateway = already-handled

    payload = {
        "session_id": session_id,
        "product_id": product_id,
        "amount_total": session.get("amount_total"),
        "customer_email": (session.get("customer_details") or {}).get("email"),
    }

    try:
        dispatch_fn(
            service=cfg["dispatch_service"],
            action=cfg["dispatch_action"],
            task_id=task_id,
            payload=payload,
            idempotency_key=task_id,
        )
        return {"dispatched": True, "task_id": task_id, "product": cfg["name"]}
    except Exception as e:
        # Return non-2xx → Stripe will retry
        raise RuntimeError(f"dispatch_failed: {e}") from e


# ----- Startup-validation IMPROVED PATTERN (not the prior all-or-nothing) -----
def validate_config_per_route(env: Optional[Dict[str, str]] = None) -> Dict[str, bool]:
    """Return per-route enablement instead of crashing the whole receiver."""
    env = env or os.environ
    stripe_secret_present = bool(env.get("STRIPE_WEBHOOK_SECRET"))
    return {
        "stripe_enabled": stripe_secret_present,
        "intake_prospect_enabled": True,      # never gated by Stripe
        "intake_onboarding_enabled": True,
        "intake_seo_enabled": True,
        "intake_call_result_enabled": True,
    }


# ----- FastAPI app skeleton -----
def create_app(dispatch_fn):
    try:
        from fastapi import FastAPI, Request, HTTPException
    except ImportError:
        return None

    app = FastAPI(title="samus-webhook-receiver")
    enablement = validate_config_per_route()

    @app.get("/health")
    def health():
        return {"status": "ok", "routes": enablement}

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request):
        if not enablement["stripe_enabled"]:
            raise HTTPException(503, "stripe_webhook_disabled_missing_secret")
        body = await request.body()
        sig = request.headers.get("Stripe-Signature", "")
        if not verify_stripe_signature(body, sig, os.environ["STRIPE_WEBHOOK_SECRET"]):
            raise HTTPException(400, "invalid_signature")
        event = json.loads(body.decode("utf-8"))
        if event.get("type") != "checkout.session.completed":
            return {"ignored": True, "type": event.get("type")}
        session = event["data"]["object"]
        return route_checkout_session(session, dispatch_fn)

    return app
