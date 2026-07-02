"""
webhook_server.py — Automated license delivery on purchase
==========================================================
The scale-up path for the delivery flow: your payment processor calls this
endpoint on every sale; it issues the buyer's license.key and emails it.

Until you wire this up, `issue_license.py` (run manually per sale) does the same
job with zero infrastructure — start there, graduate to this when volume grows.

Supported processors (payload shapes differ; parse_event handles each):
  * Gumroad         (form-encoded 'sale' ping)
  * Lemon Squeezy   (JSON 'order_created' webhook)
  * Stripe          (JSON checkout.session.completed)

Run:
  pip install fastapi uvicorn
  ICT_SOURCE_DIR=/path/to/seller/secrets \
  WEBHOOK_SECRET=... SMTP_HOST=... SMTP_USER=... SMTP_PASS=... \
  uvicorn store.webhook_server:app --host 0.0.0.0 --port 8000

Security notes are in store/README.md — verify signatures, never expose
.vault_key, run behind HTTPS.
"""

import os
import sys
import json
import hmac
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import issue_license  # noqa: E402


def parse_event(provider, payload):
    """Extract (email, order_id) from a processor payload. Pure + unit-tested.

    `payload` is a dict (already-parsed JSON, or form fields as a dict).
    Returns (email, order_id) or (None, None) if this event isn't a completed
    sale we should fulfil.
    """
    provider = (provider or "").lower()

    if provider == "gumroad":
        # Gumroad posts form fields; a refund/dispute ping sets these flags.
        if str(payload.get("refunded", "false")).lower() == "true":
            return None, None
        return payload.get("email"), payload.get("sale_id") or payload.get("order_number")

    if provider == "lemonsqueezy":
        if payload.get("meta", {}).get("event_name") != "order_created":
            return None, None
        attrs = payload.get("data", {}).get("attributes", {})
        return attrs.get("user_email"), str(payload.get("data", {}).get("id", "")) or attrs.get("identifier")

    if provider == "stripe":
        if payload.get("type") != "checkout.session.completed":
            return None, None
        obj = payload.get("data", {}).get("object", {})
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
        return email, obj.get("id")

    return None, None


def verify_signature(provider, secret, raw_body, header_sig):
    """Best-effort HMAC check. Returns True if valid or if no secret configured."""
    if not secret:
        return True  # dev mode; configure WEBHOOK_SECRET in production
    if not header_sig:
        return False
    provider = (provider or "").lower()
    if provider in ("lemonsqueezy", "stripe", "gumroad"):
        digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        # Stripe uses a t=..,v1=.. scheme; this compares the raw hex for LS/Gumroad
        # and the v1 component for Stripe if present.
        candidate = header_sig
        if "v1=" in header_sig:
            candidate = dict(p.split("=", 1) for p in header_sig.split(",") if "=" in p).get("v1", "")
        return hmac.compare_digest(digest, candidate)
    return False


# ── FastAPI app (imported lazily so the module unit-tests without fastapi) ────
def _build_app():
    from fastapi import FastAPI, Request, HTTPException

    app = FastAPI(title="ICT Vault license webhook")
    secret = os.environ.get("WEBHOOK_SECRET", "")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/webhook/{provider}")
    async def webhook(provider: str, request: Request):
        raw = await request.body()
        sig = (request.headers.get("X-Signature")
               or request.headers.get("Stripe-Signature")
               or request.headers.get("X-Gumroad-Signature"))
        if not verify_signature(provider, secret, raw, sig):
            raise HTTPException(status_code=401, detail="bad signature")

        ctype = request.headers.get("content-type", "")
        if "application/json" in ctype:
            payload = json.loads(raw or b"{}")
        else:
            form = await request.form()
            payload = dict(form)

        email, order_id = parse_event(provider, payload)
        if not email:
            return {"status": "ignored"}  # not a fulfilable sale

        issue_license.issue(email, order_id, email_it=True)
        return {"status": "issued", "email": email}

    return app


# Exposed for `uvicorn store.webhook_server:app`
try:  # pragma: no cover - only when fastapi is installed
    app = _build_app()
except Exception:  # fastapi not installed — parse_event/verify_signature still importable
    app = None
