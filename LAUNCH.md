# PlugICT — Launch Day Checklist

One ordered sequence: from "code is ready" to "a real buyer can pay and get their
vault." Do it top to bottom. Each step has a **✓ check** — don't move on until it
passes.

Backend detail lives in `store/DEPLOY.md`; this is the run sheet.

---

## Phase 0 — Confirm what's already live (5 min, phone is fine)

Verify, don't assume. Open these in a browser:

- [ ] **Landing page serves** → visit `https://godzillacode0000.github.io/plugICT/`
      ✓ check: the dark PlugICT page loads (not a 404 "There isn't a GitHub Pages
      site here"). If it 404s, GitHub Pages isn't enabled → **Settings → Pages →
      Source: `main` / `/ (root)` → Save**, wait a few minutes, retry.
- [ ] **Buyer download resolves** → `https://github.com/godzillacode0000/plugICT/releases/latest`
      ✓ check: redirects to **v1.0** and shows `plugict.zip` (~202 MB). *(Verified
      good as of last check — no license inside, full vault.)*
- [ ] **Webhook is up** → `https://plugict-webhook.onrender.com/health`
      ✓ check: returns `{"ok": true}` (first hit may take ~50s if it was asleep).

---

## Phase 1 — On your PC: rebuild the demo + feed the in-page demo (15 min)

Your transcripts + `.vault_key` live here, so these only run on your machine.

```bash
git pull

# 1) Rebuild the free demo (fixes the CURRENTLY BROKEN demo download —
#    its old license/vault were mismatched)
python store/build_demo.py --count 5 --cta "https://plugict.com/#pricing"

# 2) Export the in-page interactive-demo index
python store/export_web_demo.py \
  --vault store/demo_build/ict-vault-demo/ict-vault.kevin \
  --license store/demo_build/ict-vault-demo/license.key \
  --out demo-index.json

git add demo-index.json && git commit -m "Add demo search index for landing page" && git push
```

- [ ] **Demo actually decrypts now** (the old one didn't). In
      `store/demo_build/ict-vault-demo/`:
      ```bash
      cd store/demo_build/ict-vault-demo && python -m venv .venv
      .venv/Scripts/pip install -r requirements.txt      # (mac/Linux: .venv/bin/pip)
      .venv/Scripts/python query.py "Fair Value Gap"
      ```
      ✓ check: returns cited results, **not** "Vault file corrupted."
- [ ] **`demo-index.json` committed** at the repo root.
      ✓ check: after Pages redeploys, the landing page shows a **"Try it — no
      download"** section that answers "FVG" with cited timestamps.

---

## Phase 2 — GitHub: fix the demo release + finish the domain (15 min)

- [ ] **Replace the demo asset**: Releases → `v1.0-demo` → edit → delete the old
      `ict-vault-demo.zip`, upload the freshly rebuilt one.
      ✓ check: `releases/download/v1.0-demo/ict-vault-demo.zip` downloads the new zip.
- [ ] **Fix the price typo**: same release description says "**3.99**" → change to
      **$23.99**. Also update the CTA link if it still says the old repo URL.
- [ ] **Custom domain** (optional but you own plugict.com):
      Settings → Pages → Custom domain → `plugict.com` → Save. GitHub commits a
      `CNAME` file and validates DNS (your A records already point at GitHub).
      Then tick **Enforce HTTPS** once it's available.
      ✓ check: `https://plugict.com` loads the page (give DNS up to ~30 min).
      *(No `CNAME` file exists in the repo yet — this step hasn't been done.)*

> If you set the custom domain, later update `ICT_GETTING_STARTED_URL` in Render
> to `https://plugict.com/#setup` for cleaner emails. Not urgent — the old link
> redirects.

---

## Phase 3 — The real test purchase (THE proof) — 15 min

> **Pre-flight — the one that quietly breaks everything:** the `.vault_key` on
> Render (Secret Files) **must be the exact key that encrypted the
> `ict-vault.kevin` inside the hosted `plugict.zip`.** `build.py` mints a *new*
> random key every run, so if the hosted vault and Render's key came from two
> different `build.py` runs, every real buyer gets "Vault file corrupted" — the
> same failure that broke the demo. Confirm they're from the **same build** before
> paying. The test below is what catches it if they aren't.

Nothing else substitutes for one real payment flowing end-to-end. Stripe is in
**live mode**, so use a real card and refund yourself after.

- [ ] Open the landing page → **Get Lifetime Access → Card** → pay the real $23.99
      with your own card, using an email you can check.
- [ ] **Webhook fired**: Render → `plugict-webhook` → Logs.
      ✓ check: `POST /webhook/stripe … 200` and `{"status":"issued"}` (allow ~50s
      if the instance was asleep — Stripe retries, so it won't be lost).
- [ ] **License email arrived**: subject "🔌 Your PlugICT license is ready", with
      the license ID, download link, setup steps, and `license.key` attached.
- [ ] **Download works**: click the email's link → `plugict.zip` downloads.
- [ ] **Vault opens**: unzip → drop the emailed `license.key` next to `setup.bat`
      → run `setup.bat` → connect Claude Desktop with the generated
      `examples/claude_desktop_config.json` → restart → ask **"What is FVG?"**
      ✓ check: a cited answer with a real timestamp. **This is the whole product
      working for a real buyer.**
- [ ] **No duplicate on retry**: Stripe → that event → "Resend".
      ✓ check: Render logs show `{"status":"duplicate"}` and **no second email**.
- [ ] **Refund yourself**: Stripe → Transactions → that payment → Refund (full).

---

## Phase 4 — You're live

- [ ] Landing page reachable, buy button → Stripe, real card charges.
- [ ] Test purchase delivered a working license email + openable vault.
- [ ] Demo download fixed; in-page demo answering.

Then it's real: share the link. Watch Render logs + your inbox for the first
real order.

---

## Optional polish (anytime, not blockers)

- **Richer in-page demo**: ask Claude (connected to the vault) 6–8 popular
  questions, paste the real answers into `PLUGICT.demoQA` in `index.html` so
  visitors get full reasoned answers, not just search snippets.
- **Demo video**: OBS screen-record ~75s of the real "ask → cited answer → click
  timestamp" flow → unlisted YouTube → paste the embed URL into
  `PLUGICT.demoVideoUrl`. The video section then appears automatically.
- **Manual orders** (USDT / DuitNow) have no webhook — issue by hand:
  `python store/issue_license.py buyer@email ORDER-ID --method usdt --email`

---

## If something breaks

- **No email after paying** → Render logs. `401 bad signature` = `WEBHOOK_SECRET`
  doesn't match live mode's `whsec_…`. Anything else prints the real error.
- **Download link 404s** → the release/asset name moved; confirm `releases/latest`
  → `v1.0` → `plugict.zip`.
- **"Vault file corrupted"** → license and vault are from different builds; the
  buyer's emailed `license.key` is minted from the same `.vault_key` on Render's
  Secret Files, so this shouldn't happen for real buyers — re-check that Render's
  `.vault_key` matches the one that built the hosted `plugict.zip`.
