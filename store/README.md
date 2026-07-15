> ⚠️ LEGACY NOTICE — no longer in production use.
# Delivery Flow — Local / Lifetime

How a paid order becomes a working install on the buyer's machine. Model:
**local desktop app, pay once.**

## The key idea: the big file is public, the license is private

The vault (`ict-vault.kevin`) is AES-256 encrypted and **useless without a
per-buyer `license.key`**. So you split delivery into two halves:

| Artifact | Size | Where it lives | Per-buyer? |
|---|---|---|---|
| `ict-vault.kevin` + app zip | ~200 MB | **Hosted once** on any static host / CDN (public link is fine) | No — same for everyone |
| `license.key` | < 1 KB | **Generated per sale**, emailed to the buyer | **Yes** |

This means no big per-customer uploads, no bandwidth surprises, and the only
thing you generate per sale is a tiny text file.

## One-time setup (seller side)

1. **Build the vault:** `python scripts/build.py` → produces `ict-vault.kevin`,
   `.vault_key`, `.vault_sha256`. Keep `.vault_key` secret — it's the master.
2. **Make the buyer zip** (app + docs + vault, *no* `.vault_key`):
   `python scripts/deliver.py "you@example.com" "TEST"` builds a delivery folder;
   zip it. (Or assemble `mcp_server.py`, `vault_core.py`,
   `requirements.txt`, `setup.bat/sh`, `docs/`, `examples/`,
   and `ict-vault.kevin` yourself.)
3. **Host the zip** somewhere with a stable URL (Cloudflare R2, Backblaze B2,
   S3, Bunny, even a GitHub Release asset). Put that URL in:
   - `landing/getting-started.html` (the `data-download` link)
   - `store/issue_license.py` via `ICT_VAULT_DOWNLOAD_URL`
4. **Pick a payment processor** (see below) and paste its checkout URL into
   the `window.PLUGICT` config block at the top of `index.html`.

## Issuing licenses

### MVP — manual, zero infrastructure (start here)

