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
   zip it. (Or assemble `query.py`, `mcp_server.py`, `vault_core.py`,
   `requirements.txt`, `setup.bat/sh`, `vault.bat/sh`, `docs/`, `examples/`,
   and `ict-vault.kevin` yourself.)
3. **Host the zip** somewhere with a stable URL (Cloudflare R2, Backblaze B2,
   S3, Bunny, even a GitHub Release asset). Put that URL in:
   - `landing/getting-started.html` (the `data-download` link)
   - `store/issue_license.py` via `ICT_VAULT_DOWNLOAD_URL`
4. **Pick a payment processor** (see below) and point its buy button /
   product at your Gumroad/Lemon Squeezy/Stripe checkout. Update
   `landing/index.html` `data-buy-link`.

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
