"""
webhook_server.py — Automated license delivery on purchase
==========================================================
The scale-up path for the delivery flow: your payment processor calls this
endpoint on every sale; it issues the buyer's license.key and emails it.

Until you wire this up, `issue_license.py` (run manually per sale) does the same
job with zero infrastructure — start there, graduate to this when volume grows.

Supported processors (payload shapes differ; parse_event handles each):
  * Billplz         (form-encoded callback — Malaysia FPX / DuitNow)
  * Stripe          (JSON checkout.session.completed — international cards)
  * Lemon Squeezy   (JSON 'order_created' webhook)
  * Gumroad         (form-encoded 'sale' ping)

DuitNow QR (static/bank) and USDT (direct wallet) have no webhook — confirm the
payment, then issue manually with issue_license.py --method duitnow|usdt.

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
import time
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

    if provider == "billplz":
        # Billplz posts form fields; a paid bill has paid=true / state=paid.
        paid = str(payload.get("paid", "false")).lower() == "true" or payload.get("state") == "paid"
        if not paid:
            return None, None
        return payload.get("email"), payload.get("transaction_id") or payload.get("id")

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


def billplz_source_string(payload):
    """Billplz signs the sorted 'key+value' pairs (excluding x_signature)."""
    parts = [f"{k}{v}" for k, v in payload.items() if k != "x_signature"]
    parts.sort()
    return "|".join(parts)


def verify_billplz(secret, payload):
    """Verify a Billplz callback's x_signature (HMAC-SHA256 over sorted fields)."""
    if not secret:
        return True  # dev mode
    expected = hmac.new(secret.encode(), billplz_source_string(payload).encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(payload.get("x_signature", "")))


def _sig_pairs(header_sig):
    """Yield (key, value) pairs from a 't=..,v1=..,v1=..' signature header.

    A bare hex string (no '=', as LemonSqueezy/Gumroad send) yields a single
    ('v1', hex) pair so those providers flow through the same parser.
    """
    if "=" not in header_sig:
        yield "v1", header_sig
        return
    for part in header_sig.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            yield k.strip(), v.strip()


def verify_stripe(secret, raw_body, header_sig, tolerance=0):
    """Verify a Stripe webhook signature.

    Stripe signs `HMAC-SHA256(secret, f"{t}.{body}")` and sends
    `Stripe-Signature: t=<unix>,v1=<hexdigest>[,v1=<older>]`. The signed payload
    is the timestamp, a literal dot, then the raw body — reconstructing that is
    the fix: the previous code hashed the body alone, so every real Stripe
    webhook failed verification (401) once WEBHOOK_SECRET was set.

    `tolerance` (seconds) optionally rejects timestamps too far from now to blunt
    replay attacks; 0 disables it. Idempotency on order_id is the real replay
    guard, so this defaults off to avoid clock-skew false rejects on a fresh host.
    A header may carry several v1 values during secret rotation — any match wins.
    """
    if not secret:
        return True  # dev mode; configure WEBHOOK_SECRET in production
    if not header_sig:
        return False
    pairs = list(_sig_pairs(header_sig))
    t = next((v for k, v in pairs if k == "t"), None)
    if not t:
        return False
    if tolerance:
        try:
            if abs(time.time() - int(t)) > tolerance:
                return False
        except (TypeError, ValueError):
            return False
    signed = t.encode() + b"." + raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in pairs if k == "v1")


def verify_signature(provider, secret, raw_body, header_sig):
    """Best-effort HMAC check. Returns True if valid or if no secret configured."""
    if not secret:
        return True  # dev mode; configure WEBHOOK_SECRET in production
    if not header_sig:
        return False
    provider = (provider or "").lower()
    if provider == "stripe":
        tol = int(os.environ.get("STRIPE_SIG_TOLERANCE", "0") or 0)
        return verify_stripe(secret, raw_body, header_sig, tolerance=tol)
    if provider in ("lemonsqueezy", "gumroad"):
        # These sign the raw body directly; header is a bare hex HMAC.
        digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, header_sig)
    return False


# ── FastAPI app (imported lazily so the module unit-tests without fastapi) ────
def _build_app():
    from fastapi import FastAPI, Request, HTTPException

    app = FastAPI(title="ICT Vault license webhook")
    secret = os.environ.get("WEBHOOK_SECRET", "")

    # B1 — production guard: on a real deploy, refuse to run signature-less.
    # With no secret, verify_* returns True for ANY request, so anyone could POST
    # a forged sale and mint a free license. Render sets $RENDER; treat any
    # recognised deploy env as production.
    is_prod = any(os.environ.get(v) for v in ("RENDER", "FLY_APP_NAME", "DYNO", "K_SERVICE"))
    if is_prod and not secret:
        raise RuntimeError(
            "WEBHOOK_SECRET is not set in a production environment. Refusing to "
            "start: without it the webhook accepts forged events and mints free "
            "licenses. Set WEBHOOK_SECRET (the whsec_… from your Stripe webhook).")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/webhook/{provider}")
    async def webhook(provider: str, request: Request):
        raw = await request.body()
        ctype = request.headers.get("content-type", "")
        # B2 — a malformed body is the caller's fault: return 400 (permanent) so
        # the processor stops retrying, instead of a 500 it hammers for days.
        try:
            if "application/json" in ctype:
                payload = json.loads(raw or b"{}")
            else:
                payload = dict(await request.form())
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="unparseable body")

        # Billplz signs its payload fields; everyone else signs the raw body.
        if provider.lower() == "billplz":
            ok = verify_billplz(secret, payload)
        else:
            sig = (request.headers.get("X-Signature")
                   or request.headers.get("Stripe-Signature")
                   or request.headers.get("X-Gumroad-Signature"))
            ok = verify_signature(provider, secret, raw, sig)
        if not ok:
            raise HTTPException(status_code=401, detail="bad signature")

        email, order_id = parse_event(provider, payload)
        if not email:
            return {"status": "ignored"}  # not a fulfilable sale

        # Idempotency: processors re-deliver events on any non-2xx / timeout.
        # If we've already fulfilled this order, ack with 200 (so the processor
        # stops retrying) but don't mint a second license or re-email the buyer.
        if order_id and issue_license.find_issued(order_id):
            return {"status": "duplicate", "order_id": order_id}

        # B2 — issuance can fail transiently (e.g. SMTP hiccup). Let it surface
        # as 500 so the processor RETRIES. issue_license emails BEFORE writing
        # the ledger, so a failed send leaves no ledger row and the retry
        # re-delivers cleanly (rather than being skipped as a false duplicate).
        try:
            issue_license.issue(email, order_id, email_it=True, method=provider.lower())
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — 500 => processor retries
            raise HTTPException(status_code=500, detail="issuance failed; will retry") from e
        return {"status": "issued", "email": email}

    return app


# Exposed for `uvicorn store.webhook_server:app`
try:  # pragma: no cover - only when fastapi is installed
    app = _build_app()
except Exception:  # fastapi not installed — parse_event/verify_signature still importable
    app = None