For each sale (you'll get the buyer email + order id from the processor):

```bash
# writes store/issued/license_<email>.key and logs the sale to
# store/issued_licenses.csv
python store/issue_license.py buyer@example.com ORDER-1234
```

Then email them that file + the download link + a link to the Getting Started
page. To have the script email it for you, set the SMTP env vars and add
`--email` (see below). Batch a backlog with `--batch orders.csv`
(columns: `email,order_id`).

At low volume this 20-second step per sale is completely fine.

### Automated — webhook (graduate to this at volume)

`store/webhook_server.py` receives a purchase webhook and issues + emails the
license automatically.

```bash
pip install fastapi uvicorn
export ICT_SOURCE_DIR=/secure/path/with/.vault_key
export ICT_VAULT_DOWNLOAD_URL=https://your-cdn/ict-vault.zip
export WEBHOOK_SECRET=...        # from your processor's webhook settings
export SMTP_HOST=...  SMTP_USER=...  SMTP_PASS=...  SMTP_FROM=you@domain
uvicorn store.webhook_server:app --host 0.0.0.0 --port 8000
```

Point your processor's webhook at `https://your-host/webhook/<provider>` where
`<provider>` is `gumroad`, `lemonsqueezy`, or `stripe`. The parser understands
each payload shape and ignores refunds / non-purchase events.

**Signatures & retries (important before you go live):**

- **`WEBHOOK_SECRET` must be set in production.** With no secret the server
  runs in *dev mode* and accepts every request — convenient for local testing,
  fatal on a public URL (anyone could forge a sale and mint a free license).
  Note this means a signature bug can hide during a secret-less test and only
  surface once you set the secret — so **run your test buy with the secret set.**
- **Stripe** signs `HMAC-SHA256(secret, "<timestamp>.<body>")` and sends it as
  `Stripe-Signature: t=…,v1=…`. Use the **signing secret** from the webhook
  endpoint (`whsec_…`), not your API key.
- **Replay window:** set `STRIPE_SIG_TOLERANCE=300` (seconds) to reject Stripe
  events whose timestamp is too old. Off by default so a fresh host with clock
  skew doesn't reject real webhooks; idempotency (below) is the primary guard.
- **Idempotency is automatic.** Processors re-deliver an event on any non-2xx
  response or timeout. The handler looks up the order/session id in
  `issued_licenses.csv` first and, if already fulfilled, returns
  `{"status":"duplicate"}` with 200 — no second license, no second email. Keep
  the ledger intact for this to work.

## Payment methods (Malaysia-first)

Four methods, split by how the license gets issued:

| Method | Fee | Best for | License issuance |
|---|---|---|---|
| **Billplz** (FPX / DuitNow) | ~1.5% | Malaysia, automatic | **Automated** — webhook `/webhook/billplz` |
| **Stripe** | ~2.9% | International cards | **Automated** — webhook `/webhook/stripe` |
| **DuitNow QR** (static/bank) | 0% | Malaysia, manual | **Manual** — confirm, then `issue_license.py ... --method duitnow` |
| **USDT** (Solana / TRC20) | ~gas | Crypto / international | **Manual** — confirm the tx, then `... --method usdt` |

- **Billplz** is your automation workhorse for Malaysia — it covers FPX online
  banking *and* DuitNow, and its callback is verified by `verify_billplz`
  (signs the sorted payload fields, not the raw body). Set your Billplz
  **X-Signature key** as `WEBHOOK_SECRET`.
- **DuitNow QR** paid straight to your bank has no callback, so it's a manual
  step: when the money lands, run
  `python store/issue_license.py buyer@email ORDER-ID --method duitnow --email`.
- **USDT** direct-to-wallet is the same manual pattern (verify the on-chain tx,
  then issue with `--method usdt`). Want it automated later? A crypto processor
  like NOWPayments/Coinbase Commerce adds a webhook — we can add a parser then.

Because your `license.key` is *custom* (envelope-encrypted), don't use a
gateway's built-in "license key" generator — let `issue_license.py` /
`webhook_server.py` mint the real key. Use the gateway only for the payment +
the purchase event.

### Malaysian seller notes
- All four methods route through the **same** `issue_license.issue(...)`, so
  the vault, license format and buyer experience are identical regardless of
  how they paid. The only difference is automated vs manual triggering.
- The ledger's `method` column lets you reconcile against each gateway and see
  the payment mix. DuitNow QR at 0% fee is your best-margin option — worth
  featuring for local buyers.

## Wiring the landing page (frontend half)

The landing page (`index.html` — the only copy; `landing/index.html` is just
a redirect stub) has a payment-method
modal wired to every **Get Lifetime Access** button. All of its knobs live in
one `window.PLUGICT` config block at the top of the file — fill in what you
have; anything left empty degrades gracefully:

| Config field | Where you get it | Empty behaviour |
|---|---|---|
| `billplzUrl` | Billplz → create a Payment Form for RM price → copy URL | Method shows "SOON" + email fallback |
| `stripeUrl` | Stripe → Payment Links → create $23.99 link → copy URL | Method shows "SOON" + email fallback |
| `duitnowQrImg` | Export your bank's DuitNow QR as an image, commit it, set path | Pane shows instructions without QR |
| `usdtSol` / `usdtTrc20` | Your wallet addresses | Address rows hidden |
| `supportEmail` | Where buyers send receipts / tx hashes | — |
| `priceMyr` | Optional MYR display, e.g. `'RM 105'` | USD-equivalent wording |
| `saleEndsAt` | A **real** launch-price deadline (ISO 8601) | Countdown hidden — no fake scarcity |
| `emailFormAction` | Formspree/Buttondown endpoint for launch updates | Falls back to `mailto:` |

Checklist to go fully live:

1. Create the Billplz payment form and Stripe payment link; paste both URLs
   into the config. Both checkouts collect the buyer's email.
2. Point each processor's webhook at your deployed
   `store/webhook_server.py` (`/webhook/billplz`, `/webhook/stripe`) with
   `WEBHOOK_SECRET` set — from then on those two methods are fully automated:
   pay → license emailed, no manual step.
3. DuitNow QR and USDT stay manual by design: buyer emails proof to
   `supportEmail`, you verify, then
   `python store/issue_license.py buyer@email ORDER-ID --method duitnow|usdt --email`.
4. Keep the config's price in sync with the processors if you change it.

## Security must-dos

- **Never** put `.vault_key` in the buyer zip, the repo, or a public host. It
  lives only where you issue licenses.
- Run the webhook behind **HTTPS** and set `WEBHOOK_SECRET` so forged requests
  can't mint free licenses (`verify_signature` enforces it).
- Keep `store/issued_licenses.csv` — it's your record for refunds, support and
  tracing a leaked license back to a buyer.
- Sending the license by email publishes it to that inbox; that's expected, but
  don't also log full key contents anywhere public.

## Honesty / positioning reminder

This flow ships transcripts of a third party's videos. It keeps content only on
buyers' machines (lowest-exposure posture), but the underlying rights question
is unresolved and is the gate on scaling — especially before moving to any
*hosted* connector where the content would sit on your servers.
