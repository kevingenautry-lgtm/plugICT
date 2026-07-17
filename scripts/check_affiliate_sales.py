#!/usr/bin/env python3
"""check_affiliate_sales.py — Cron script: check Stripe for new affiliate sales.

Every run:
  1. Fetch recent Checkout Sessions from Stripe
  2. Filter those with client_reference_id (affiliate code)
  3. Compare against already-notified log
  4. Print any NEW affiliate sales (cron delivers stdout to Telegram)

Env vars required:
  STRIPE_API_KEY=sk_live_...  (restricted read-only key is safe)

Output format (one per line, for cron delivery):
  AFFILIATE_SALE|code|buyer_email|amount|session_id
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "").strip()
if not STRIPE_API_KEY:
    print("ERROR: STRIPE_API_KEY not set", file=sys.stderr)
    sys.exit(1)

LOG_FILE = Path(os.environ.get(
    "AFFILIATE_LOG",
    str(Path(__file__).resolve().parent.parent / "store" / "affiliate_sales.log"),
))

# How far back to check (seconds). First run catches 7 days, subsequent runs
# catch whatever the cron interval is (30 min = 1800s, padded to 3600).
LOOKBACK = int(os.environ.get("AFFILIATE_LOOKBACK", "3600"))

STRIPE_API = "https://api.stripe.com/v1"


def stripe_get(path, params=None):
    """Call Stripe REST API and return parsed JSON."""
    url = f"{STRIPE_API}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        url = f"{url}?{qs}"
    req = Request(url, headers={
        "Authorization": f"Bearer {STRIPE_API_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except URLError as e:
        print(f"ERROR: Stripe API failed: {e}", file=sys.stderr)
        sys.exit(1)


def load_log():
    """Return set of already-notified session IDs."""
    if not LOG_FILE.exists():
        return set()
    seen = set()
    with open(LOG_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split("|")
                if len(parts) >= 5:
                    seen.add(parts[4])  # session_id
    return seen


def append_log(code, email, amount, session_id):
    """Log a notified affiliate sale."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts}|{code}|{email}|{amount}|{session_id}\n")


def main():
    seen = load_log()
    cutoff = int(time.time()) - LOOKBACK

    new_sales = []
    has_more = True
    starting_after = None

    while has_more:
        params = {
            "limit": 100,
            "created[gte]": cutoff,
            "expand[]": "data.customer_details",
        }
        if starting_after:
            params["starting_after"] = starting_after

        data = stripe_get("/checkout/sessions", params)
        sessions = data.get("data", [])
        has_more = data.get("has_more", False)

        for s in sessions:
            sid = s.get("id", "")
            ref = (s.get("client_reference_id") or "").strip()
            if not ref:
                continue  # no affiliate code
            if sid in seen:
                continue  # already notified

            # Only count completed, paid Checkout Sessions. A referral link can
            # create an unpaid or abandoned session; do not notify/pay until
            # Stripe confirms both conditions.
            if s.get("status") != "complete" or s.get("payment_status") != "paid":
                continue

            # Extract buyer email from customer_details or payment_intent
            email = ""
            cd = s.get("customer_details") or {}
            email = (cd.get("email") or "").strip()
            if not email:
                pi_data = s.get("payment_intent") or {}
                pi = pi_data if isinstance(pi_data, dict) else {}
                email = (pi.get("receipt_email") or "").strip()

            amount_total = s.get("amount_total", 0)
            currency = (s.get("currency") or "usd").upper()
            amount = f"{amount_total / 100:.2f} {currency}"

            new_sales.append((ref, email, amount, sid))

        if sessions:
            starting_after = sessions[-1]["id"]

    # Report new sales — print nothing if no new sales (cron silence)
    if not new_sales:
        return

    for ref, email, amount, sid in new_sales:
        print(f"AFFILIATE_SALE|{ref}|{email}|{amount}|{sid}")
        append_log(ref, email, amount, sid)


if __name__ == "__main__":
    main()
