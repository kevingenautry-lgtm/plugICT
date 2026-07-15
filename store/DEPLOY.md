> ⚠️ LEGACY NOTICE — no longer in production use.
# Go-Live Runbook — money → license email in inbox

The one-time setup that turns a paid Stripe order into an automatic license
email. ~1–2 hours, mostly waiting on account signups. Everything the *code*
needs is already in the repo (`webhook_server.py`, `render.yaml`,
`store/requirements.txt`, `store/.env.example`); this is the **account + click**
half only you can do.

Order matters — do the sections top to bottom.

---

## 0. Before you start — what you need on hand

- [ ] The repo deploys from: `godzillacode0000/plugICT`
- [ ] `.vault_key` and `.vault_sha256` from `python scripts/build.py` (the master
      key that mints licenses). **These never go in the repo or a public place.**
- [ ] The buyer download zip hosted (Section 4) so the email link works.

---

## 1. Resend (email) — ~15 min

1. Sign up at resend.com (free tier).
2. Add & verify a sender. Fastest: verify a domain you own (add the DNS records
   they show). No domain? Use their onboarding sender to test, but buyers'
   inboxes trust a verified domain far more — do the domain before real sales.
3. Create an **API key** (`re_...`). Copy it.
4. You now have your SMTP values:
   - `SMTP_HOST=smtp.resend.com`  ·  `SMTP_PORT=587`
   - `SMTP_USER=resend`  ·  `SMTP_PASS=<your re_... key>`
   - `SMTP_FROM=<your verified sender>`

> Why SMTP and not their API? Zero code change — the webhook already sends over
> SMTP. Keep it simple for launch.

---

## 2. Render (webhook host) — ~20 min

1. Sign up at render.com, connect your GitHub, grant access to `plugICT`.
2. **New + → Blueprint → pick the repo.** Render reads `render.yaml` and creates
   the `plugict-webhook` service.
3. It will prompt for every secret marked `sync: false`. Paste:
   - `WEBHOOK_SECRET` — leave blank for now, you fill it in Section 3 step 3.
   - `SMTP_*` and `ICT_SUPPORT_EMAIL` — from Section 1.
4. **Upload the vault key as a Secret File** (Service → Environment → Secret
   Files): add `.vault_key` and `.vault_sha256` with mount path `/etc/secrets/`
   (matches `ICT_SOURCE_DIR` in `render.yaml`). This is how the server mints
   licenses without the key ever touching the repo.
5. Deploy. When it's live, note the URL, e.g. `https://plugict-webhook.onrender.com`.
6. Check `https://<your-url>/health` returns `{"ok": true}`.

> Free-tier Render sleeps after inactivity and cold-starts in ~30–60s. Stripe
> retries webhooks, so a cold start just means the first delivery may retry once
> — the idempotency guard makes that safe. Upgrade later if you want instant.

---

## 3. Stripe (point the webhook) — ~10 min

1. Dashboard → Developers → **Webhooks → Add endpoint**.
2. Endpoint URL: `https://<your-render-url>/webhook/stripe`
3. Events: select **`checkout.session.completed`**.
4. Save, then copy the endpoint's **Signing secret** (`whsec_...`).
5. Back in Render → Environment → set `WEBHOOK_SECRET` to that `whsec_...` value.
   Redeploy (Render usually redeploys on env change automatically).

> ⚠️ Do not skip step 5. With no secret the server runs in dev mode and accepts
> **any** request — anyone could forge a sale and mint a free license.

---

## 4. Host the buyer download — ~10 min

1. `python scripts/build.py` → produces `ict-vault.kevin` (+ the keys, which stay
   secret).
2. `python scripts/deliver.py --hosted` → builds `delivery/plugict/` **and**
   `delivery/plugict.zip` (app + vault + `setup.bat/.sh` + example configs,
   **no** `license.key`, **no** `.vault_key` — it aborts if a license sneaks in).
3. Create a **GitHub Release** on `plugICT` (tag `v1.0`) and attach
   `plugict.zip`. The vault is AES-encrypted and useless without a per-buyer
   `license.key`, so a public Release asset is safe by design. The new release
   becomes `/releases/latest` — exactly where the license email points (the demo
   keeps its own `v1.0-demo` tag, so the landing-page demo link is unaffected).
