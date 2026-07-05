"""
issue_license.py — Turn a paid order into a delivered license.key
=================================================================
This is the working core of the post-purchase delivery flow (local/lifetime).

The big encrypted vault + app are hosted once and shared by everyone; the only
per-buyer artifact is the tiny license.key this produces.

Usage
-----
  # One buyer (writes store/issued/license_<email>.key and logs it):
  python store/issue_license.py buyer@example.com ORDER-1234

  # Many buyers from a CSV with columns: email,order_id
  python store/issue_license.py --batch orders.csv

  # Also email each license (needs SMTP_* env vars, see store/README.md):
  python store/issue_license.py buyer@example.com ORDER-1234 --email

Every issuance is appended to store/issued_licenses.csv so you always have a
record for support, refunds and traceability.
"""

import os
import sys
import csv
import ssl
import shutil
import smtplib
import argparse
from pathlib import Path
from datetime import datetime, timezone
from email.message import EmailMessage

# Reuse the vetted envelope-encryption logic.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_key import generate_license  # noqa: E402
import emails  # noqa: E402

STORE_DIR = Path(__file__).resolve().parent
# Where .vault_key / .vault_sha256 live (seller secrets). Override with env.
SOURCE_DIR = Path(os.environ.get("ICT_SOURCE_DIR", STORE_DIR.parent / "scripts"))
ISSUED_DIR = STORE_DIR / "issued"
LEDGER = STORE_DIR / "issued_licenses.csv"


def issue(email, order_id, email_it=False, method="manual"):
    """Generate one license.key for a paid order. Returns the license path.

    `method` records how they paid (duitnow, usdt, fpx/billplz, stripe, ...) in
    the ledger, so you can reconcile against each gateway.
    """
    email = email.strip().lower()
    order_id = (order_id or f"ORDER-{datetime.now(timezone.utc):%Y%m%d%H%M%S}").strip()
    ISSUED_DIR.mkdir(exist_ok=True)

    # generate_license writes next to vault_dir; point it at SOURCE_DIR, then
    # move the result into store/issued/ so seller secrets stay put.
    out_file, license_id = generate_license(email, order_id, vault_dir=SOURCE_DIR)
    dest = ISSUED_DIR / out_file.name
    shutil.move(str(out_file), str(dest))

    _log(email, order_id, license_id, dest.name, method)
    print(f"  issued  {email}  →  {dest.name}  (license {license_id}, {method})")

    if email_it:
        _email_license(email, dest, license_id)
    return dest


def find_issued(order_id):
    """Return the ledger row (dict) for an already-fulfilled order_id, or None.

    The webhook uses this to stay idempotent: payment processors (Stripe in
    particular) re-deliver an event on any non-2xx response or timeout, and a
    naive handler would mint a second license and email the buyer twice. Keyed
    on the processor's own order/session id, which is stable across retries.

    Note: this is a read-before-write check, not a lock. On a single webhook
    instance with retries seconds apart that's sufficient; it is not safe
    against two genuinely concurrent deliveries of the same event (worst case:
    one duplicate email — acceptable at this volume, not worth a file lock).
    """
    if not order_id or not LEDGER.exists():
        return None
    key = str(order_id).strip()
    with open(LEDGER, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("order_id") or "").strip() == key:
                return row
    return None


def _log(email, order_id, license_id, filename, method):
    new = not LEDGER.exists()
    with open(LEDGER, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["issued_at_utc", "email", "order_id", "license_id", "file", "method"])
        w.writerow([datetime.now(timezone.utc).isoformat(), email, order_id, license_id, filename, method])


def _email_license(email, license_path, license_id):
    """Send the license.key as an attachment via SMTP. Config via env vars."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("  (skipping email: SMTP_HOST not set — see store/README.md)")
        return
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    pw = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("SMTP_FROM", user)

    subject, text, html = emails.license_email(email, license_id)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with open(license_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="octet-stream",
                           filename="license.key")

    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port) as s:
        s.starttls(context=ctx)
        if user:
            s.login(user, pw)
        s.send_message(msg)
    print(f"  emailed license to {email}")


def _batch(csv_path, email_it, method="manual"):
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("No rows in CSV.")
        return
    for r in rows:
        addr = r.get("email") or r.get("Email")
        order = r.get("order_id") or r.get("order") or ""
        if addr:
            issue(addr, order, email_it, r.get("method") or method)


def main():
    p = argparse.ArgumentParser(description="Issue ICT Vault licenses for paid orders.")
    p.add_argument("email", nargs="?", help="Buyer email")
    p.add_argument("order_id", nargs="?", help="Order / purchase ID")
    p.add_argument("--batch", help="CSV file with columns: email,order_id")
    p.add_argument("--email", action="store_true", dest="email_it",
                   help="Also email the license (needs SMTP_* env vars)")
    p.add_argument("--method", default="manual",
                   help="Payment method for the ledger: duitnow, usdt, fpx, stripe, ... (default manual)")
    args = p.parse_args()

    if args.batch:
        _batch(args.batch, args.email_it, args.method)
    elif args.email:
        issue(args.email, args.order_id, args.email_it, args.method)
    else:
        p.error("Provide a buyer email (and optional order_id), or --batch <csv>.")


if __name__ == "__main__":
    main()