4. Confirm `ICT_VAULT_DOWNLOAD_URL` (in Render env / `render.yaml`) points at the
   release — the `/releases/latest` default works once a release exists.

> Two rules: **`.vault_key` never appears in any zip, the repo, or a Release**
> (it lives only on your machine and in Render's Secret Files). And **only the
> `--hosted` zip is ever uploaded publicly** — a per-buyer folder
> (`python scripts/deliver.py buyer@email ORDER-1`) contains that buyer's real
> `license.key` and is for direct manual delivery only.

---

## 5. End-to-end test (do this before real buyers) — ~10 min

Use Stripe **test mode** (toggle in the dashboard; test-mode has its own webhook
secret — set that on Render while testing, swap to live before launch).

1. Open the landing page → **Get Lifetime Access → Card**.
2. Test card `4242 4242 4242 4242`, any future date, any CVC, pay.
3. Within a minute you should receive **"🔌 Your PlugICT license is ready"** with
   the license ID, download link, setup steps, and `license.key` attached.
4. Render logs should show `{"status":"issued"}`. Trigger Stripe's "resend" on the
   event → logs show `{"status":"duplicate"}` and **no second email** (idempotency).
5. Download the zip → unzip → drop in `license.key` → run `setup.bat` → connect
   Claude Desktop with the generated `examples/claude_desktop_config.json` →
   ask "What is FVG?" → cited answer. ✅

If step 3 produces nothing, check Render logs: a `401 bad signature` means the
`WEBHOOK_SECRET` doesn't match this mode's signing secret (test vs live mismatch
is the usual cause).

---

## 6. Go live

- Swap Stripe to **live mode**, repoint/confirm the live webhook + its live
  `whsec_...` on Render.
- Confirm `SMTP_FROM` uses your verified domain.
- Do one more real card test on yourself, refund it in Stripe, done.

---

## Manual methods (USDT / DuitNow QR) — no webhook

These have no automatic callback. When you confirm the payment (tx hash / bank
receipt), issue by hand — same license pipeline:

```bash
python store/issue_license.py buyer@email ORDER-ID --method usdt --email
python store/issue_license.py buyer@email ORDER-ID --method duitnow --email
```

---

## Shipping NEW videos to existing buyers (ICT uploads more content)

The vault key is **stable across rebuilds** (`build.py` reuses `.vault_key`
unless you pass `--rotate-key`), so a vault rebuilt with new videos still opens
with **every license already issued**. Existing buyers just re-download and keep
their same `license.key`.

1. Drop the new transcript `.md` files into your source library.
2. Rebuild the search infra + vault (reprocesses everything, **reuses the key**):
   ```bash
   python scripts/ict_ingest.py          # rebuilds FTS5 + Chroma + kg.db
   python scripts/build.py               # reuses .vault_key -> old licenses stay valid
   python scripts/deliver.py --hosted    # -> delivery/plugict.zip
   ```
3. Upload the new `plugict.zip` to the GitHub Release (new tag, e.g. `v1.1`, or
   replace the `v1.0` asset). `/releases/latest` keeps the email link correct.
4. Tell existing buyers to re-download and replace their `ict-vault.kevin` — their
   existing `license.key` opens it unchanged. (New buyers: nothing changes.)
5. Update the video count in the landing page / schema / emails if it changed.

> **Measure before you ship.** After any rebuild — or any change to chunking,
> embeddings, reranking, FTS, or KG expansion — run the retrieval eval against
> your real vault and compare to the last run:
> ```bash
> ICT_VAULT_FILE=…/ict-vault.kevin ICT_VAULT_LICENSE=…/license.key \
>   python tests/run_benchmark.py --json bench-$(date +%F).json
> ```
> It reports top-1 hit rate, top-5 recall, timing, and a per-category breakdown
> (definition / comparison / timing / news / model / …), and fails if top-5
> recall drops below 80%. That's how you know a change actually helped instead
> of guessing.

> **Guard your `.vault_key`.** Losing it means you can't rebuild an
> update-compatible vault (you'd be forced to `--rotate-key`, which re-licenses
> everyone). Keep a secure backup — it's the one irreplaceable secret.
>
> **`--rotate-key`** is only for a security incident (key leaked). It mints a new
> key and **invalidates every issued license** — you'd have to re-issue them all.

(Needs the same `SMTP_*` and `ICT_SOURCE_DIR` env as the webhook.)
